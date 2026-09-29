import json
from datetime import datetime, timezone

import pytest

from audit import AuditLog
from auth import check_login, hash_password


@pytest.fixture(scope="module")
def users():
    return {"junior": hash_password("correct-horse-1")}


def test_right_password_logs_in(users):
    assert check_login(users, "junior", "correct-horse-1")


def test_wrong_password_is_refused(users):
    assert not check_login(users, "junior", "correct-horse-2")


def test_unknown_user_is_refused(users):
    assert not check_login(users, "nobody", "correct-horse-1")


def test_overlong_password_is_refused_not_an_error(users):
    assert not check_login(users, "junior", "x" * 200)


def test_hashing_an_overlong_password_is_an_error():
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password("x" * 73)


def test_audit_writes_one_json_line_per_event(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    log.write("login", user="junior")
    log.write("query", user="junior", rows=2, at=datetime(2026, 9, 29, tzinfo=timezone.utc))
    lines = [json.loads(line) for line in (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()]
    assert [line["event"] for line in lines] == ["login", "query"]
    assert lines[1]["rows"] == 2
    assert lines[1]["at"].startswith("2026-09-29")
    assert lines[0]["ts"].endswith("+00:00")
