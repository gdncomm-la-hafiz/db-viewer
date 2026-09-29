import json
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from app import create_app
from auth import hash_password
from tests.helpers import LOCAL_INSTANCE, TEST_DATABASE, TEST_SCHEMA

pytestmark = pytest.mark.integration
PASSWORD = "correct-horse-1"
DB = {"instance": "local", "database": TEST_DATABASE}
QUERY = {**DB, "schema_name": TEST_SCHEMA, "table": "item"}
ITEM = f"{TEST_SCHEMA}.item"
FORM = {"db": f"local/{TEST_DATABASE}", "table": ITEM}


def saved(sql, params=()):
    return {"title": sql[:20], **DB, "params": list(params), "sql": sql}


@pytest.fixture
def client(tmp_path, local_env, unchanged_db):
    config = {
        "limits": {"max_rows": 500, "default_rows": 50, "statement_timeout_ms": 2000},
        "instances": {"local": {**LOCAL_INSTANCE, "environment": "Local"}},
        "users": {"junior": {"password_hash": hash_password(PASSWORD)}},
        "saved_queries": {
            "by_code": saved(f"SELECT id, code FROM {ITEM} WHERE code = %(code)s", ["code"]),
            "sneaky_nextval": saved(f"SELECT nextval('{TEST_SCHEMA}.item_id_seq')"),
            "sneaky_lo": saved("SELECT lo_create(0)"),
            "slow": saved("SELECT pg_sleep(20)"),
            "html": saved("SELECT '<script>alert(1)</script>' AS v"),
            "big": saved("SELECT 9007199254740993::bigint AS id, 'a,b' AS v"),
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    app = create_app(tmp_path / "config.yaml", tmp_path / "audit.log", local_env)
    with TestClient(app) as c:
        c.audit_path = tmp_path / "audit.log"
        yield c


def login(c, password=PASSWORD):
    return c.post("/login", data={"username": "junior", "password": password}, follow_redirects=False)


def audit_lines(c, event=None):
    if not c.audit_path.exists():
        return []
    lines = [json.loads(line) for line in c.audit_path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if event is None or line["event"] == event]


# --- auth ---

def test_api_requires_login(client):
    r = client.post("/api/query", json=QUERY)
    assert r.status_code == 401
    assert audit_lines(client, "query") == []


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/databases"),
    ("GET", f"/api/tables?db=local/{TEST_DATABASE}"),
    ("GET", "/api/saved"),
    ("POST", "/api/explain"),
    ("GET", f"/api/table-info?db=local/{TEST_DATABASE}&table={ITEM}"),
])
def test_every_api_endpoint_requires_login(client, method, path):
    assert client.request(method, path, json=QUERY if method == "POST" else None).status_code == 401


def test_wrong_password_is_refused_and_audited_without_the_password(client):
    assert login(client, "nope-nope-nope").status_code == 401
    last = audit_lines(client)[-1]
    assert (last["event"], last["user"]) == ("login_failed", "junior")
    assert "nope-nope-nope" not in client.audit_path.read_text(encoding="utf-8")


