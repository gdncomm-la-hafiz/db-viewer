import datetime
import decimal
import uuid

from cells import format_cell, to_csv


def test_csv_is_built_exactly_on_the_server():
    rows = [[9007199254740993, "a,b"], [None, 'say "hi"']]
    assert to_csv(["id", "v"], rows) == 'id,v\n9007199254740993,"a,b"\n,"say ""hi"""\n'


def test_json_native_values_pass_through():
    assert [format_cell(v) for v in (None, True, 7, "x", 1.5)] == [None, True, 7, "x", 1.5]


def test_other_values_become_strings():
    assert format_cell(decimal.Decimal("27500.50")) == "27500.50"
    assert format_cell(datetime.datetime(2026, 9, 29, 7, 0)) == "2026-09-29 07:00:00"
    assert format_cell(uuid.UUID(int=1)) == "00000000-0000-0000-0000-000000000001"
    assert format_cell(float("nan")) == "nan"


def test_bytes_become_hex():
    assert format_cell(b"\x01\xff") == "\\x01ff"


def test_json_values_become_json_text():
    assert format_cell({"color": "red"}) == '{"color": "red"}'
