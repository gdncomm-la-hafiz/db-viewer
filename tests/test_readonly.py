import pytest

from builder import Filter, QueryError, QueryRequest, build_select
from config import parse_config
from db import Databases, DatabaseError, QueryTimeout, explain, run_readonly
from tests.helpers import LOCAL_INSTANCE, TEST_DATABASE, TEST_SCHEMA

pytestmark = pytest.mark.integration
SEQ = f"{TEST_SCHEMA}.item_id_seq"


@pytest.fixture
def dbs(local_env, unchanged_db):
    config = parse_config({"limits": {"statement_timeout_ms": 2000}, "instances": {"local": LOCAL_INSTANCE}})
    databases = Databases(config, local_env)
    yield databases
    databases.close()


@pytest.fixture
def pool(dbs):
    return dbs.pool("local", TEST_DATABASE)


def test_catalog_lists_tables_only(dbs):
    catalog = dbs.catalog("local", TEST_DATABASE)
    assert catalog[(TEST_SCHEMA, "item")][:2] == ["id", "code"]
    assert (TEST_SCHEMA, "warehouse") in catalog
    assert (TEST_SCHEMA, "item_id_seq") not in catalog
    assert (TEST_SCHEMA, "item_name_idx") not in catalog
    assert all(not schema.startswith("pg_") and schema != "information_schema" for schema, _ in catalog)


def test_runs_inside_read_only_transaction_with_timeout(pool):
    result = run_readonly(pool, "SELECT current_setting('transaction_read_only'), current_setting('statement_timeout')", None, 2000)
    assert result.rows == [("on", "2s")]


@pytest.mark.parametrize("statement", [f"SELECT nextval('{SEQ}')", f"SELECT setval('{SEQ}', 1)"])
def test_side_effect_functions_are_refused(pool, statement):
    with pytest.raises(DatabaseError, match="read-only transaction"):
        run_readonly(pool, statement, None, 2000)


def test_transactional_writes_are_rolled_back(pool):
    # Postgres lets lo_create run inside READ ONLY; the ROLLBACK is what undoes it.
    count = "SELECT count(*) FROM pg_largeobject_metadata"
    before = run_readonly(pool, count, None, 2000).rows
    assert run_readonly(pool, "SELECT lo_create(0)", None, 2000).rows[0][0] > 0
    assert run_readonly(pool, count, None, 2000).rows == before


def test_dates_python_cannot_hold_come_back_as_text(pool):
    result = run_readonly(
        pool,
        "SELECT 'infinity'::timestamptz, '10000-01-01'::date, '0001-01-01 BC'::date, '1 day'::interval",
        None, 2000,
    )
    assert result.rows == [("infinity", "10000-01-01", "0001-01-01 BC", "1 day")]


def test_a_table_holding_an_infinity_date_can_be_browsed(dbs, pool):
    catalog = dbs.catalog("local", TEST_DATABASE)
    query, params = build_select(catalog, QueryRequest(TEST_SCHEMA, "item", columns=("code", "valid_to"), order_by="code"), 50, 500)
    assert run_readonly(pool, query, params, 2000).rows == [("ITM-002", None), ("ITM-003", None), ("ITX-001", "infinity")]


def test_long_query_times_out_and_pool_recovers(pool):
    with pytest.raises(QueryTimeout):
        run_readonly(pool, "SELECT pg_sleep(20)", None, 1000)
    assert run_readonly(pool, "SELECT 1", None, 1000).rows == [(1,)]


def test_session_changes_do_not_survive_the_rollback(pool):
    run_readonly(pool, "SET application_name = 'changed'", None, 1000)
    for _ in range(4):  # pool max_size is 3, so this touches every connection
        assert run_readonly(pool, "SELECT current_setting('application_name')", None, 1000).rows == [("db-viewer",)]


def test_table_info_shows_columns_constraints_and_indexes(dbs):
    info = dbs.table_info("local", TEST_DATABASE, TEST_SCHEMA, "item")
    columns = {c["name"]: c for c in info["columns"]}
    assert (columns["id"]["type"], columns["id"]["nullable"]) == ("bigint", False)
    assert "nextval" in columns["id"]["default"]
    assert columns["code"]["type"] == "character varying(20)"
    assert columns["description"]["nullable"] is True
    kinds = {c["name"]: c["kind"] for c in info["constraints"]}
    assert kinds["item_pkey"] == "primary key"
    assert kinds["item_code_key"] == "unique"
    assert kinds["item_warehouse_code_fkey"] == "foreign key"
    assert "not null" not in kinds.values()
    indexes = {i["name"]: i for i in info["indexes"]}
    assert (indexes["item_pkey"]["primary"], indexes["item_pkey"]["unique"], indexes["item_pkey"]["method"]) == (True, True, "btree")
    assert "(name)" in indexes["item_name_idx"]["definition"]
    assert indexes["item_name_idx"]["unique"] is False
    assert indexes["item_pkey"]["size"].endswith(("bytes", "kB", "MB", "GB"))
    assert info["join_suggestions"] == [{
        "kind": "LEFT", "schema": TEST_SCHEMA, "table": "warehouse",
        "left": "warehouse_code", "right": "code", "via": "item_warehouse_code_fkey",
    }]


def test_table_info_shows_foreign_keys_pointing_in(dbs):
    info = dbs.table_info("local", TEST_DATABASE, TEST_SCHEMA, "warehouse")
    incoming = [c for c in info["constraints"] if c["direction"] == "in"]
    assert [(c["name"], c["table"]) for c in incoming] == [("item_warehouse_code_fkey", f"{TEST_SCHEMA}.item")]
    assert info["join_suggestions"] == [{
        "kind": "LEFT", "schema": TEST_SCHEMA, "table": "item",
        "left": "code", "right": "warehouse_code", "via": "item_warehouse_code_fkey",
    }]


def test_table_info_for_a_table_outside_the_catalog_is_refused(dbs):
    with pytest.raises(QueryError, match="unknown table"):
        dbs.table_info("local", TEST_DATABASE, "pg_catalog", "pg_authid")


def test_explain_shows_the_plan_without_running_it(dbs, pool):
    catalog = dbs.catalog("local", TEST_DATABASE)
    query, params = build_select(catalog, QueryRequest(TEST_SCHEMA, "item", filters=(Filter("code", "=", "ITM-002"),)), 50, 500)
    plan = explain(pool, query, params, 2000)
    assert plan.sql_text.startswith("EXPLAIN SELECT")
    assert any("Scan" in line for line in plan.lines)
