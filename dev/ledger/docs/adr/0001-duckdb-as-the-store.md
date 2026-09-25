# ADR 0001: DuckDB is the store

Status: Accepted, 2026-09-25.

## Context

ledger reads a CSV of expenses and answers one question about it: what was spent in a month, in total and per category. The answer has to be the same every time the same files are loaded, a file loaded twice must not double a month, and a file in the wrong shape must be refused before anything is stored. The tool runs on a laptop and in CI, as one process, with no server to install or keep running.

The options were plain CSV files summed in Python, SQLite, and DuckDB.

- Summing CSV files in Python keeps no record of what was loaded, so the idempotent load would need a bookkeeping file of its own, and every report would reread every file.
- SQLite is everywhere and needs no dependency, but has no fixed-point decimal type: amounts would be stored as floating point or as integer cents converted by hand.
- DuckDB is an embedded analytical database in one file, installed as a Python wheel, with a `decimal(12, 2)` type, transactions, and SQL aggregation of the kind the report needs.

## Decision

DuckDB is the store. `ledger ingest` writes the rows into an `expenses` table and records each file in a `loads` table under its sha256, inside one transaction, so a file is stored whole or not at all, and the same bytes are stored once. Amounts are `decimal(12, 2)` in the table and `Decimal` in Python. The schema of the input is declared in `schema.json` and checked before the database is opened.

## Consequences

- One dependency with a native wheel. `uv.lock` pins it, and the first sync needs the network.
- A database file is created wherever `--db` points; the tool never picks a location on its own.
- A change to the table layout needs a migration once a release has shipped; 0.1.0 has no data to migrate.
- DuckDB allows one writing process per file, which is all a command line tool needs.
