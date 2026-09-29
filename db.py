"""Connection pools, the table catalog, and run_readonly: the only place SQL runs."""
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass

import psycopg
from psycopg import errors, sql
from psycopg.conninfo import make_conninfo
from psycopg.types.string import TextLoader
from psycopg_pool import ConnectionPool

from builder import Catalog, QueryError
from config import Config

CATALOG_TTL_SECONDS = 600
POOL_TIMEOUT_SECONDS = 10
# Postgres holds dates Python can't ('infinity', year 10000, BC); one such value
# would fail the whole fetch, so these types come back as Postgres prints them.
TEXT_LOADED_TYPES = ("date", "timestamp", "timestamptz", "interval")
# Ordinary and partitioned tables only; partitions show up through their parent.
# No '%' in this text: psycopg would read it as a placeholder.
CATALOG_SQL = """
    SELECT n.nspname, c.relname, a.attname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    WHERE c.relkind IN ('r', 'p')
      AND NOT c.relispartition
      AND n.nspname <> 'information_schema'
      AND left(n.nspname, 3) <> 'pg_'
      AND has_table_privilege(c.oid, 'SELECT')
    ORDER BY n.nspname, c.relname, a.attnum
"""


# Table structure. Each takes the schema and table name as parameters; callers
# check the table is in the catalog first. No '%' literals (see CATALOG_SQL).
COLUMNS_SQL = """
    SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
"""
# Constraints on the table plus foreign keys pointing at it. PG18 not-null
# constraints ('n') and per-partition copies (conparentid <> 0) are left out.
CONSTRAINTS_SQL = """
    SELECT con.conname, con.contype, pg_get_constraintdef(con.oid), con.conrelid = c.oid,
           sn.nspname || '.' || s.relname,
           (SELECT array_agg(a.attname ORDER BY k.o) FROM unnest(con.conkey) WITH ORDINALITY k(n, o)
              JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n),
           tn.nspname || '.' || t.relname,
           (SELECT array_agg(a.attname ORDER BY k.o) FROM unnest(con.confkey) WITH ORDINALITY k(n, o)
              JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_constraint con ON con.conrelid = c.oid OR (con.confrelid = c.oid AND con.contype = 'f')
    JOIN pg_class s ON s.oid = con.conrelid
    JOIN pg_namespace sn ON sn.oid = s.relnamespace
    LEFT JOIN pg_class t ON t.oid = con.confrelid
    LEFT JOIN pg_namespace tn ON tn.oid = t.relnamespace
    WHERE n.nspname = %s AND c.relname = %s AND con.contype <> 'n' AND con.conparentid = 0
    ORDER BY con.conrelid = c.oid DESC, con.contype, con.conname
"""
# Size and scan counts are summed over partitions, so a partitioned parent shows real
# numbers; pg_partition_tree returns no rows for an unpartitioned index, hence COALESCE.
INDEXES_SQL = """
    SELECT i.relname, pg_get_indexdef(i.oid), x.indisunique, x.indisprimary, am.amname,
           pg_size_pretty(COALESCE((SELECT sum(pg_relation_size(p.relid)) FROM pg_partition_tree(i.oid) p),
                                   pg_relation_size(i.oid))),
           COALESCE((SELECT sum(ps.idx_scan) FROM pg_partition_tree(i.oid) p
                       JOIN pg_stat_all_indexes ps ON ps.indexrelid = p.relid), s.idx_scan)
    FROM pg_index x
    JOIN pg_class c ON c.oid = x.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_class i ON i.oid = x.indexrelid
    JOIN pg_am am ON am.oid = i.relam
    LEFT JOIN pg_stat_all_indexes s ON s.indexrelid = i.oid
    WHERE n.nspname = %s AND c.relname = %s
    ORDER BY x.indisprimary DESC, i.relname
"""
CONSTRAINT_KINDS = {"p": "primary key", "u": "unique", "f": "foreign key", "c": "check", "x": "exclusion", "t": "trigger"}


class DatabaseError(Exception):
    """Postgres refused the query, or the database can't be reached."""


class QueryTimeout(Exception):
    """The statement ran past statement_timeout and was cancelled."""


class UnknownDatabase(LookupError):
    """The instance/database pair isn't in config.yaml."""


@dataclass(frozen=True)
class Result:
    columns: list[str]
    rows: list[tuple]
    duration_ms: int
    sql_text: str


def run_readonly(pool: ConnectionPool, query, params, timeout_ms: int) -> Result:
    """Run one statement inside BEGIN READ ONLY ... ROLLBACK with a statement timeout.

    The transaction wrapper lives here, so no caller can send its own BEGIN, SET
    or COMMIT around the statement.
    """
    started = time.monotonic()
    try:
        with pool.connection() as conn:
            try:
                sql_text, columns, rows = _run_in_readonly_transaction(conn, query, params, timeout_ms)
            except psycopg.Error:
                conn.close()  # never hand a connection in an unknown state back to the pool
                raise
    except errors.QueryCanceled as e:
        raise QueryTimeout(f"query took over {timeout_ms // 1000} s") from e
    except psycopg.Error as e:
        raise DatabaseError(_short_message(e)) from e
    return Result(columns, rows, int((time.monotonic() - started) * 1000), sql_text)


@dataclass(frozen=True)
class Plan:
    lines: list[str]
    duration_ms: int
    sql_text: str


