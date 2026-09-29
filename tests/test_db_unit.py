import pytest

from config import parse_config
from db import Databases, DatabaseError, UnknownDatabase, join_suggestions


def fk(name, direction, table, columns, ref_table, ref_columns):
    return {"name": name, "kind": "foreign key", "definition": "", "direction": direction,
            "table": table, "columns": columns, "ref_table": ref_table, "ref_columns": ref_columns}


def test_join_suggestions_follow_single_column_foreign_keys_both_ways():
    constraints = [
        {"name": "str_pkey", "kind": "primary key", "definition": "PRIMARY KEY (id)", "direction": "out",
         "table": "public.str", "columns": ["id"], "ref_table": None, "ref_columns": None},
        fk("str_wh_fk", "out", "public.str", ["warehouse_code"], "wh.warehouse", ["code"]),
        fk("item_str_fk", "in", "public.str_item", ["str_id"], "public.str", ["id"]),
        fk("two_col_fk", "out", "public.str", ["a", "b"], "public.x", ["a", "b"]),
    ]
    assert join_suggestions(constraints) == [
        {"kind": "LEFT", "schema": "wh", "table": "warehouse", "left": "warehouse_code", "right": "code", "via": "str_wh_fk"},
        {"kind": "LEFT", "schema": "public", "table": "str_item", "left": "id", "right": "str_id", "via": "item_str_fk"},
    ]


def test_join_suggestions_skip_tables_outside_the_catalog():
    constraints = [
        fk("str_wh_fk", "out", "public.str", ["warehouse_code"], "wh.warehouse", ["code"]),
        fk("part_fk", "in", "public.str_item_p1", ["str_id"], "public.str", ["id"]),
    ]
    assert [s["table"] for s in join_suggestions(constraints, known={("wh", "warehouse")})] == ["warehouse"]


CONFIG = parse_config({"instances": {"omnichannel_integrator": {
    "title": "Omni", "host": "nowhere.invalid", "port": 5432, "user": "scm_user",
    "password_env": "OMNI_PW", "databases": ["omnichannel_integrator"],
}}})


def test_missing_password_env_names_the_variable():
    dbs = Databases(CONFIG, env={})
    with pytest.raises(DatabaseError, match="OMNI_PW"):
        dbs.pool("omnichannel_integrator", "omnichannel_integrator")


@pytest.mark.parametrize("instance, database", [("omnichannel_integrator", "other"), ("nope", "omnichannel_integrator")])
def test_unknown_database_is_rejected(instance, database):
    dbs = Databases(CONFIG, env={"OMNI_PW": "x"})
    with pytest.raises(UnknownDatabase):
        dbs.pool(instance, database)
