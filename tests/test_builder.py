import pytest

from builder import Aggregate, Filter, Join, QueryError, QueryRequest, build_select

CATALOG = {
    ("public", "sample_item"): ["id", "code", "name", "Weird Col"],
    ("public", "odd%table"): ["id"],
    ("public", "odd_cols"): ["id", "discount%"],
    ("public", "str"): ["id", "document_number", "status", "sto_reference_number"],
    ("public", "sto"): ["id", "document_number", "status"],
    ("wh", "warehouse"): ["code", "name"],
}


def render(req, default_rows=50, max_rows=500):
    query, params = build_select(CATALOG, req, default_rows, max_rows)
    return query.as_string(), params


# --- single table (v1 behaviour, now with t0 aliases) ---

def test_all_columns_and_default_limit():
    text, params = render(QueryRequest("public", "sample_item"))
    assert text == (
        'SELECT "t0"."id" AS "id", "t0"."code" AS "code", "t0"."name" AS "name", "t0"."Weird Col" AS "Weird Col"'
        ' FROM "public"."sample_item" AS "t0" LIMIT 50'
    )
    assert params == []


def test_selected_columns_filters_order():
    req = QueryRequest(
        "public", "sample_item",
        columns=("id", "code"),
        filters=(Filter("code", "=", "ITM-002"), Filter("name", "ilike", "%item%")),
        order_by="id", descending=True, limit=10,
    )
    text, params = render(req)
    assert text == (
        'SELECT "t0"."id" AS "id", "t0"."code" AS "code" FROM "public"."sample_item" AS "t0"'
        ' WHERE "t0"."code" = %s AND "t0"."name" ILIKE %s ORDER BY "id" DESC LIMIT 10'
    )
    assert params == ["ITM-002", "%item%"]


def test_injection_attempt_stays_a_parameter():
    text, params = render(QueryRequest("public", "sample_item", filters=(Filter("code", "=", "'; DROP TABLE x --"),)))
    assert text.endswith('WHERE "t0"."code" = %s LIMIT 50')
    assert params == ["'; DROP TABLE x --"]


def test_in_list_gets_one_parameter_per_item():
    text, params = render(QueryRequest("public", "sample_item", filters=(Filter("id", "IN", "1, 2,,3"),)))
    assert 'WHERE "t0"."id" IN (%s, %s, %s)' in text
    assert params == ["1", "2", "3"]


def test_in_without_values_is_rejected():
    with pytest.raises(QueryError, match="at least one value"):
        render(QueryRequest("public", "sample_item", filters=(Filter("id", "IN", " , "),)))


def test_is_null_takes_no_parameter():
    text, params = render(QueryRequest("public", "sample_item", filters=(Filter("name", "IS NOT NULL"),)))
    assert 'WHERE "t0"."name" IS NOT NULL' in text
    assert params == []


def test_not_equal_maps_to_sql_operator():
    text, _ = render(QueryRequest("public", "sample_item", filters=(Filter("id", "!=", "1"),)))
    assert 'WHERE "t0"."id" <> %s' in text


@pytest.mark.parametrize("req", [
    QueryRequest("public", "nope"),
    QueryRequest('public"; --', "sample_item"),
])
def test_unknown_table_is_rejected(req):
    with pytest.raises(QueryError, match="unknown table"):
        render(req)


@pytest.mark.parametrize("req", [
    QueryRequest("public", "sample_item", columns=("id", "password")),
    QueryRequest("public", "sample_item", filters=(Filter("nope", "=", "1"),)),
    QueryRequest("public", "sample_item", order_by="id; DELETE"),
])
def test_unknown_column_is_rejected(req):
    with pytest.raises(QueryError, match="unknown column"):
        render(req)


@pytest.mark.parametrize("op", ["; DELETE", "= 1 OR 1=1 --", "BETWEEN"])
def test_operator_outside_the_list_is_rejected(op):
    with pytest.raises(QueryError, match="unsupported operator"):
        render(QueryRequest("public", "sample_item", filters=(Filter("id", op, "1"),)))


def test_limit_is_capped():
    text, _ = render(QueryRequest("public", "sample_item", limit=9999))
    assert text.endswith("LIMIT 500")


def test_limit_below_one_is_rejected():
    with pytest.raises(QueryError, match="at least 1"):
        render(QueryRequest("public", "sample_item", limit=0))


@pytest.mark.parametrize("req", [
    QueryRequest("public", "odd%table"),
    QueryRequest("public", "odd_cols", columns=("discount%",)),
    QueryRequest("public", "odd_cols"),
    QueryRequest("public", "str", joins=(Join("LEFT", "public", "odd%table", "id", "id"),)),
])
def test_percent_in_identifier_is_rejected(req):
    with pytest.raises(QueryError, match="not supported"):
        render(req)


# --- joins ---

STR_STO = Join("LEFT", "public", "sto", "sto_reference_number", "document_number")


def test_left_join_with_table_qualified_output_names():
    req = QueryRequest("public", "str", columns=("id", "t1.status"), joins=(STR_STO,), limit=5)
    text, params = render(req)
    assert text == (
        'SELECT "t0"."id" AS "str.id", "t1"."status" AS "sto.status"'
        ' FROM "public"."str" AS "t0"'
        ' LEFT JOIN "public"."sto" AS "t1" ON "t0"."sto_reference_number" = "t1"."document_number"'
        ' LIMIT 5'
    )
    assert params == []