def test_index_redirects_to_login_when_signed_out(client):
    r = client.get("/", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login")


def test_logout_ends_the_session(client):
    login(client)
    client.post("/logout", follow_redirects=False)
    assert client.post("/api/query", json=QUERY).status_code == 401


# --- single-table queries ---

def test_table_query_returns_rows_and_writes_one_audit_line(client):
    assert login(client).status_code == 303
    r = client.post("/api/query", json={
        **QUERY, "columns": ["id", "code"], "filters": [{"column": "code", "op": "=", "value": "ITM-002"}],
    })
    assert r.status_code == 200, r.text
    assert (r.json()["columns"], r.json()["rows"]) == (["id", "code"], [[2, "ITM-002"]])
    queries = audit_lines(client, "query")
    assert len(queries) == 1
    assert (queries[0]["user"], queries[0]["rows"], queries[0]["table"]) == ("junior", 1, ITEM)


def test_function_call_in_filter_value_is_just_text(client):
    login(client)
    r = client.post("/api/query", json={**QUERY, "filters": [{"column": "code", "op": "=", "value": f"nextval('{TEST_SCHEMA}.item_id_seq')"}]})
    assert (r.status_code, r.json()["rows"]) == (200, [])


def test_like_on_number_column_is_a_clean_error(client):
    login(client)
    r = client.post("/api/query", json={**QUERY, "filters": [{"column": "id", "op": "LIKE", "value": "1%"}]})
    assert r.status_code == 400
    assert "operator does not exist" in r.json()["error"]


def test_unknown_column_is_a_clean_error(client):
    login(client)
    r = client.post("/api/query", json={**QUERY, "columns": ["nope"]})
    assert r.status_code == 400
    assert "unknown column" in r.json()["error"]


def test_infinity_dates_are_shown_as_text(client):
    login(client)
    r = client.post("/api/query", json={**QUERY, "columns": ["code", "valid_to"], "filters": [{"column": "code", "op": "=", "value": "ITX-001"}]})
    assert r.json()["rows"] == [["ITX-001", "infinity"]]


# --- joins and aggregates ---

def test_api_query_with_a_foreign_key_join(client):
    login(client)
    r = client.post("/api/query", json={
        **QUERY, "columns": ["code", "t1.name"], "order_by": "t0.id",
        "joins": [{"kind": "LEFT", "schema_name": TEST_SCHEMA, "table": "warehouse", "left": "warehouse_code", "right": "code"}],
    })
    assert r.status_code == 200, r.text
    assert r.json()["columns"] == ["item.code", "warehouse.name"]
    assert r.json()["rows"] == [["ITX-001", "Marunda"], ["ITM-002", "Cakung"], ["ITM-003", "Marunda"]]
    assert audit_lines(client, "query")[-1]["joins"][0]["table"] == "warehouse"


def test_api_query_self_join(client):
    login(client)
    r = client.post("/api/query", json={
        **QUERY, "columns": ["code", "t1.code"], "order_by": "t0.id", "limit": 1,
        "joins": [{"kind": "INNER", "schema_name": TEST_SCHEMA, "table": "item", "left": "id", "right": "id"}],
    })
    assert (r.json()["columns"], r.json()["rows"]) == (["item.code", "item#1.code"], [["ITX-001", "ITX-001"]])


def test_api_query_count_per_value(client):
    login(client)
    r = client.post("/api/query", json={**QUERY, "columns": ["is_active"], "aggregates": [{"func": "COUNT(*)"}],
                                        "order_by": "count", "descending": True})
    assert r.status_code == 200, r.text
    assert (r.json()["columns"], r.json()["rows"]) == (["is_active", "count"], [[True, 2], [False, 1]])
    assert audit_lines(client, "query")[-1]["aggregates"] == [{"func": "COUNT(*)", "column": ""}]


def test_api_query_sum_per_joined_value(client):
    login(client)
    r = client.post("/api/query", json={
        **QUERY, "columns": ["t1.name"], "aggregates": [{"func": "SUM", "column": "price"}], "order_by": "t1.name",
        "joins": [{"kind": "INNER", "schema_name": TEST_SCHEMA, "table": "warehouse", "left": "warehouse_code", "right": "code"}],
    })
    assert r.json()["rows"] == [["Cakung", "27500.50"], ["Marunda", "20000.00"]]


# --- saved queries ---

def test_saved_query_with_params(client):
    login(client)
    r = client.post("/api/saved/by_code", json={"code": "ITM-002"})
    assert (r.status_code, r.json()["rows"]) == (200, [[2, "ITM-002"]])
    assert audit_lines(client, "query")[-1]["saved_query"] == "by_code"


def test_nextval_fails_in_saved_queries(client):
    login(client)
    r = client.post("/api/saved/sneaky_nextval", json={})
    assert r.status_code == 400
    assert "read-only transaction" in r.json()["error"]


def test_lo_create_in_saved_queries_is_rolled_back(client):
    # Runs (Postgres allows it inside READ ONLY), but the ROLLBACK undoes it;
    # the unchanged_db fixture fails the test if a large object was left behind.
    login(client)
    assert client.post("/api/saved/sneaky_lo", json={}).status_code == 200


def test_slow_query_is_cancelled_and_the_next_one_works(client):
    login(client)
    started = time.monotonic()
    assert client.post("/api/saved/slow", json={}).status_code == 408
    assert time.monotonic() - started < 6
    assert client.post("/api/saved/by_code", json={"code": "ITM-002"}).json()["rows"] == [[2, "ITM-002"]]


def test_unknown_saved_query_is_404(client):
    login(client)
    assert client.post("/api/saved/nope", json={}).status_code == 404


# --- discovery, structure, explain ---

def test_api_lists_databases(client):
    login(client)
    assert client.get("/api/databases").json() == {"databases": [
        {"instance": "local", "title": "Local Postgres", "database": TEST_DATABASE},
    ]}


def test_api_lists_tables_with_columns(client):
    login(client)
    tables = {t["name"]: t for t in client.get("/api/tables", params={"db": f"local/{TEST_DATABASE}"}).json()["tables"]}
    assert (tables[ITEM]["schema"], tables[ITEM]["table"]) == (TEST_SCHEMA, "item")
    assert tables[ITEM]["columns"][:2] == ["id", "code"]


def test_api_tables_for_unknown_database_is_400(client):
    login(client)
    r = client.get("/api/tables", params={"db": "local/nope"})
    assert r.status_code == 400
    assert "unknown database" in r.json()["error"]


def test_api_lists_saved_queries(client):
    login(client)
    saved_list = {q["key"]: q for q in client.get("/api/saved").json()["saved"]}
    assert saved_list["by_code"] == {
        "key": "by_code", "title": saved_list["by_code"]["title"], "instance": "local",
        "database": TEST_DATABASE, "params": ["code"],
    }


def test_api_table_info(client):
    login(client)
    r = client.get("/api/table-info", params={"db": f"local/{TEST_DATABASE}", "table": ITEM})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["name"] for c in body["columns"]][:2] == ["id", "code"]
    assert {i["name"] for i in body["indexes"]} >= {"item_pkey", "item_code_key", "item_name_idx"}
    assert body["join_suggestions"][0]["table"] == "warehouse"
    assert audit_lines(client, "table_info")[-1]["table"] == ITEM


