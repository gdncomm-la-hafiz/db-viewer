"""Turn a structured request into a parameterized SELECT.

No text from the user ever becomes SQL: table and column names must exist in the
catalog and are quoted as identifiers, operators, join kinds and aggregate
functions come from fixed maps in this file, and values are always bind
parameters.

Tables are aliased t0 (the base table) .. t3. A column reference is `tN.column`;
a bare `column` means `t0.column`.
"""
import re
from dataclasses import dataclass

from psycopg import sql

OPERATORS = {
    "=": "=", "!=": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">=",
    "LIKE": "LIKE", "ILIKE": "ILIKE", "IN": "IN",
    "IS NULL": "IS NULL", "IS NOT NULL": "IS NOT NULL",
}
NO_VALUE_OPERATORS = {"IS NULL", "IS NOT NULL"}
JOIN_KINDS = {"LEFT": "LEFT JOIN", "INNER": "JOIN"}
# UI name -> (SQL function, label prefix). COUNT(*) is the only one without a column.
AGGREGATES = {
    "COUNT(*)": ("count", "count"),
    "COUNT": ("count", "count"),
    "COUNT DISTINCT": ("count", "count_distinct"),
    "SUM": ("sum", "sum"),
    "AVG": ("avg", "avg"),
    "MIN": ("min", "min"),
    "MAX": ("max", "max"),
}
MAX_JOINS = 3
_ALIASED_REF = re.compile(r"^t(\d)\.(.+)$", re.DOTALL)

# (schema, table) -> column names in table order
Catalog = dict[tuple[str, str], list[str]]


class QueryError(ValueError):
    """The request names something the catalog doesn't have, or is malformed."""


@dataclass(frozen=True)
class Filter:
    column: str
    op: str
    value: str = ""


@dataclass(frozen=True)
class Join:
    kind: str
    schema: str
    table: str
    left: str   # reference to the base table or an earlier join
    right: str  # column of the joined table


@dataclass(frozen=True)
class Aggregate:
    func: str
    column: str = ""


@dataclass(frozen=True)
class QueryRequest:
    schema: str
    table: str
    columns: tuple[str, ...] = ()
    filters: tuple[Filter, ...] = ()
    order_by: str | None = None
    descending: bool = False
    limit: int | None = None
    joins: tuple[Join, ...] = ()
    aggregates: tuple[Aggregate, ...] = ()


@dataclass(frozen=True)
class _Table:
    schema: str
    table: str
    columns: list[str]
    label: str  # used in output names when the query has joins


def aggregate_label(func: str, column_label: str = "") -> str:
    """Output name of an aggregate, e.g. "count" or "sum(price)"; func must be a key of AGGREGATES."""
    func = " ".join(func.strip().upper().split())
    return "count" if func == "COUNT(*)" else f"{AGGREGATES[func][1]}({column_label})"


def _identifier(*names: str) -> sql.Identifier:
    # psycopg scans the final query text for %s / %(name)s, quoted identifiers included,
    # so a name containing % would be misread as a placeholder.
    for name in names:
        if "%" in name:
            raise QueryError(f"name {name!r} contains '%', which is not supported")
    return sql.Identifier(*names)


def _tables(catalog: Catalog, req: QueryRequest) -> list[_Table]:
    if len(req.joins) > MAX_JOINS:
        raise QueryError(f"at most {MAX_JOINS} joins are supported")
    tables: list[_Table] = []
    for schema, table in [(req.schema, req.table)] + [(j.schema, j.table) for j in req.joins]:
        columns = catalog.get((schema, table))
        if columns is None:
            raise QueryError(f"unknown table {schema}.{table}")
        seen_before = any(t.table == table for t in tables)
        tables.append(_Table(schema, table, columns, f"{table}#{len(tables)}" if seen_before else table))
    return tables


