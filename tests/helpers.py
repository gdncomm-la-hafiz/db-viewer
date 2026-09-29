# Integration tests run against their own schema in local-postgres's `postgres`
# database, created and dropped by tests/conftest.py — never against app tables.
TEST_DATABASE = "postgres"
TEST_SCHEMA = "dbviewer_test"
SCHEMA_MARKER = "db-viewer integration tests; safe to drop"

LOCAL_INSTANCE = {
    "title": "Local Postgres", "host": "postgres", "port": 5432, "user": "postgres",
    "password_env": "LOCAL_PG_PW", "databases": [TEST_DATABASE],
}

SEED_SQL = f"""
CREATE SCHEMA {TEST_SCHEMA};
COMMENT ON SCHEMA {TEST_SCHEMA} IS '{SCHEMA_MARKER}';
CREATE TABLE {TEST_SCHEMA}.warehouse (
    code text PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE {TEST_SCHEMA}.item (
    id bigserial PRIMARY KEY,
    code varchar(20) NOT NULL UNIQUE,
    name text,
    description text,
    price numeric(12, 2),
    is_active boolean NOT NULL DEFAULT true,
    attributes jsonb,
    warehouse_code text REFERENCES {TEST_SCHEMA}.warehouse (code),
    created_date timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz
);
CREATE INDEX item_name_idx ON {TEST_SCHEMA}.item (name);
INSERT INTO {TEST_SCHEMA}.warehouse VALUES ('MAR', 'Marunda'), ('CKG', 'Cakung');
INSERT INTO {TEST_SCHEMA}.item (code, name, description, price, is_active, attributes, warehouse_code, valid_to) VALUES
    ('ITX-001', 'Sample Item 1', 'qwerty111', 15000.00, true, '{{"color": "red"}}', 'MAR', 'infinity'),
    ('ITM-002', 'Sample Item 2', NULL, 27500.50, true, '{{"color": "blue"}}', 'CKG', NULL),
    ('ITM-003', 'Sample Item 3', NULL, 5000.00, false, NULL, 'MAR', NULL);
"""