def test_api_table_info_for_unknown_table_is_400(client):
    login(client)
    r = client.get("/api/table-info", params={"db": f"local/{TEST_DATABASE}", "table": f"{TEST_SCHEMA}.nope"})
    assert r.status_code == 400
    assert "unknown table" in r.json()["error"]


def test_api_explain_returns_the_plan_and_is_audited(client):
    login(client)
    r = client.post("/api/explain", json={**QUERY, "filters": [{"column": "code", "op": "=", "value": "ITM-002"}]})
    assert r.status_code == 200, r.text
    assert any("Scan" in line for line in r.json()["plan"])
    assert r.json()["sql"].startswith("EXPLAIN SELECT")
    assert audit_lines(client, "explain")[-1]["table"] == ITEM


# --- HTML ---

def test_index_lists_tables_in_the_sidebar(client):
    login(client)
    r = client.get("/", params={"db": FORM["db"]})
    assert r.status_code == 200
    assert f"{TEST_SCHEMA}.warehouse" in r.text


def test_environment_badge_is_shown(client):
    login(client)
    assert 'class="badge' in client.get("/", params=FORM).text


def test_html_run_skips_blank_filter_rows(client):
    login(client)
    r = client.post("/run", data={
        **FORM, "action": "run", "columns": ["id", "code"],
        "filter_column": ["", "code", ""], "filter_op": ["=", "=", "="], "filter_value": ["", "ITM-002", ""], "limit": "10",
    })
    assert r.status_code == 200, r.text
    assert "ITM-002" in r.text
    assert "ITX-001" not in r.text


def test_html_run_rejects_a_bad_limit(client):
    login(client)
    r = client.post("/run", data={**FORM, "action": "run", "limit": "ten"})
    assert r.status_code == 400
    assert "whole number" in r.text


def test_html_join_via_form(client):
    login(client)
    r = client.post("/run", data={
        **FORM, "action": "run", "columns": ["t0.code", "t1.name"], "order_by": "t0.id",
        "join_kind": ["LEFT"], "join_table": [f"{TEST_SCHEMA}.warehouse"], "join_left": ["t0.warehouse_code"], "join_right": ["code"],
    })
    assert r.status_code == 200, r.text
    assert "warehouse.name" in r.text
    assert "Cakung" in r.text


def test_html_join_suggestion_adds_the_join(client):
    login(client)
    r = client.post("/run", data={**FORM, "action": "suggest:0"})
    assert r.status_code == 200, r.text
    assert 'value="t1.name"' in r.text  # warehouse columns are now offered
    assert audit_lines(client, "query") == []


def test_html_count_via_form(client):
    login(client)
    r = client.post("/run", data={**FORM, "action": "run", "columns": ["t0.is_active"],
                                  "agg_func": ["COUNT(*)"], "agg_column": [""]})
    assert r.status_code == 200, r.text
    assert ">count<" in r.text
    assert audit_lines(client, "query")[-1]["rows"] == 2


