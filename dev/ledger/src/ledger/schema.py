"""The declared shape of an expenses CSV, and the drift from it.

schema.json is the contract: the columns in order and the type of each. A file
whose header or values differ is refused before anything is stored, so the table
never holds a row the report would misread.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[2] / "schema.json"
TYPES = ("date", "text", "decimal")


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class Schema:
    name: str
    version: int
    columns: tuple[Column, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


def load_schema(path: Path = SCHEMA) -> Schema:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError(f"{path.name}: version {data.get('version')!r} is not 1")
    columns = tuple(Column(c["name"], c["type"]) for c in data["columns"])
    unknown = [c.type for c in columns if c.type not in TYPES]
    if unknown:
        raise ValueError(f"{path.name}: unknown column types {unknown}")
    return Schema(data["name"], data["version"], columns)


def valid(kind: str, value: str) -> bool:
    """Whether a CSV cell reads as the declared type."""
    if kind == "date":
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if kind == "decimal":
        try:
            return Decimal(value).is_finite()
        except InvalidOperation:
            return False
    return value.strip() != ""


def drift(
    header: Sequence[str],
    sample: Iterable[Sequence[str]],
    schema: Schema | None = None,
) -> list[str]:
    """Every difference between a file and the schema, each naming its column.

    An empty list means the file may be stored. Values are checked only when
    the header matches, since a shifted column would name the wrong culprit.
    """
    schema = schema or load_schema()
    expected = schema.names
    found = tuple(h.strip() for h in header)
    differences = [f"missing column {n}" for n in expected if n not in found]
    differences += [f"unexpected column {n}" for n in found if n not in expected]
    if not differences and found != expected:
        differences.append(
            f"columns out of order: {', '.join(found)}; expected {', '.join(expected)}"
        )
    if differences:
        return differences
    for line, row in enumerate(sample, start=2):
        if len(row) != len(expected):
            differences.append(
                f"line {line} has {len(row)} fields, expected {len(expected)}"
            )
            continue
        for column, value in zip(schema.columns, row, strict=True):
            if not valid(column.type, value):
                differences.append(
                    f"column {column.name}: {value!r} on line {line} "
                    f"is not a {column.type}"
                )
    return differences
