"""`tac session`: the session journal of design section 6, run by the launcher.

One JSON-lines file per session under the worker store,
`sessions/<UTC date>/<session id>.jsonl`, one event per line, and a per-day
`_index.json` that rolls each session of that day into one line. Every event
carries the four token fields, null where the client did not say. Each event is
scanned for secrets before it is written, and refused by pattern name, never
echoed. Nothing here deletes: `purge` is the one audited way a session leaves
the journal, moving the file under `sessions/_purged/` and appending who, when,
why and the sha256 of what moved to `sessions/_purges.jsonl`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from tac import secrets_scan
from tac.draft07 import draft07
from tac.memory import append_line, locked, utc_text
from tac.work import Bad

SESSIONS_DIR = "sessions"
PURGED_DIR = "_purged"
PURGES_FILE = "_purges.jsonl"
DAY_INDEX = "_index.json"
SESSION_CONTRACT = "contracts/session-event.schema.json"
SESSION_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
DAY = r"^\d{4}-\d{2}-\d{2}$"
UTC_SECONDS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Tokens(_Model):
    """All four fields are always written; null where the client did not say."""

    input: int | None = Field(default=None, ge=0)
    output: int | None = Field(default=None, ge=0)
    cache_read: int | None = Field(default=None, ge=0)
    cache_write: int | None = Field(default=None, ge=0)


class SessionEvent(_Model):
    """One line of a session's journal. Not model-facing: the launcher writes it,
    so it keeps defaults."""

    schema_version: Literal[1] = 1
    ts: str = Field(pattern=UTC_SECONDS)
    session_id: str = Field(pattern=SESSION_ID)
    provider: str | None = None
    harness: str | None = None
    model: str | None = None
    effort: str | None = None
    role: str | None = None
    event: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
    tool: str | None = None
    topic: tuple[str, ...] = ()
    cost_usd: float | None = Field(default=None, ge=0)
    tokens: Tokens = Tokens()

    @property
    def day(self) -> str:
        return self.ts[:10]

    def line(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
        )


def session_json_schema() -> dict[str, JsonValue]:
    return draft07(SessionEvent)


def parse_event(data: object) -> SessionEvent:
    try:
        return SessionEvent.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "event"
        raise Bad(f"the session event is refused at {where}: {first['msg']}") from None


def sessions_dir(worker_store: Path) -> Path:
    return worker_store / SESSIONS_DIR


def log(worker_store: Path, event: SessionEvent) -> Path:
    """Append the event to its session's file and refresh that day's index."""
    hit = secrets_scan.names(event.line())
    if hit:
        raise Bad(
            f"the session event is refused: the secrets scan found {', '.join(hit)}; "
            "the value is not repeated here"
        )
    day = sessions_dir(worker_store) / event.day
    path = day / f"{event.session_id}.jsonl"
    with locked(day):
        append_line(path, event.line())
        write_day_index(day)
    return path


def _read(path: Path) -> list[SessionEvent]:
    events = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(SessionEvent.model_validate_json(raw))
        except ValidationError:
            # A torn line is left where it is and counted out of the roll-up;
            # the journal is append-only, so nothing rewrites it.
            continue
    return events


def _sum(values: list[float | int | None]) -> float | int | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def day_index(day: Path) -> dict[str, object]:
    """One entry per session of the day: first and last ts, the event count,
    the cost and token sums (null where no event gave a value)."""
    sessions = []
    for path in sorted(day.glob("*.jsonl")):
        events = _read(path)
        if not events:
            continue
        stamps = sorted(e.ts for e in events)
        sessions.append(
            {
                "session_id": path.name.removesuffix(".jsonl"),
                "first_ts": stamps[0],
                "last_ts": stamps[-1],
                "events": len(events),
                "cost_usd": _sum([e.cost_usd for e in events]),
                "tokens": {
                    name: _sum([getattr(e.tokens, name) for e in events])
                    for name in TOKEN_FIELDS
                },
            }
        )
    return {"schema_version": 1, "date": day.name, "sessions": sessions}


def day_index_text(index: dict[str, object]) -> str:
    """The index as JSON with each session on one line, so a day reads as a list."""
    sessions = index["sessions"]
    assert isinstance(sessions, list)
    rows = ",\n".join(
        "    " + json.dumps(s, sort_keys=True, ensure_ascii=False) for s in sessions
    )
    body = f"[\n{rows}\n  ]" if rows else "[]"
    return (
        "{\n"
        f'  "date": {json.dumps(index["date"])},\n'
        f'  "schema_version": {index["schema_version"]},\n'
        f'  "sessions": {body}\n'
        "}\n"
    )


def write_day_index(day: Path) -> Path:
    path = day / DAY_INDEX
    text = day_index_text(day_index(day))
    tmp = path.with_name(f".{DAY_INDEX}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


@dataclass(frozen=True, slots=True)
class Purge:
    session_id: str
    moved: tuple[Path, ...]
    entries: tuple[dict[str, str], ...]


def purge(
    worker_store: Path, session_id: str, *, reason: str, by: str, now: dt.datetime
) -> Purge:
    """Move every file of the session under `_purged/` and append one audit line
    per file: who, when, why and the sha256 of what moved. Nothing is deleted."""
    if not reason.strip():
        raise Bad("a purge needs a --reason: the audit line says why")
    hit = secrets_scan.names(reason)
    if hit:
        raise Bad(f"the reason is refused: the secrets scan found {', '.join(hit)}")
    root = sessions_dir(worker_store)
    found = sorted(
        p
        for p in root.glob(f"*/{session_id}.jsonl")
        if p.parent.name != PURGED_DIR and p.parent.name[:1] != "_"
    )
    if not found:
        raise Bad(f"no session {session_id} in the journal")
    moved, entries = [], []
    with locked(root):
        for path in found:
            data = path.read_bytes()
            target_dir = root / PURGED_DIR / path.parent.name
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            n = 1
            while target.exists():
                n += 1
                target = target_dir / f"{session_id}.{n}.jsonl"
            os.replace(path, target)
            entry = {
                "at": utc_text(now),
                "by": by,
                "date": path.parent.name,
                "reason": reason,
                "session_id": session_id,
                "sha256": hashlib.sha256(data).hexdigest(),
                "to": target.relative_to(root).as_posix(),
            }
            append_line(root / PURGES_FILE, json.dumps(entry, sort_keys=True))
            write_day_index(path.parent)
            moved.append(target)
            entries.append(entry)
    return Purge(session_id, tuple(moved), tuple(entries))
