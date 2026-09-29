import os

import psycopg
import pytest

from tests.helpers import LOCAL_INSTANCE, SCHEMA_MARKER, SEED_SQL, TEST_DATABASE, TEST_SCHEMA


def _connect(password):
    return psycopg.connect(
        host=LOCAL_INSTANCE["host"], port=LOCAL_INSTANCE["port"], dbname=TEST_DATABASE,
        user=LOCAL_INSTANCE["user"], password=password, autocommit=True,
    )


def _drop_test_schema(conn):
    # Only a schema carrying our marker is ever dropped, so a real schema that
    # happens to share the name is left alone (and the run stops instead).
    comment = conn.execute(
        "SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace WHERE nspname = %s", [TEST_SCHEMA],
    ).fetchone()
    if comment is None:
        return
    if comment[0] != SCHEMA_MARKER:
        pytest.exit(f"schema {TEST_SCHEMA} exists and isn't ours; refusing to touch it", returncode=2)
    conn.execute(f"DROP SCHEMA {TEST_SCHEMA} CASCADE")


@pytest.fixture(scope="session")
def local_env():
    password = os.environ.get("LOCAL_PG_PW")
    if not password:
        pytest.skip("LOCAL_PG_PW not set; run with --env-file secrets.env")
    return {"LOCAL_PG_PW": password, "SESSION_SECRET": "t" * 32}


@pytest.fixture(scope="session")
def seeded_db(local_env):
    """Create the test schema once per run (replacing a leftover from a crashed run) and drop it after."""
    with _connect(local_env["LOCAL_PG_PW"]) as conn:
        _drop_test_schema(conn)
        conn.execute(SEED_SQL)
    yield
    with _connect(local_env["LOCAL_PG_PW"]) as conn:
        _drop_test_schema(conn)


@pytest.fixture
def unchanged_db(local_env, seeded_db):
    """Fail the test if anything it did through the viewer left a trace."""
    def snapshot():
        with _connect(local_env["LOCAL_PG_PW"]) as conn:
            return (
                conn.execute("SELECT last_value FROM pg_sequences WHERE schemaname = %s AND sequencename = 'item_id_seq'",
                             [TEST_SCHEMA]).fetchone(),
                conn.execute("SELECT count(*) FROM pg_largeobject_metadata").fetchone(),
                conn.execute(f"SELECT count(*), sum(price) FROM {TEST_SCHEMA}.item").fetchone(),
            )

    before = snapshot()
    yield
    assert snapshot() == before
