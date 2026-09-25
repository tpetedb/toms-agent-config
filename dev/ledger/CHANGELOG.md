# Changelog

All notable changes to ledger are recorded in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-25

### Added

- `ledger ingest <csv> --db <file>` stores a CSV of expenses in DuckDB once per file, keyed on its sha256.
- The schema drift gate: a file whose header or values differ from `schema.json` is refused whole with exit 3, each differing column named.
- `ledger report --db <file> --month YYYY-MM` prints the month's total and the total per category.
- ADR 0001, DuckDB as the store, and the ingest diagram.

[Unreleased]: https://github.com/tpetedb/toms-agent-config/tree/main/dev/ledger
[0.1.0]: https://github.com/tpetedb/toms-agent-config/tree/main/dev/ledger
