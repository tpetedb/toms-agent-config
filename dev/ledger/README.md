# ledger

A small command line tool that stores a CSV of expenses in DuckDB and prints a monthly report. It lives in `dev/` as the proving ground of toms-agent-config: a real tool, built to every standard the harness enforces, that nothing else in the repository depends on.

## Quickstart

From the repository root, with [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/) installed:

```sh
uv sync --frozen --project dev/ledger
uv run --frozen --project dev/ledger ledger ingest dev/ledger/fixtures/expenses-2026-08.csv --db /tmp/ledger.duckdb
uv run --frozen --project dev/ledger ledger report --db /tmp/ledger.duckdb --month 2026-08
```

The report prints the month, the total and the total per category, largest first:

```text
month 2026-08
total 1486.09 EUR
  rent             950.00
  groceries        203.20
  ...
```

## Usage

| Command | What it does |
|---|---|
| `ledger ingest <csv> --db <file>` | Checks the header and every value against [`schema.json`](schema.json), then stores the rows once. The same bytes ingested again add nothing. A file that drifts from the schema is refused whole with exit 3 and each differing column named. |
| `ledger report --db <file> --month YYYY-MM` | Prints the month's total and the total per category. |
| `ledger --version` | Prints the version. |

The CSV has four columns, in this order: `date` (ISO 8601), `category`, `description` and `amount_eur` (a decimal number).

## Checks

Run from the repository root:

| Command | What it proves |
|---|---|
| `just --justfile dev/ledger/justfile --working-directory dev/ledger verify` | ruff check, ruff format and the tests pass. |
| `just --justfile dev/ledger/justfile --working-directory dev/ledger gate-schema-drift` | The good fixture loads and the drifted one exits 3. |
| `just --justfile dev/ledger/justfile --working-directory dev/ledger gate-cli-smoke` | The installed command prints its version and its help. |

`just dev-proof` at the root runs the first two as part of acceptance 8.

## Structure

| Path | Holds |
|---|---|
| `src/ledger/schema.py` | Loads `schema.json` and names the drift of a file from it. |
| `src/ledger/ingest.py` | The idempotent load into DuckDB, keyed on the file's sha256. |
| `src/ledger/report.py` | The monthly totals. |
| `src/ledger/cli.py` | The `ledger` command. |
| `fixtures/` | An invented month of expenses, and a drifted copy with a renamed column and a word where a number belongs. |
| `docs/adr/` | Why DuckDB is the store. |
| `docs/diagrams/ingest.mmd` | The ingest path as a flowchart. |
| `CHANGELOG.md` | Every release, in Keep a Changelog form. |

## Licence

MIT, as the rest of the repository ([LICENSE](../../LICENSE)).