def test_join_defaults_to_every_column_of_every_table():
    text, _ = render(QueryRequest("public", "str", joins=(Join("INNER", "wh", "warehouse", "status", "code"),)))
    assert '"t1"."name" AS "warehouse.name"' in text
    assert ' JOIN "wh"."warehouse" AS "t1" ON "t0"."status" = "t1"."code"' in text
    assert "LEFT JOIN" not in text


def test_self_join_names_tables_apart():
    req = QueryRequest("public", "sto", columns=("id", "t1.id"), joins=(Join("INNER", "public", "sto", "id", "id"),))
    text, _ = render(req)
    assert '"t0"."id" AS "sto.id", "t1"."id" AS "sto#1.id"' in text


def test_second_join_can_hang_off_the_first():
    joins = (STR_STO, Join("LEFT", "wh", "warehouse", "t1.status", "code"))
    text, _ = render(QueryRequest("public", "str", columns=("id",), joins=joins))
    assert 'LEFT JOIN "wh"."warehouse" AS "t2" ON "t1"."status" = "t2"."code"' in text


def test_filters_and_sort_can_use_joined_columns():
    req = QueryRequest("public", "str", columns=("id", "t1.status"), joins=(STR_STO,),
                       filters=(Filter("t1.status", "=", "DRAFT"),), order_by="t1.id", descending=True)
    text, params = render(req)
    assert 'WHERE "t1"."status" = %s ORDER BY "t1"."id" DESC' in text
    assert params == ["DRAFT"]


@pytest.mark.parametrize("join, message", [
    (Join("LEFT", "public", "nope", "id", "id"), "unknown table"),
    (Join("LEFT", "public", "sto", "id", "nope"), "unknown column"),
    (Join("LEFT", "public", "sto", "nope", "id"), "unknown column"),
    (Join("LEFT", "public", "sto", "t1.id", "id"), "not joined yet"),
    (Join("CROSS", "public", "sto", "id", "id"), "unsupported join"),
])
def test_bad_join_is_rejected(join, message):
    with pytest.raises(QueryError, match=message):
        render(QueryRequest("public", "str", joins=(join,)))


def test_reference_to_a_missing_alias_is_rejected():
    with pytest.raises(QueryError, match="not joined yet"):
        render(QueryRequest("public", "str", columns=("t2.id",), joins=(STR_STO,)))


def test_more_than_three_joins_is_rejected():
    joins = tuple(Join("LEFT", "public", "sto", "id", "id") for _ in range(4))
    with pytest.raises(QueryError, match="at most 3 joins"):
        render(QueryRequest("public", "str", joins=joins))


# --- aggregates ---

def test_count_per_value_groups_by_the_selected_columns():
    req = QueryRequest("public", "str", columns=("status",), aggregates=(Aggregate("COUNT(*)"),),
                       order_by="count", descending=True)
    text, params = render(req)
    assert text == (
        'SELECT "t0"."status" AS "status", count(*) AS "count" FROM "public"."str" AS "t0"'
        ' GROUP BY "t0"."status" ORDER BY "count" DESC LIMIT 50'
    )
    assert params == []


def test_aggregate_without_columns_covers_all_rows():
    text, _ = render(QueryRequest("public", "str", aggregates=(Aggregate("count(*)"),)))
    assert text == 'SELECT count(*) AS "count" FROM "public"."str" AS "t0" LIMIT 50'


def test_every_aggregate_function_renders():
    aggs = tuple(Aggregate(f, "id") for f in ("COUNT", "COUNT DISTINCT", "SUM", "AVG", "MIN", "MAX"))
    text, _ = render(QueryRequest("public", "str", aggregates=aggs))
    assert text == (
        'SELECT count("t0"."id") AS "count(id)", count(DISTINCT "t0"."id") AS "count_distinct(id)",'
        ' sum("t0"."id") AS "sum(id)", avg("t0"."id") AS "avg(id)", min("t0"."id") AS "min(id)",'
        ' max("t0"."id") AS "max(id)" FROM "public"."str" AS "t0" LIMIT 50'
    )


def test_aggregate_over_a_joined_column():
    req = QueryRequest("public", "str", columns=("status",), joins=(STR_STO,),
                       aggregates=(Aggregate("COUNT DISTINCT", "t1.id"),))
    text, _ = render(req)
    assert 'count(DISTINCT "t1"."id") AS "count_distinct(sto.id)"' in text
    assert 'GROUP BY "t0"."status"' in text


@pytest.mark.parametrize("agg, message", [
    (Aggregate("MEDIAN", "id"), "unsupported aggregate"),
    (Aggregate("pg_terminate_backend", "id"), "unsupported aggregate"),
    (Aggregate("SUM"), "needs a column"),
    (Aggregate("COUNT(*)", "id"), "takes no column"),
    (Aggregate("SUM", "nope"), "unknown column"),
])
def test_bad_aggregate_is_rejected(agg, message):
    with pytest.raises(QueryError, match=message):
        render(QueryRequest("public", "str", aggregates=(agg,)))


def test_duplicate_output_names_are_rejected():
    with pytest.raises(QueryError, match="twice"):
        render(QueryRequest("public", "str", aggregates=(Aggregate("COUNT(*)"), Aggregate("COUNT(*)"))))
