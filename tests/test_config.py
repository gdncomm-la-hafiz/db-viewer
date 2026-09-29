import copy

import pytest

from config import ConfigError, Limits, parse_config

HASH = "$2b$12$abcdefghijklmnopqrstuuMWbQy6V5dQ3fQ0W8p1eJ8m2oQvbZ1y."

VALID = {
    "limits": {"max_rows": 500, "default_rows": 50, "statement_timeout_ms": 15000},
    "instances": {
        "fbb_core": {
            "title": "DEV - FAAS - FBB Core", "host": "db.example", "port": 5432,
            "user": "blibli_fulfillment", "password_env": "FBB_CORE_PW",
            "databases": ["blibli_fulfillment"],
        },
    },
    "users": {"junior": {"password_hash": HASH}},
    "saved_queries": {
        "str_with_sto": {
            "title": "STR with its STO", "instance": "fbb_core", "database": "blibli_fulfillment",
            "params": ["document_number"],
            "sql": "SELECT r.id FROM stock_transport_returns r WHERE r.document_number = %(document_number)s\n",
        },
    },
}


def with_saved_sql(sql, params=()):
    raw = copy.deepcopy(VALID)
    raw["saved_queries"]["str_with_sto"]["sql"] = sql
    raw["saved_queries"]["str_with_sto"]["params"] = list(params)
    return raw


def test_valid_config_parses():
    config = parse_config(VALID)
    assert config.limits == Limits(500, 50, 15000)
    inst = config.instances["fbb_core"]
    assert (inst.port, inst.databases) == (5432, ("blibli_fulfillment",))
    assert config.users == {"junior": HASH}
    saved = config.saved_queries["str_with_sto"]
    assert saved.params == ("document_number",)
    assert not saved.sql.endswith("\n")


def test_environment_badge_is_optional():
    assert parse_config(VALID).instances["fbb_core"].environment == ""
    raw = copy.deepcopy(VALID)
    raw["instances"]["fbb_core"]["environment"] = "QA2"
    assert parse_config(raw).instances["fbb_core"].environment == "QA2"


def test_limits_default_when_missing():
    raw = copy.deepcopy(VALID)
    del raw["limits"]
    assert parse_config(raw).limits == Limits()


def test_unknown_limit_key_is_rejected():
    raw = copy.deepcopy(VALID)
    raw["limits"] = {"statement_timeout": "15s"}
    with pytest.raises(ConfigError, match="limits"):
        parse_config(raw)


def test_instance_missing_field_is_rejected():
    raw = copy.deepcopy(VALID)
    del raw["instances"]["fbb_core"]["password_env"]
    with pytest.raises(ConfigError, match="fbb_core"):
        parse_config(raw)


def test_instance_key_with_slash_is_rejected():
    raw = copy.deepcopy(VALID)
    raw["instances"]["a/b"] = raw["instances"].pop("fbb_core")
    raw["saved_queries"] = {}
    with pytest.raises(ConfigError, match="a/b"):
        parse_config(raw)


def test_plain_text_password_is_rejected():
    raw = copy.deepcopy(VALID)
    raw["users"]["junior"]["password_hash"] = "hunter2"
    with pytest.raises(ConfigError, match="bcrypt"):
        parse_config(raw)


@pytest.mark.parametrize("sql", [
    "SELECT 1; DELETE FROM x",
    "SET default_transaction_read_only = off",
    "DELETE FROM x",
    "COMMIT",
])
def test_saved_query_must_be_one_read_statement(sql):
    with pytest.raises(ConfigError, match="str_with_sto"):
        parse_config(with_saved_sql(sql))


def test_saved_query_with_cte_is_allowed():
    config = parse_config(with_saved_sql("WITH x AS (SELECT 1 AS a) SELECT a FROM x"))
    assert config.saved_queries["str_with_sto"].sql.startswith("WITH")


def test_saved_query_params_must_match_placeholders():
    with pytest.raises(ConfigError, match="placeholders"):
        parse_config(with_saved_sql("SELECT %(a)s, %(b)s", params=["a"]))


def test_saved_query_unknown_database_is_rejected():
    raw = copy.deepcopy(VALID)
    raw["saved_queries"]["str_with_sto"]["database"] = "other"
    with pytest.raises(ConfigError, match="unknown instance/database"):
        parse_config(raw)
