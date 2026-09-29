"""Turn database values into values that are safe for JSON, HTML and CSV."""
import csv
import io
import json
import math


def to_csv(columns: list[str], rows: list[list]) -> str:
    # Built here, not in the browser: JavaScript would round integers above 2**53.
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return out.getvalue()


def format_cell(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)  # Decimal, datetime, date, timedelta, UUID, inet, ranges ...