def build_select(catalog: Catalog, req: QueryRequest, default_rows: int, max_rows: int) -> tuple[sql.Composed, list[str]]:
    tables = _tables(catalog, req)
    has_joins = bool(req.joins)

    def resolve(ref: str, visible: int) -> tuple[int, str]:
        """(alias index, column) for a reference; `visible` = how many tables it may see."""
        match = _ALIASED_REF.match(ref)
        index, column = (int(match[1]), match[2]) if match else (0, ref)
        if index >= visible:
            raise QueryError(f"{ref} refers to a table not joined yet")
        if column not in tables[index].columns:
            raise QueryError(f"unknown column {ref}")
        return index, column

    def column_sql(index: int, column: str) -> sql.Identifier:
        return _identifier(f"t{index}", column)

    def column_label(index: int, column: str) -> str:
        return f"{tables[index].label}.{column}" if has_joins else column

    # FROM and JOINs
    from_sql = [sql.SQL("{} AS {}").format(_identifier(req.schema, req.table), sql.Identifier("t0"))]
    for i, join in enumerate(req.joins, start=1):
        kind = join.kind.strip().upper()
        if kind not in JOIN_KINDS:
            raise QueryError(f"unsupported join {join.kind}; use LEFT or INNER")
        left = resolve(join.left, visible=i)
        if join.right not in tables[i].columns:
            raise QueryError(f"unknown column {join.right} in {join.schema}.{join.table}")
        from_sql.append(sql.SQL(" {} {} AS {} ON {} = {}").format(
            sql.SQL(JOIN_KINDS[kind]), _identifier(join.schema, join.table), sql.Identifier(f"t{i}"),
            column_sql(*left), column_sql(i, join.right),
        ))

    # SELECT list
    if req.columns:
        selected = [resolve(c, len(tables)) for c in req.columns]
    elif req.aggregates:
        selected = []
    else:
        selected = [(i, c) for i, t in enumerate(tables) for c in t.columns]
    items, labels = [], []
    for index, column in selected:
        items.append(sql.SQL("{} AS {}").format(column_sql(index, column), sql.Identifier(column_label(index, column))))
        labels.append(column_label(index, column))
    for agg in req.aggregates:
        func = " ".join(agg.func.strip().upper().split())
        if func not in AGGREGATES:
            raise QueryError(f"unsupported aggregate {agg.func}")
        sql_func = AGGREGATES[func][0]
        if func == "COUNT(*)":
            if agg.column:
                raise QueryError("COUNT(*) takes no column")
            expr, label = sql.SQL("count(*)"), aggregate_label(func)
        else:
            if not agg.column:
                raise QueryError(f"{func} needs a column")
            ref = resolve(agg.column, len(tables))
            distinct = "DISTINCT " if func == "COUNT DISTINCT" else ""
            expr = sql.SQL(f"{sql_func}({distinct}{{}})").format(column_sql(*ref))
            label = aggregate_label(func, column_label(*ref))
        items.append(sql.SQL("{} AS {}").format(expr, _identifier(label)))
        labels.append(label)
    if not items:
        raise QueryError(f"table {req.schema}.{req.table} has no columns")
    duplicates = {label for label in labels if labels.count(label) > 1}
    if duplicates:
        raise QueryError(f"output column {sorted(duplicates)[0]} appears twice")

    # WHERE
    params: list[str] = []
    conditions = []
    for f in req.filters:
        op = f.op.strip().upper()
        if op not in OPERATORS:
            raise QueryError(f"unsupported operator {f.op}")
        ident = column_sql(*resolve(f.column, len(tables)))
        if op in NO_VALUE_OPERATORS:
            conditions.append(sql.SQL("{} " + OPERATORS[op]).format(ident))
        elif op == "IN":
            items_in = [v.strip() for v in f.value.split(",") if v.strip()]
            if not items_in:
                raise QueryError(f"IN on {f.column} needs at least one value")
            placeholders = sql.SQL(", ").join([sql.Placeholder()] * len(items_in))
            conditions.append(sql.SQL("{} IN ({})").format(ident, placeholders))
            params.extend(items_in)
        else:
            conditions.append(sql.SQL("{} " + OPERATORS[op] + " {}").format(ident, sql.Placeholder()))
            params.append(f.value)

    limit = req.limit if req.limit is not None else default_rows
    if limit < 1:
        raise QueryError("limit must be at least 1")
    limit = min(limit, max_rows)

    parts = [sql.SQL("SELECT {} FROM ").format(sql.SQL(", ").join(items)), *from_sql]
    if conditions:
        parts.append(sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions))
    if req.aggregates and selected:
        parts.append(sql.SQL(" GROUP BY ") + sql.SQL(", ").join(column_sql(i, c) for i, c in selected))
    if req.order_by:
        # An output name (e.g. "count") sorts by that result column; anything else is a column reference.
        target = sql.Identifier(req.order_by) if req.order_by in labels else column_sql(*resolve(req.order_by, len(tables)))
        parts.append(sql.SQL(" ORDER BY {} {}").format(target, sql.SQL("DESC" if req.descending else "ASC")))
    parts.append(sql.SQL(" LIMIT {}").format(sql.Literal(limit)))
    return sql.Composed(parts), params
