"""Hand-made records and events for the memory and selector tests.

Every id, time and hash is fixed here, so a test reads the same bytes on every
run; nothing reads the clock or a random source.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from typing import Any

from tac.memory import Event, Record, content_hash

REPOSITORY = "example/demo"
NOW = dt.datetime(2026, 9, 24, 14, 25, tzinfo=dt.UTC)
NOW_TEXT = "2026-09-24T14:25:00Z"


def suffixes(start: int = 0) -> Callable[[], str]:
    """A deterministic stand-in for the random id suffix: 0000, 0001, ..."""
    counter: Iterator[int] = iter(range(start, 0x10000))
    return lambda: f"{next(counter):04x}"


def rid(n: int, stamp: str = "20260924T142500Z") -> str:
    return f"mem:{stamp}:{n:04x}"


def record(n: int, **changes: Any) -> Record:
    body: dict[str, Any] = {
        "schema_version": 1,
        "id": rid(n),
        "sequence": n,
        "supersedes": None,
        "kind": "observation",
        "repository": REPOSITORY,
        "scope": "project",
        "paths": [],
        "topic": ["harness.config"],
        "statement": f"Statement number {n}.",
        "provider": None,
        "harness": None,
        "model": None,
        "effort_requested": None,
        "effort_actual": None,
        "role": None,
        "created": "2026-09-01T00:00:00Z",
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to": None,
        "source_refs": [],
        "order_id": None,
        "run_id": None,
        "commit_sha": None,
        "sensitivity": "internal",
    }
    body.update(changes)
    body["content_hash"] = content_hash(body["statement"], body["topic"], body["kind"])
    return Record.model_validate(body)


def event(n: int, kind: str, target: str, **changes: Any) -> Event:
    body: dict[str, Any] = {
        "schema_version": 1,
        "id": f"evt:20260924T142500Z:{n:04x}",
        "sequence": n,
        "at": "2026-09-02T00:00:00Z",
        "kind": kind,
        "record": target,
        "by": "test",
        "basis": None,
        "note": None,
        "supersedes_with": None,
    }
    body.update(changes)
    return Event.model_validate(body)


def review(n: int, target: str, basis: str = "reviewed") -> Event:
    return event(n, "review", target, basis=basis)