def explain(pool: ConnectionPool, query: sql.Composable, params, timeout_ms: int) -> Plan:
    """The planner's plan for a built query. Plain EXPLAIN never runs the query; ANALYZE would."""
    result = run_readonly(pool, sql.SQL("EXPLAIN ") + query, params, timeout_ms)
    return Plan([row[0] for row in result.rows], result.duration_ms, result.sql_text)


def join_suggestions(constraints: list[dict], known=None) -> list[dict]:
    """One-click joins from single-column foreign keys, in either direction.

    `known` (e.g. the catalog) limits them to tables the viewer can join; an FK can
    point at a partition or a table the login can't read.
    """
    suggestions = []
    for con in constraints:
        if con["kind"] != "foreign key" or len(con["columns"]) != 1:
            continue
        if con["direction"] == "out":
            other, left, right = con["ref_table"], con["columns"][0], con["ref_columns"][0]
        else:
            other, left, right = con["table"], con["ref_columns"][0], con["columns"][0]
        schema, _, table = other.partition(".")
        if known is not None and (schema, table) not in known:
            continue
        suggestions.append({"kind": "LEFT", "schema": schema, "table": table, "left": left, "right": right, "via": con["name"]})
    return suggestions


def _run_in_readonly_transaction(conn, query, params, timeout_ms):
    with conn.cursor() as cur:
        cur.execute("BEGIN READ ONLY")
        cur.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(timeout_ms)))
        cur.execute(sql.SQL("SET LOCAL idle_in_transaction_session_timeout = {}").format(sql.Literal(timeout_ms)))
        sql_text = query.as_string(conn) if isinstance(query, sql.Composable) else query
        cur.execute(query, params, prepare=False)
        columns = [d.name for d in cur.description] if cur.description else []
        rows = cur.fetchall() if cur.description else []
    conn.execute("ROLLBACK")
    return sql_text, columns, rows


def _configure(conn) -> None:
    for type_name in TEXT_LOADED_TYPES:
        conn.adapters.register_loader(type_name, TextLoader)


def _short_message(e: psycopg.Error) -> str:
    diag = getattr(e, "diag", None)
    message = (diag.message_primary if diag and diag.message_primary else str(e)) or type(e).__name__
    return message.splitlines()[0][:300]


class Databases:
    def __init__(self, config: Config, env: Mapping[str, str]):
        self._config = config
        self._env = env
        self._pools: dict[tuple[str, str], ConnectionPool] = {}
        self._catalogs: dict[tuple[str, str], tuple[float, Catalog]] = {}
        self._lock = threading.Lock()

    def pool(self, instance: str, database: str) -> ConnectionPool:
        inst = self._config.instances.get(instance)
        if inst is None or database not in inst.databases:
            raise UnknownDatabase(f"unknown database {instance}/{database}")
        key = (instance, database)
        with self._lock:
            if key not in self._pools:
                password = self._env.get(inst.password_env)
                if not password:
                    raise DatabaseError(f"{inst.password_env} is not set; ask the admin to add it to secrets.env")
                conninfo = make_conninfo(
                    host=inst.host, port=inst.port, dbname=database, user=inst.user, password=password,
                    application_name="db-viewer", connect_timeout=5,
                )
                self._pools[key] = ConnectionPool(
                    conninfo, min_size=1, max_size=3, timeout=POOL_TIMEOUT_SECONDS,
                    kwargs={"autocommit": True}, configure=_configure, name=f"{instance}/{database}", open=True,
                )
            return self._pools[key]

    def catalog(self, instance: str, database: str, refresh: bool = False) -> Catalog:
        key = (instance, database)
        cached = self._catalogs.get(key)
        if cached and not refresh and time.monotonic() - cached[0] < CATALOG_TTL_SECONDS:
            return cached[1]
        result = run_readonly(self.pool(instance, database), CATALOG_SQL, None, self._config.limits.statement_timeout_ms)
        catalog: Catalog = {}
        for schema, table, column in result.rows:
            catalog.setdefault((schema, table), []).append(column)
        self._catalogs[key] = (time.monotonic(), catalog)
        return catalog

    def table_info(self, instance: str, database: str, schema: str, table: str) -> dict:
        if (schema, table) not in self.catalog(instance, database):
            raise QueryError(f"unknown table {schema}.{table}")
        pool, timeout, params = self.pool(instance, database), self._config.limits.statement_timeout_ms, [schema, table]
        columns = [
            {"name": name, "type": type_, "nullable": nullable, "default": default}
            for name, type_, nullable, default in run_readonly(pool, COLUMNS_SQL, params, timeout).rows
        ]
        constraints = [
            {"name": name, "kind": CONSTRAINT_KINDS.get(kind, kind), "definition": definition,
             "direction": "out" if outgoing else "in", "table": source, "columns": cols or [],
             "ref_table": ref_table, "ref_columns": ref_cols or []}
            for name, kind, definition, outgoing, source, cols, ref_table, ref_cols
            in run_readonly(pool, CONSTRAINTS_SQL, params, timeout).rows
        ]
        indexes = [
            {"name": name, "definition": definition, "unique": unique, "primary": primary,
             "method": method, "size": size, "scans": int(scans) if scans is not None else None}
            for name, definition, unique, primary, method, size, scans
            in run_readonly(pool, INDEXES_SQL, params, timeout).rows
        ]
        return {"columns": columns, "constraints": constraints, "indexes": indexes,
                "join_suggestions": join_suggestions(constraints, known=self.catalog(instance, database))}

    def close(self) -> None:
        with self._lock:
            for pool in self._pools.values():
                pool.close()
            self._pools.clear()
