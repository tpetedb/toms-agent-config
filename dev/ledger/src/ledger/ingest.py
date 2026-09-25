"""Store an expenses CSV in DuckDB, once per file.

The load is keyed on the file's sha256: ingesting the same bytes again stores
nothing, so a retried run never doubles a month. A file that drifts from the
schema is refused whole, before any row is written.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import duckdb

from ledger.schema import Schema, drift, load_schema

TABLE = """
create table if not exists expenses (
    date date not null,
    category varchar not null,
    description varchar not null,
    amount_eur decimal(12, 2) not null,
    source_sha256 varchar not null
)
"""
LOADS = """
create table if not exists loads (
    source_sha256 varchar primary key,
    file_name varchar not null,
    row_count integer not null,
    loaded_at timestamp not null default current_timestamp
)
"""


class Drift(Exception):
    """The file does not match the declared schema."""

    def __init__(self, differences: list[str]) -> None:
        super().__init__("; ".join(differences))
        self.differences = differences


@dataclass(frozen=True, slots=True)
class Loaded:
    sha256: str
    rows: int
    already: bool


def read_csv(data: bytes) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(data.decode("utf-8")))
    rows = [row for row in reader if row]
    if not rows:
        raise Drift(["the file is empty; expected a header line"])
    return rows[0], rows[1:]


def connect(db: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db))
    con.execute(TABLE)
    con.execute(LOADS)
    return con


def ingest(source: Path, db: Path, schema: Schema | None = None) -> Loaded:
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    header, rows = read_csv(data)
    differences = drift(header, rows, schema or load_schema())
    if differences:
        raise Drift(differences)
    con = connect(db)
    try:
        seen = con.execute(
            "select row_count from loads where source_sha256 = ?", [digest]
        ).fetchone()
        if seen is not None:
            return Loaded(digest, int(seen[0]), already=True)
        con.execute("begin transaction")
        con.executemany(
            "insert into expenses values (?, ?, ?, ?, ?)",
            [
                [date, category, description, Decimal(amount), digest]
                for date, category, description, amount in rows
            ],
        )
        con.execute(
            "insert into loads (source_sha256, file_name, row_count) values (?, ?, ?)",
            [digest, source.name, len(rows)],
        )
        con.execute("commit")
    finally:
        con.close()
    return Loaded(digest, len(rows), already=False)
