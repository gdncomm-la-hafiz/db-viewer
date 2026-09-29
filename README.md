# DB Viewer

Read-only table browser for local and QA2 Postgres. Juniors pick a table,
columns and filters; the server builds the SELECT and runs it inside
`BEGIN READ ONLY … ROLLBACK` with a 15 s timeout. No typed SQL.

## Run

First time: `cp config.example.yaml config.yaml` and fill in instances and users (config.yaml is git-ignored).

    cd ~/local-infra && docker compose up -d --build --no-deps db-viewer

Open http://127.0.0.1:8083. After editing `config.yaml`: `docker compose restart db-viewer`.

## Add a user

    cd ~/local-infra/db-viewer && docker run --rm -it -v "$PWD":/app db-viewer-dev python hashpw.py

Paste the hash under `users:` in `config.yaml`, restart. Remove the entry to revoke access (takes effect on the next request).

## What juniors can do

- **Rows**: pick columns, filters (`= != < > <= >= LIKE ILIKE IN IS [NOT] NULL`), sort, limit (max 500), copy as CSV.
- **Joins**: up to 3 LEFT/INNER joins on any `column = column`; foreign keys show up as one-click suggestions.
- **Aggregates**: COUNT(*), COUNT, COUNT DISTINCT, SUM, AVG, MIN, MAX; the selected columns become the GROUP BY.
- **Explain**: `EXPLAIN` (never `ANALYZE`) of the built query — the plan, without running it.
- **Structure / Indexes** tabs: columns, constraints (incl. FKs from other tables), indexes with size and scan count.

Everything is built from the catalog; no SQL text from the browser is ever run.

## Add a database

Add it to an instance's `databases:` list (or a new instance with its own `password_env`), put the password in `secrets.env`, restart.
An instance's `environment:` (e.g. `Local`, `QA2`) is shown as a badge in the header.

## Saved queries

Written by the admin in `config.yaml`. Rules (checked at startup): one statement, starts with `SELECT` or `WITH`, no `;`,
placeholders `%(name)s` listed in `params`. Write a literal `%` as `%%` (e.g. `LIKE 'STR%%'`).

## Access from other laptops (LAN)

In an admin PowerShell on this laptop (the WSL IP changes after a reboot; re-run the first command then):

    netsh interface portproxy add v4tov4 listenport=8083 listenaddress=0.0.0.0 connectport=8083 connectaddress=(wsl hostname -I).Split()[0]
    New-NetFirewallRule -DisplayName "db-viewer 8083" -Direction Inbound -LocalPort 8083 -Protocol TCP -Action Allow

## Audit

`data/audit.log`: one JSON line per login and query (who, when, table, filters, rows, duration, error).

## Tests

    docker build --target dev -t db-viewer-dev .
    docker run --rm -v "$PWD":/app db-viewer-dev pytest -q
    docker run --rm --network local-infra_default --env-file secrets.env -v "$PWD":/app db-viewer-dev pytest -q -m integration

Integration tests only touch local-postgres: they create their own `dbviewer_test` schema in the `postgres` database
(dropped only if its comment marks it as theirs) and never use existing tables.

## What it doesn't protect

Juniors can read every row of every listed table. A heavy filter can use up to 15 s of database time per run.

Saved queries are trusted: juniors can't write them, but whatever you put there runs. `READ ONLY` blocks
sequence changes (`nextval`, `setval`) and all DML/DDL; transactional writes Postgres still allows inside it
(e.g. `lo_create`) are undone by the `ROLLBACK`. Some calls are neither blocked nor undone —
`pg_terminate_backend`, `pg_cancel_backend`, `pg_advisory_lock` — so never use those in a saved query.