def test_html_explain_button(client):
    login(client)
    r = client.post("/run", data={**FORM, "action": "explain"})
    assert r.status_code == 200, r.text
    assert "Scan" in r.text
    assert audit_lines(client, "explain")


def test_html_refresh_redraws_without_running(client):
    login(client)
    r = client.post("/run", data={
        **FORM, "action": "refresh",
        "join_kind": ["LEFT"], "join_table": [f"{TEST_SCHEMA}.warehouse"], "join_left": ["t0.warehouse_code"], "join_right": [""],
    })
    assert r.status_code == 200, r.text
    assert 'value="t1.name"' in r.text
    assert audit_lines(client, "query") == []


WAREHOUSE = f"{TEST_SCHEMA}.warehouse"


def test_html_remove_join_renumbers_later_refs_and_drops_the_removed_ones(client):
    login(client)
    r = client.post("/run", data={
        **FORM, "action": "remove_join:0", "columns": ["t0.code", "t1.name", "t2.code"],
        "join_kind": ["LEFT", "LEFT"], "join_table": [WAREHOUSE, ITEM],
        "join_left": ["t0.warehouse_code", "t1.code"], "join_right": ["code", "warehouse_code"],
        "filter_column": ["t1.name", "t2.code"], "filter_op": ["=", "="], "filter_value": ["Marunda", "ITM-003"],
    })
    assert r.status_code == 200, r.text
    assert 'value="t2.' not in r.text           # nothing points past the last table any more
    assert 'value="t1.code" checked' in r.text  # t2.code became t1.code
    assert "ITM-003" in r.text                  # its filter survived, renumbered
    assert "Marunda" not in r.text              # the removed table's filter is gone
    # the surviving join pointed at the removed table: its left side is cleared, not guessed
    assert '<option value="" selected>' in r.text


def test_html_stale_ref_is_kept_visible_instead_of_replaced(client):
    login(client)
    r = client.post("/run", data={**FORM, "action": "refresh",
                                  "filter_column": ["t1.name"], "filter_op": ["="], "filter_value": ["x"]})
    assert 'value="t1.name" selected' in r.text
    r = client.post("/run", data={**FORM, "action": "run",
                                  "filter_column": ["t1.name"], "filter_op": ["="], "filter_value": ["x"]})
    assert r.status_code == 400
    assert "not joined yet" in r.text


def test_html_unknown_join_table_keeps_later_joins_aligned(client):
    login(client)
    data = {**FORM, "join_kind": ["LEFT", "LEFT"], "join_table": ["nosuch.table", WAREHOUSE],
            "join_left": ["t0.id", "t0.warehouse_code"], "join_right": ["", "code"]}
    r = client.post("/run", data={**data, "action": "refresh"})
    assert 'value="t2.name"' in r.text  # warehouse is still the second join
    r = client.post("/run", data={**data, "action": "run"})
    assert r.status_code == 400
    assert "unknown table nosuch.table" in r.text


def test_html_first_aggregate_clears_the_all_columns_selection(client):
    login(client)
    columns = ["t0.id", "t0.code", "t0.name"]
    r = client.post("/run", data={**FORM, "action": "add_agg", "columns": columns})
    assert r.status_code == 200, r.text
    assert 'value="t0.id" checked' not in r.text


def test_html_suggestion_respects_the_join_limit(client):
    login(client)
    three = {"join_kind": ["LEFT"] * 3, "join_table": [WAREHOUSE] * 3,
             "join_left": ["t0.warehouse_code"] * 3, "join_right": ["code"] * 3}
    r = client.post("/run", data={**FORM, **three, "action": "suggest:0"})
    assert r.text.count('name="join_table"') == 3


@pytest.mark.parametrize("tab, expected", [
    ("structure", "character varying(20)"), ("structure", "foreign key"), ("indexes", "btree"), ("indexes", "item_name_idx"),
])
def test_html_structure_and_index_tabs(client, tab, expected):
    login(client)
    r = client.get("/", params={**FORM, "tab": tab})
    assert r.status_code == 200, r.text
    assert expected in r.text


def test_html_escapes_cell_values(client):
    login(client)
    r = client.post("/saved/run", data={"key": "html"})
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


def test_result_page_carries_exact_csv_for_copying(client):
    login(client)
    r = client.post("/saved/run", data={"key": "big"})
    assert r.status_code == 200
    assert 'id="result-csv"' in r.text
    assert "9007199254740993,&#34;a,b&#34;" in r.text
