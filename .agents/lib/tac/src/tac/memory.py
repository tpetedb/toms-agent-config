"""`tac memory`: the append-only memory bus of design section 6.

Two stores hold the same two shapes. The live journal sits in the worker store
(`<worker store>/memory/records.jsonl`, `events.jsonl`, `quarantine.jsonl`),
which every worktree of a checkout shares and agents may write. The promoted
store sits in the checkout (`.agents/memory/records/<id>.json`,
`events/<UTC date>.jsonl`, `_index/`) and reaches main only through a pull
request. Nothing in either is ever rewritten:

- a record is written once; supersession, review, decay, correction and
  conflict are events that name it, and `status` and `confidence` are derived
  from those events every time they are read, never stored on the record;
- every write opens its file with O_APPEND, writes one JSON line and fsyncs,
  and takes `sequence` as the last one plus one under an exclusive lock, so a
  gap or a repeat means something wrote around this module;
- a line that does not parse is copied to quarantine.jsonl with the reason and
  the raw text by the next writer, counted and reported, never dropped silently;
  an unknown schema_version is refused, since a versioned format fails loudly.

Provider, harness, model, effort and role are written by the tool from the
launch record, never typed into a payload. `basis` is never taken from input:
`promote` recomputes it from evidence it verifies itself (a signed receipt, the
order's review gate, a host-signed approval), and only a record whose basis is
test-receipt, reviewed or owner, whose derived status is confirmed and in which
the secrets scan finds nothing is copied into the checkout.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import secrets
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from tac import secrets_scan
from tac.draft07 import draft07
from tac.receipts import ID_PATTERN, ORDER_PATTERN, SHA_PATTERN
from tac.work import Bad

SCHEMA_VERSION = 1
SELECTOR_VERSION = 1

# The caps a record is held to; a record over any of them is refused on write
# and named by lint, so no stage prompt is flooded by one record.
MAX_STATEMENT = 2000
MAX_TOPICS = 8
MAX_PATHS = 32
MAX_SOURCE_REFS = 16

PROMOTED_DIR = ".agents/memory"
RECORDS_DIR = "records"
EVENTS_DIR = "events"
INDEX_DIR = "_index"
INDEX_FILES = ("by-topic.json", "by-order.json", "by-status.json")
INDEX_README = "README.md"
# Under the worker store.
LIVE_DIR = "memory"
RECORDS_FILE = "records.jsonl"
EVENTS_FILE = "events.jsonl"
QUARANTINE_FILE = "quarantine.jsonl"
RECORD_CONTRACT = "contracts/memory-record.schema.json"

RECORD_ID = r"^mem:\d{8}T\d{6}Z:[0-9a-f]{4}$"
EVENT_ID = r"^evt:\d{8}T\d{6}Z:[0-9a-f]{4}$"
UTC_SECONDS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
REPOSITORY = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
CONTENT_HASH = r"^sha256:[0-9a-f]{64}$"
TOPIC = r"^[a-z0-9][a-z0-9._-]{0,63}$"
# A topic that marks a record every stage it reaches must receive whole: the
# selector refuses rather than truncate one.
MANDATORY_TOPIC = "policy"

Kind = Literal[
    "observation",
    "decision",
    "lesson",
    "question",
    "contract",
    "component",
    "session-digest",
    "trace-digest",
]
Scope = Literal["project", "team", "order", "session"]
Sensitivity = Literal["public", "internal", "secret"]
Basis = Literal["test-receipt", "reviewed", "single-source", "inferred", "owner"]
EventKind = Literal["supersede", "review", "decay", "correct", "conflict"]
Status = Literal["confirmed", "unconfirmed", "conflict", "superseded"]
Level = Literal["high", "medium", "low"]

STATUSES: tuple[Status, ...] = ("confirmed", "conflict", "superseded", "unconfirmed")
# The bases promotion accepts; a test receipt outranks repetition.
STRONG: frozenset[str] = frozenset({"test-receipt", "reviewed", "owner"})
BASIS_RANK: dict[str, int] = {
    "test-receipt": 0,
    "reviewed": 1,
    "owner": 2,
    "single-source": 3,
    "inferred": 4,
}
LEVEL_OF: dict[str, Level] = {
    "test-receipt": "high",
    "owner": "high",
    "reviewed": "medium",
    "single-source": "low",
    "inferred": "low",
}
LOWER: dict[Level, Level] = {"high": "medium", "medium": "low", "low": "low"}
# Events that close a record: its valid_to becomes the event's time.
CLOSING: frozenset[str] = frozenset({"supersede", "correct"})
# Events that pair a record with another one.
PAIRED: frozenset[str] = frozenset({"supersede", "correct", "conflict"})

# Fields the launcher writes from its launch record; a payload that types one
# is refused, never silently overwritten.
TOOL_WRITTEN = (
    "provider",
    "harness",
    "model",
    "effort_requested",
    "effort_actual",
    "role",
)
# Fields this module computes.
COMPUTED = ("id", "sequence", "created", "content_hash")
# Derived from events on every read; never stored.
DERIVED = ("status", "confidence")
# Recomputed from verified evidence by promote; whatever input says is dropped.
IGNORED = ("basis",)

Topic = Annotated[str, Field(pattern=TOPIC)]


def _identity(line: dict[str, Any]) -> dict[str, Any]:
    return line


# schema_version to the function that brings a line of that version to the
# current one. A later version adds its upgrade here; a version missing from it
# is refused with a message, never guessed at.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {1: _identity}


# ---------------------------------------------------------------- time and ids


def utc_text(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        raise Bad(f"{text!r} is not a UTC time like 2026-09-24T14:25:00Z") from None


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def random_suffix() -> str:
    return secrets.token_hex(2)


def new_id(prefix: str, now: dt.datetime, suffix: str) -> str:
    """`<prefix>:<UTC basic datetime>:<4 hex>`."""
    return f"{prefix}:{now.astimezone(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}:{suffix}"


def content_hash(statement: str, topic: Iterable[str], kind: str) -> str:
    """sha256 of the canonical statement, topic set and kind: the same claim
    written with other spacing or topics in another order hashes the same."""
    body = json.dumps(
        {
            "kind": kind,
            "statement": " ".join(statement.split()),
            "topic": sorted(set(topic)),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- models


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def line(self) -> str:
        """One JSON line, keys sorted, as every store writes it."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
        )


class Record(_Model):
    """One memory record, shaped for the strict subset: every property present,
    nulls explicit. Its caps sit in the validator, not in the schema, since the
    strict subset has no string length keyword."""

    schema_version: Literal[1]
    id: str = Field(pattern=RECORD_ID)
    sequence: int = Field(ge=1)
    supersedes: str | None = Field(pattern=RECORD_ID)
    kind: Kind
    repository: str = Field(pattern=REPOSITORY)
    scope: Scope
    paths: tuple[str, ...] = Field(max_length=MAX_PATHS)
    topic: tuple[Topic, ...] = Field(max_length=MAX_TOPICS)
    statement: str
    provider: str | None
    harness: str | None
    model: str | None
    effort_requested: str | None
    effort_actual: str | None
    role: str | None
    created: str = Field(pattern=UTC_SECONDS)
    valid_from: str = Field(pattern=UTC_SECONDS)
    valid_to: str | None = Field(pattern=UTC_SECONDS)
    source_refs: tuple[str, ...] = Field(max_length=MAX_SOURCE_REFS)
    order_id: str | None = Field(pattern=ORDER_PATTERN)
    run_id: str | None = Field(pattern=ID_PATTERN)
    commit_sha: str | None = Field(pattern=SHA_PATTERN)
    sensitivity: Sensitivity
    content_hash: str = Field(pattern=CONTENT_HASH)

    @model_validator(mode="after")
    def _sound(self) -> Self:
        if not self.statement.strip():
            raise ValueError("statement is empty")
        if len(self.statement) > MAX_STATEMENT:
            raise ValueError(
                f"statement holds {len(self.statement)} characters, over the cap "
                f"of {MAX_STATEMENT}"
            )
        for path in self.paths:
            parts = path.split("/")
            if not path or path.startswith("/") or ".." in parts:
                raise ValueError(f"path {path!r} is not relative to the checkout")
        if self.supersedes == self.id:
            raise ValueError("a record cannot supersede itself")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to is not after valid_from")
        want = content_hash(self.statement, self.topic, self.kind)
        if self.content_hash != want:
            raise ValueError(
                "content_hash does not match the statement, topic and kind"
            )
        return self

    @property
    def mandatory(self) -> bool:
        return MANDATORY_TOPIC in self.topic


class Event(_Model):
    """Something that happened to a record. The only way a record changes."""

    schema_version: Literal[1]
    id: str = Field(pattern=EVENT_ID)
    sequence: int = Field(ge=1)
    at: str = Field(pattern=UTC_SECONDS)
    kind: EventKind
    record: str = Field(pattern=RECORD_ID)
    by: str | None
    basis: Basis | None
    note: str | None
    # supersede and correct: the record that replaces it; conflict: the other side.
    supersedes_with: str | None = Field(pattern=RECORD_ID)

    @model_validator(mode="after")
    def _sound(self) -> Self:
        if self.kind in PAIRED:
            if self.supersedes_with is None:
                raise ValueError(f"a {self.kind} event names the other record")
            if self.supersedes_with == self.record:
                raise ValueError(f"a {self.kind} event pairs a record with itself")
        elif self.supersedes_with is not None:
            raise ValueError(f"a {self.kind} event names no other record")
        if self.basis is not None and self.kind != "review":
            raise ValueError("only a review event carries a basis")
        return self


class Launch(_Model):
    """What the launcher wrote when it started the session: the only source of
    the tool-written fields."""

    provider: str | None
    harness: str | None
    model: str | None
    effort_requested: str | None
    effort_actual: str | None
    role: str | None


def read_launch(path: Path) -> Launch:
    try:
        return Launch.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise Bad(f"{path}: cannot read the launch record: {e.strerror}") from None
    except ValidationError as e:
        raise Bad(f"{path}: not a launch record: {_first(e)}") from None


def _first(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(p) for p in first["loc"])
    return f"{where}: {first['msg']}" if where else str(first["msg"])


# ---------------------------------------------------------------- the contract


def _nullable_as_type_list(schema: Any) -> Any:
    """`anyOf: [X, {type: null}]` as `X` with `type: [t, "null"]`, the form the
    strict subset names; anything else is left as it is."""
    if isinstance(schema, list):
        return [_nullable_as_type_list(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _nullable_as_type_list(v) for k, v in schema.items()}
    options = out.get("anyOf")
    if isinstance(options, list) and len(options) == 2:
        rest = [o for o in options if o != {"type": "null"}]
        if len(rest) == 1 and isinstance(rest[0].get("type"), str):
            merged = {k: v for k, v in out.items() if k != "anyOf"}
            merged.update(rest[0])
            merged["type"] = [rest[0]["type"], "null"]
            return merged
    return out


def record_json_schema() -> dict[str, JsonValue]:
    """The record as a model-facing contract: draft-07 in the strict subset."""
    return _nullable_as_type_list(draft07(Record))


def schema_text(schema: object) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------- reading


@dataclass(frozen=True, slots=True)
class Torn:
    """A line that did not parse: where, why, and the raw text."""

    file: str
    line: int
    reason: str
    raw: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Lines[M: BaseModel]:
    items: list[M]
    torn: list[Torn]


def _migrate(data: object, where: str) -> object:
    if not isinstance(data, dict):
        return data
    version = data.get("schema_version")
    if isinstance(version, int) and not isinstance(version, bool):
        upgrade = MIGRATIONS.get(version)
        if upgrade is None:
            known = ", ".join(str(v) for v in sorted(MIGRATIONS))
            raise Bad(
                f"{where}: schema_version {version} is unknown; this tac reads "
                f"{known}. Upgrade tac before reading this store"
            )
        return upgrade(data)
    return data


def parse_line[M: BaseModel](model: type[M], raw: str, where: str) -> M | str:
    """The line as `model`, or the reason it is not one. An unknown
    schema_version raises instead: that is refused, not quarantined."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return f"not JSON: {e.msg}"
    data = _migrate(data, where)
    try:
        return model.model_validate(data)
    except ValidationError as e:
        return _first(e)


def read_lines[M: BaseModel](model: type[M], path: Path, name: str) -> Lines[M]:
    """Every line of a JSON-lines file; `name` is how findings spell the file."""
    items: list[M] = []
    torn: list[Torn] = []
    if not path.is_file():
        return Lines(items, torn)
    text = path.read_text(encoding="utf-8", errors="replace")
    for number, raw in enumerate(text.split("\n"), start=1):
        if not raw.strip():
            continue
        parsed = parse_line(model, raw, f"{name}:{number}")
        if isinstance(parsed, str):
            torn.append(Torn(name, number, parsed, raw))
        else:
            items.append(parsed)
    return Lines(items, torn)


@dataclass(frozen=True, slots=True)
class Store:
    """One store's records and events as read, with what did not parse."""

    records: list[Record] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    torn: list[Torn] = field(default_factory=list)
    # Findings a reader can name without judging: a file whose name disagrees.
    problems: list[str] = field(default_factory=list)


def read_live(live: Path) -> Store:
    records = read_lines(Record, live / RECORDS_FILE, f"live/{RECORDS_FILE}")
    events = read_lines(Event, live / EVENTS_FILE, f"live/{EVENTS_FILE}")
    return Store(records.items, events.items, records.torn + events.torn)


def event_files(promoted: Path) -> list[Path]:
    folder = promoted / EVENTS_DIR
    return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []


def read_promoted(promoted: Path) -> Store:
    store = Store()
    folder = promoted / RECORDS_DIR
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        name = f"{PROMOTED_DIR}/{RECORDS_DIR}/{path.name}"
        raw = path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_line(Record, raw, name)
        if isinstance(parsed, str):
            store.torn.append(Torn(name, 1, parsed, raw))
            continue
        if path.name != f"{_file_id(parsed.id)}.json":
            store.problems.append(
                f"{name}: holds record {parsed.id}, the file name says otherwise"
            )
        store.records.append(parsed)
    for path in event_files(promoted):
        lines = read_lines(Event, path, f"{PROMOTED_DIR}/{EVENTS_DIR}/{path.name}")
        store.events.extend(lines.items)
        store.torn.extend(lines.torn)
    return store


def _file_id(record_id: str) -> str:
    # A colon is legal on macOS and Linux but not on every checkout's filesystem.
    return record_id.replace(":", "_")


# ---------------------------------------------------------------- writing


@contextmanager
def locked(folder: Path) -> Iterator[None]:
    """An exclusive lock on a store's folder for one read-then-append."""
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            return True
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"


def append_line(path: Path, line: str) -> None:
    """One line, appended and synced. A torn last line is closed first, so the
    new line never joins it and the fragment stays whole for quarantine."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        lead = "" if _ends_with_newline(path) else "\n"
        data = (lead + line + "\n").encode("utf-8")
        while data:
            data = data[os.write(fd, data) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def quarantine(live: Path, torn: Sequence[Torn], now: dt.datetime) -> int:
    """Copy each torn line into quarantine.jsonl once; the count newly held."""
    path = live / QUARANTINE_FILE
    held = {(t.file, t.line, t.sha256) for t in _quarantined(live)}
    added = 0
    for item in torn:
        if (item.file, item.line, item.sha256) in held:
            continue
        entry = {
            "at": utc_text(now),
            "file": item.file,
            "line": item.line,
            "reason": item.reason,
            "raw": item.raw,
            "sha256": item.sha256,
        }
        append_line(path, json.dumps(entry, sort_keys=True, ensure_ascii=False))
        held.add((item.file, item.line, item.sha256))
        added += 1
    return added


def _quarantined(live: Path) -> list[Torn]:
    path = live / QUARANTINE_FILE
    if not path.is_file():
        return []
    held = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(raw)
            held.append(
                Torn(entry["file"], int(entry["line"]), entry["reason"], entry["raw"])
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return held


def _next(items: Iterable[Record | Event]) -> int:
    return max((i.sequence for i in items), default=0) + 1


def _fresh_id(
    prefix: str, now: dt.datetime, suffix: Callable[[], str], taken: set[str]
) -> str:
    for _ in range(64):
        candidate = new_id(prefix, now, suffix())
        if candidate not in taken:
            return candidate
    raise Bad(f"no free {prefix} id at {utc_text(now)}; the suffix source repeats")


# ---------------------------------------------------------------- derivation


@dataclass(frozen=True, slots=True)
class Derived:
    """What the events make of a record, at read time."""

    status: Status
    level: Level
    basis: Basis
    valid_to: str | None
    conflicts_with: tuple[str, ...]


def default_basis(record: Record) -> Basis:
    """With no verified evidence: one named source, or nothing but inference."""
    return "single-source" if record.source_refs else "inferred"


def best_basis(bases: Iterable[Basis], default: Basis) -> Basis:
    """The strongest basis given, by BASIS_RANK; `default` when none is."""
    given = sorted(bases, key=lambda b: BASIS_RANK[b])
    return given[0] if given else default


def _ordered(events: Iterable[Event]) -> list[Event]:
    unique = {e.id: e for e in events}
    return sorted(unique.values(), key=lambda e: (e.at, e.id))


def derive(
    records: Iterable[Record], events: Iterable[Event], at: str | None = None
) -> dict[str, Derived]:
    """Status, confidence and the effective valid_to of every record, from the
    events up to `at` (all of them when None), so a past moment reads as it was.

    Superseded: a supersede or correct event closed it, at that event's time.
    Conflict: a conflict event pairs it with a record neither side of which is
    closed. Confirmed: a review event gave it a strong basis. Else unconfirmed.
    """
    known = {r.id: r for r in records}
    closed: dict[str, str] = {}
    bases: dict[str, list[Basis]] = {}
    decays: dict[str, int] = {}
    pairs: list[tuple[str, str]] = []
    for event in _ordered(events):
        if at is not None and event.at > at:
            continue
        if event.kind in CLOSING:
            closed.setdefault(event.record, event.at)
        elif event.kind == "review" and event.basis is not None:
            bases.setdefault(event.record, []).append(event.basis)
        elif event.kind == "decay":
            decays[event.record] = decays.get(event.record, 0) + 1
        elif event.kind == "conflict" and event.supersedes_with is not None:
            pairs.append((event.record, event.supersedes_with))
    out: dict[str, Derived] = {}
    for rid, record in sorted(known.items()):
        ends = [t for t in (record.valid_to, closed.get(rid)) if t is not None]
        others = sorted(
            {b if a == rid else a for a, b in pairs if rid in (a, b)} - {rid}
        )
        open_conflicts = tuple(
            o for o in others if o in known and o not in closed and rid not in closed
        )
        seen = bases.get(rid, [])
        basis = best_basis(seen, default_basis(record))
        status: Status
        if rid in closed:
            status = "superseded"
        elif open_conflicts:
            status = "conflict"
        elif basis in STRONG:
            status = "confirmed"
        else:
            status = "unconfirmed"
        level = LEVEL_OF[basis]
        for _ in range(decays.get(rid, 0)):
            level = LOWER[level]
        out[rid] = Derived(
            status, level, basis, min(ends, default=None), open_conflicts
        )
    return out


def valid_at(record: Record, derived: Derived, moment: str) -> bool:
    if record.valid_from > moment:
        return False
    return derived.valid_to is None or moment < derived.valid_to


# ---------------------------------------------------------------- the stores


@dataclass(frozen=True, slots=True)
class Stores:
    """Where the two stores are: the live journal, if any, and the checkout's."""

    promoted: Path
    live: Path | None

    @classmethod
    def at(cls, root: Path, worker_store: Path | None) -> Stores:
        return cls(
            root / PROMOTED_DIR, worker_store / LIVE_DIR if worker_store else None
        )


@dataclass(frozen=True, slots=True)
class Known:
    """Both stores together: one record per id, one event per id."""

    records: dict[str, Record]
    events: list[Event]
    live: Store
    promoted: Store

    @property
    def derived(self) -> dict[str, Derived]:
        return derive(self.records.values(), self.events)


def known(stores: Stores) -> Known:
    live = read_live(stores.live) if stores.live else Store()
    promoted = read_promoted(stores.promoted)
    records = {r.id: r for r in live.records}
    records.update({r.id: r for r in promoted.records})
    events = _ordered([*live.events, *promoted.events])
    return Known(records, events, live, promoted)


def _refuse_secrets(record_like: Mapping[str, Any], what: str) -> None:
    text = "\n".join(
        [str(record_like.get("statement") or "")]
        + [str(s) for s in record_like.get("source_refs") or ()]
    )
    hit = secrets_scan.names(text)
    if hit:
        raise Bad(
            f"{what} refused: the secrets scan found {', '.join(hit)}; the value "
            "is not repeated here. Remove it and write again"
        )


@dataclass(frozen=True, slots=True)
class Added:
    record: Record
    events: tuple[Event, ...]
    # What the caller should hear: dropped input, quarantined lines.
    notes: tuple[str, ...]


def _check_draft(draft: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    typed = [k for k in TOOL_WRITTEN if k in draft]
    if typed:
        raise Bad(
            f"{', '.join(typed)} {'is' if len(typed) == 1 else 'are'} written by "
            "the tool from the launch record (--launch), never typed into a "
            "payload; remove it"
        )
    computed = [k for k in (*COMPUTED, *DERIVED) if k in draft]
    if computed:
        raise Bad(
            f"{', '.join(computed)}: computed by tac memory, never given; remove it"
        )
    version = draft.get("schema_version", SCHEMA_VERSION)
    if version not in MIGRATIONS:
        raise Bad(
            f"schema_version {version} is unknown; this tac writes {SCHEMA_VERSION}"
        )
    body = {k: v for k, v in draft.items() if k not in IGNORED}
    if len(body) != len(draft):
        notes.append(
            "basis ignored: promote recomputes it from evidence it checks itself"
        )
    return body, notes


def add(
    stores: Stores,
    draft: Mapping[str, Any],
    *,
    repository: str,
    launch: Launch | None,
    now: dt.datetime,
    suffix: Callable[[], str] = random_suffix,
) -> Added:
    """Append one record to the live journal, and the events it implies: a
    supersede event on the record it replaces, and a conflict event against each
    confirmed record it contradicts."""
    if stores.live is None:
        raise Bad("no worker store: set memory.runtime_store or run inside git")
    body, notes = _check_draft(draft)
    _refuse_secrets(body, "the record")
    stamp = utc_text(now)
    with locked(stores.live):
        seen = known(stores)
        held = quarantine(stores.live, seen.live.torn, now)
        if held:
            notes.append(f"quarantined {held} line(s) of the live journal")
        rid = _fresh_id("mem", now, suffix, set(seen.records))
        fields: dict[str, Any] = {
            "supersedes": None,
            "paths": (),
            "topic": (),
            "valid_from": stamp,
            "valid_to": None,
            "source_refs": (),
            "order_id": None,
            "run_id": None,
            "commit_sha": None,
            "sensitivity": "internal",
            **body,
            **(launch.model_dump() if launch else dict.fromkeys(TOOL_WRITTEN)),
            "schema_version": SCHEMA_VERSION,
            "id": rid,
            "sequence": _next(seen.live.records),
            "repository": body.get("repository", repository),
            "created": stamp,
        }
        fields["content_hash"] = content_hash(
            str(fields.get("statement") or ""),
            fields.get("topic") or (),
            str(fields.get("kind") or ""),
        )
        try:
            record = Record.model_validate(fields)
        except ValidationError as e:
            raise Bad(f"the record is refused: {_first(e)}") from None
        if record.supersedes is not None and record.supersedes not in seen.records:
            raise Bad(f"supersedes {record.supersedes}, which no store holds")
        derived = seen.derived
        wanted: list[tuple[EventKind, str, str | None]] = []
        if record.supersedes is not None:
            wanted.append(("supersede", record.supersedes, None))
        for other in sorted(seen.records.values(), key=lambda r: r.id):
            view = derived[other.id]
            if (
                other.id != record.supersedes
                and view.status == "confirmed"
                and other.kind == record.kind
                and set(other.topic) == set(record.topic)
                and other.content_hash != record.content_hash
                and valid_at(other, view, stamp)
                and record.valid_from <= stamp
            ):
                wanted.append(("conflict", record.id, other.id))
        append_line(stores.live / RECORDS_FILE, record.line())
        taken = {e.id for e in seen.events}
        events: list[Event] = []
        sequence = _next(seen.live.events)
        for kind, target, other_id in wanted:
            event = Event(
                schema_version=SCHEMA_VERSION,
                id=_fresh_id("evt", now, suffix, taken),
                sequence=sequence,
                at=stamp,
                kind=kind,
                record=target,
                by="tac memory add",
                basis=None,
                note=None,
                supersedes_with=record.id if kind == "supersede" else other_id,
            )
            append_line(stores.live / EVENTS_FILE, event.line())
            taken.add(event.id)
            events.append(event)
            sequence += 1
    return Added(record, tuple(events), tuple(notes))


def append_event(
    stores: Stores,
    *,
    kind: EventKind,
    record: str,
    other: str | None,
    note: str | None,
    by: str | None,
    basis: Basis | None,
    now: dt.datetime,
    suffix: Callable[[], str] = random_suffix,
) -> Event:
    """Append one event to the live journal, naming a record either store holds."""
    if stores.live is None:
        raise Bad("no worker store: set memory.runtime_store or run inside git")
    if note:
        hit = secrets_scan.names(note)
        if hit:
            raise Bad(f"the note is refused: the secrets scan found {', '.join(hit)}")
    with locked(stores.live):
        seen = known(stores)
        quarantine(stores.live, seen.live.torn, now)
        for rid in (record, other):
            if rid is not None and rid not in seen.records:
                raise Bad(f"no record {rid} in either store")
        try:
            event = Event(
                schema_version=SCHEMA_VERSION,
                id=_fresh_id("evt", now, suffix, {e.id for e in seen.events}),
                sequence=_next(seen.live.events),
                at=utc_text(now),
                kind=kind,
                record=record,
                by=by,
                basis=basis,
                note=note,
                supersedes_with=other,
            )
        except ValidationError as e:
            raise Bad(f"the event is refused: {_first(e)}") from None
        append_line(stores.live / EVENTS_FILE, event.line())
    return event


# ---------------------------------------------------------------- evidence


@dataclass(frozen=True, slots=True)
class Evidence:
    """One piece of evidence, verified: the basis it earns and how."""

    basis: Basis
    source: str


def recompute_basis(
    root: Path, record: Record, evidence: Sequence[str], now: dt.datetime
) -> Basis:
    """The best basis the evidence earns once checked; the record's own say and
    anything a payload carried are never read. Refused evidence raises."""
    earned: list[Basis] = [
        verify_evidence(root, record, item, now).basis for item in evidence
    ]
    return best_basis(earned, default_basis(record))


def verify_evidence(
    root: Path, record: Record, item: str, now: dt.datetime
) -> Evidence:
    kind, _, value = item.partition(":")
    if not value:
        raise Bad(
            f"evidence {item!r}: write receipt:<path>, review:<order> or approval:<id>"
        )
    if kind == "receipt":
        return _receipt_evidence(root, record, Path(value))
    if kind == "review":
        return _review_evidence(root, record, value)
    if kind == "approval":
        return _approval_evidence(root, record, value, now)
    raise Bad(f"evidence {item!r}: the kinds are receipt, review and approval")


def _receipt_evidence(root: Path, record: Record, path: Path) -> Evidence:
    """A receipt the runner signed, verified against runner.pub at HEAD, bound
    to this record's repository, run and order and to a revision this checkout
    contains, and showing a passing result."""
    from tac import receipts

    where = path if path.is_absolute() else root / path
    try:
        signed = receipts.parse(where.read_text(encoding="utf-8"))
        run = record.run_id or record.order_id
        if run is None:
            raise Bad(
                f"receipt {path}: the record names no run or order for it to bind"
            )
        revision = signed.receipt.binding.revision
        if record.commit_sha is not None and revision != record.commit_sha:
            raise Bad(
                f"receipt {path}: taken at {revision[:12]}, the record is about "
                f"{record.commit_sha[:12]}"
            )
        if not receipts.is_ancestor(root, revision, "HEAD"):
            raise Bad(f"receipt {path}: revision {revision[:12]} is not in HEAD")
        expected = receipts.Binding(
            repository=record.repository,
            revision=revision,
            run_id=run,
            stage=signed.receipt.binding.stage,
            policy_hash=receipts.policy_hash(root, revision),
            order_id=record.order_id,
        )
        trusted = receipts.trusted_key_from_revision(root, "HEAD")
        receipt = receipts.verify(signed, trusted, expected)
    except OSError as e:
        raise Bad(f"receipt {path}: cannot read it: {e.strerror}") from None
    except receipts.ReceiptError as e:
        raise Bad(f"receipt {path}: {e}") from None
    observed = receipt.observed
    passed = observed.get("exit") == 0 or observed.get("matched") is True
    if receipt.kind not in ("gate", "probe") or not passed:
        raise Bad(f"receipt {path}: it does not record a passing gate or probe")
    return Evidence("test-receipt", f"receipt {receipt.receipt_id}")


def _review_evidence(root: Path, record: Record, order_id: str) -> Evidence:
    """The order's review, through the same gate `tac work review` runs."""
    from tac import work

    if record.order_id != order_id:
        raise Bad(
            f"review:{order_id}: the record belongs to order "
            f"{record.order_id or 'none'}"
        )
    try:
        why = work.review_gate(work.find(order_id, root), work.base_of(root, None))
    except OSError as e:
        raise Bad(f"review:{order_id}: cannot read the order: {e.strerror}") from None
    if why:
        raise Bad(f"review:{order_id}: {'; '.join(why)}")
    return Evidence("reviewed", f"review of {order_id}")


def _approval_evidence(
    root: Path, record: Record, item_id: str, now: dt.datetime
) -> Evidence:
    """A host-signed approval of an item whose arguments name this record."""
    from tac import approvals, human
    from tac.github import agent_identity
    from tac.receipts import ReceiptError, trusted_key_from_revision

    try:
        item = human.load_item(human.human_paths(root), item_id)
        if item.arguments.get("record") != record.id:
            raise Bad(
                f"approval:{item_id}: its arguments name record "
                f"{item.arguments.get('record')!r}, not {record.id}"
            )
        verdict = approvals.verify_signed(
            item,
            trusted_key_from_revision(root, "HEAD"),
            repository=record.repository,
            head=None,
            agent_identity=agent_identity(root),
            now=now,
            consumed=set(),
        )
    except (human.HumanError, ReceiptError) as e:
        raise Bad(f"approval:{item_id}: {e}") from None
    if not verdict.approved:
        raise Bad(f"approval:{item_id}: {verdict.state}: {verdict.reason}")
    return Evidence("owner", f"approval {item_id}")


# ---------------------------------------------------------------- promotion


@dataclass(frozen=True, slots=True)
class Promoted:
    record: Record
    basis: Basis
    path: Path
    events: tuple[Event, ...]


def record_text(record: Record) -> str:
    body = record.model_dump(mode="json")
    return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def promote(
    root: Path,
    stores: Stores,
    record_id: str,
    evidence: Sequence[str],
    *,
    now: dt.datetime,
    environ: Mapping[str, str],
    ancestors: list[str] | None = None,
    by: str = "tac memory promote",
    suffix: Callable[[], str] = random_suffix,
) -> Promoted:
    """Copy one live record into the checkout under the three rules of section
    6: a recomputed basis of test-receipt, reviewed or owner; a derived status
    of confirmed; no secret-like line. Refused inside an agent session, as
    `tac approve` is."""
    from tac.runner import RunnerError, refuse_agent_parent

    try:
        refuse_agent_parent(environ, ancestors)
    except RunnerError as e:
        raise Bad(f"promote refused: {e}") from None
    if stores.live is None:
        raise Bad("no worker store: set memory.runtime_store or run inside git")
    with locked(stores.live), locked(stores.promoted):
        seen = known(stores)
        quarantine(stores.live, seen.live.torn, now)
        record = next((r for r in seen.live.records if r.id == record_id), None)
        if record is None:
            raise Bad(f"no record {record_id} in the live journal")
        if any(r.id == record_id for r in seen.promoted.records):
            raise Bad(f"{record_id} is promoted already")
        if record.sensitivity == "secret":
            raise Bad(
                f"rule sensitivity: {record_id} is marked secret and stays in "
                "the worker store"
            )
        basis = recompute_basis(root, record, evidence, now)
        if basis not in STRONG:
            raise Bad(
                f"rule basis: the recomputed basis of {record_id} is {basis}; "
                "promotion needs test-receipt, reviewed or owner (give --evidence "
                "receipt:<path>, review:<order> or approval:<id>)"
            )
        stamp = utc_text(now)
        review = Event(
            schema_version=SCHEMA_VERSION,
            id=_fresh_id("evt", now, suffix, {e.id for e in seen.events}),
            sequence=_next(seen.live.events),
            at=stamp,
            kind="review",
            record=record_id,
            by=by,
            basis=basis,
            note=None,
            supersedes_with=None,
        )
        status = derive(seen.records.values(), [*seen.events, review])[record_id]
        if status.status != "confirmed":
            raise Bad(
                f"rule status: {record_id} is {status.status}; promotion needs "
                "confirmed"
                + (
                    f" (it conflicts with {', '.join(status.conflicts_with)}; a "
                    "supersede or correct event closes one side)"
                    if status.conflicts_with
                    else ""
                )
            )
        _refuse_secrets(record.model_dump(), "promotion")
        append_line(stores.live / EVENTS_FILE, review.line())
        target = stores.promoted / RECORDS_DIR / f"{_file_id(record_id)}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(record_text(record), encoding="utf-8")
        promoted_ids = {r.id for r in seen.promoted.records} | {record_id}
        have = {e.id for e in seen.promoted.events}
        copied: list[Event] = []
        sequence = _next(seen.promoted.events)
        day = stores.promoted / EVENTS_DIR / f"{now.astimezone(dt.UTC):%Y-%m-%d}.jsonl"
        for event in _ordered([*seen.live.events, review]):
            touches = event.record == record_id or (
                event.supersedes_with == record_id and event.record in promoted_ids
            )
            if not touches or event.id in have or event.record not in promoted_ids:
                continue
            moved = event.model_copy(update={"sequence": sequence})
            append_line(day, moved.line())
            copied.append(moved)
            sequence += 1
        write_index(stores.promoted)
    return Promoted(record, basis, target, tuple(copied))


# ---------------------------------------------------------------- the index


def _json_text(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def build_index(records: Iterable[Record], events: Iterable[Event]) -> dict[str, str]:
    """The index files by name, from the promoted records and events alone.
    Deterministic: sorted keys, sorted ids, `\\n` endings."""
    records = sorted(records, key=lambda r: r.id)
    derived = derive(records, events)
    by_topic: dict[str, list[str]] = {}
    by_order: dict[str, list[str]] = {}
    by_status: dict[str, list[str]] = {s: [] for s in STATUSES}
    detail: dict[str, dict[str, Any]] = {}
    for record in records:
        view = derived[record.id]
        for topic in sorted(set(record.topic)):
            by_topic.setdefault(topic, []).append(record.id)
        if record.order_id is not None:
            by_order.setdefault(record.order_id, []).append(record.id)
        by_status[view.status].append(record.id)
        detail[record.id] = {
            "status": view.status,
            "confidence": {"level": view.level, "basis": view.basis},
            "valid_to": view.valid_to,
            "conflicts_with": list(view.conflicts_with),
            "content_hash": record.content_hash,
        }
    files = {
        "by-topic.json": _json_text({"schema_version": 1, "topics": by_topic}),
        "by-order.json": _json_text({"schema_version": 1, "orders": by_order}),
        "by-status.json": _json_text(
            {"schema_version": 1, "status": by_status, "records": detail}
        ),
    }
    lines = [
        "# The memory index",
        "",
        "Generated by `tac memory index` from `records/` and `events/`; never edit",
        "it by hand. `tac memory lint` fails when these files differ from a fresh",
        "build. Status and confidence are derived from the events here, never",
        "stored on a record.",
        "",
        f"Records: {len(records)}.",
        "",
        "| File | sha256 |",
        "|---|---|",
        *(
            f"| `{name}` | `{hashlib.sha256(text.encode('utf-8')).hexdigest()}` |"
            for name, text in sorted(files.items())
        ),
        "",
    ]
    files[INDEX_README] = "\n".join(lines)
    return files


def _index_of(promoted: Path) -> dict[str, str]:
    store = read_promoted(promoted)
    return build_index(store.records, store.events)


def write_index(promoted: Path) -> list[str]:
    """Rebuild the index; the names of the files that changed."""
    folder = promoted / INDEX_DIR
    folder.mkdir(parents=True, exist_ok=True)
    changed = []
    for name, text in _index_of(promoted).items():
        path = folder / name
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed.append(name)
    return changed


def index_problems(promoted: Path) -> list[str]:
    folder = promoted / INDEX_DIR
    problems = []
    for name, text in _index_of(promoted).items():
        path = folder / name
        rel = f"{PROMOTED_DIR}/{INDEX_DIR}/{name}"
        if not path.is_file():
            problems.append(f"{rel}: missing; run tac memory index")
        elif path.read_text(encoding="utf-8") != text:
            problems.append(f"{rel}: differs from a fresh build; run tac memory index")
    return problems


# ---------------------------------------------------------------- search


def search(
    records: Iterable[Record],
    events: Iterable[Event],
    *,
    at: str | None,
    scope: str | None = None,
    repository: str | None = None,
    kind: str | None = None,
    topic: str | None = None,
    order: str | None = None,
    status: str | None = None,
    text: str | None = None,
) -> list[tuple[Record, Derived]]:
    """Records matching every filter given, oldest first; `at` None skips the
    validity filter."""
    records = list(records)
    derived = derive(records, events, at)
    needle = text.casefold() if text else None
    found = []
    for record in sorted(records, key=lambda r: (r.sequence, r.id)):
        view = derived[record.id]
        if (
            (scope and record.scope != scope)
            or (repository and record.repository != repository)
            or (kind and record.kind != kind)
            or (topic and topic not in record.topic)
            or (order and record.order_id != order)
            or (status and view.status != status)
            or (at is not None and not valid_at(record, view, at))
            or (needle and needle not in record.statement.casefold())
        ):
            continue
        found.append((record, view))
    return found


# ---------------------------------------------------------------- lint


def _sequence_problems(name: str, items: Sequence[Record | Event]) -> list[str]:
    problems = []
    expected = 1
    for item in items:
        if item.sequence < expected:
            problems.append(f"{name}: sequence {item.sequence} repeats or goes back")
        elif item.sequence > expected:
            problems.append(
                f"{name}: sequence jumps from {expected - 1} to {item.sequence}"
            )
        expected = max(expected, item.sequence) + 1
    return problems


def _secret_problems(name: str, record: Record) -> list[str]:
    text = "\n".join([record.statement, *record.source_refs])
    hit = secrets_scan.names(text)
    return [f"{name}: {record.id} looks like it holds a {h}" for h in hit]


def lint(stores: Stores) -> list[str]:
    """Every finding, one line each; empty means both stores are clean."""
    problems: list[str] = []
    live = Store()
    if stores.live is not None:
        try:
            live = read_live(stores.live)
        except Bad as e:
            problems.append(str(e))
        held = {(t.file, t.line, t.sha256) for t in _quarantined(stores.live)}
        for torn in live.torn:
            if (torn.file, torn.line, torn.sha256) not in held:
                problems.append(
                    f"{torn.file}:{torn.line}: does not parse ({torn.reason}); the "
                    "next write quarantines it"
                )
        problems += _sequence_problems(f"live/{RECORDS_FILE}", live.records)
        problems += _sequence_problems(f"live/{EVENTS_FILE}", live.events)
        live_ids = {r.id for r in live.records}
        for event in live.events:
            for rid in (event.record, event.supersedes_with):
                if rid is not None and rid not in live_ids:
                    problems.append(
                        f"live/{EVENTS_FILE}: event {event.id} names {rid}, which "
                        "the live journal does not hold"
                    )
        for record in live.records:
            problems += _secret_problems(f"live/{RECORDS_FILE}", record)
    try:
        promoted = read_promoted(stores.promoted)
    except Bad as e:
        return [*problems, str(e)]
    problems += promoted.problems
    problems += [
        f"{t.file}:{t.line}: does not parse ({t.reason})" for t in promoted.torn
    ]
    seqs: dict[int, str] = {}
    for record in promoted.records:
        # A promoted record keeps its journal sequence; not every record is
        # promoted, so gaps are expected here and only a repeat is wrong.
        if record.sequence in seqs:
            problems.append(
                f"{PROMOTED_DIR}/{RECORDS_DIR}: {record.id} repeats sequence "
                f"{record.sequence} of {seqs[record.sequence]}"
            )
        seqs[record.sequence] = record.id
        problems += _secret_problems(f"{PROMOTED_DIR}/{RECORDS_DIR}", record)
    problems += _sequence_problems(f"{PROMOTED_DIR}/{EVENTS_DIR}", promoted.events)
    ids = {r.id for r in promoted.records}
    for event in promoted.events:
        if event.record not in ids:
            problems.append(
                f"{PROMOTED_DIR}/{EVENTS_DIR}: event {event.id} names {event.record}, "
                "which is not promoted"
            )
    # A checkout that has never promoted anything has no store to index yet.
    if not promoted.torn and stores.promoted.is_dir():
        problems += index_problems(stores.promoted)
    return problems


# ---------------------------------------------------------------- selection


@dataclass(frozen=True, slots=True)
class Selection:
    """What a stage prompt receives from memory, and the proof of it: the ids in
    order, each record's content hash, the text and its length."""

    selector_version: int
    stage: str
    order: str | None
    now: str
    cap_chars: int
    ids: tuple[str, ...]
    content_hashes: tuple[str, ...]
    text: str
    chars: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "selector_version": self.selector_version,
            "stage": self.stage,
            "order": self.order,
            "now": self.now,
            "cap_chars": self.cap_chars,
            "ids": list(self.ids),
            "content_hashes": list(self.content_hashes),
            "text": self.text,
            "chars": self.chars,
        }


def render_line(record: Record, basis: str) -> str:
    statement = " ".join(record.statement.split())
    return f"- [{record.kind}] {statement} ({record.id}, {basis})\n"


def _epoch(text: str) -> int:
    return int(parse_utc(text).timestamp())


def select(
    records: Iterable[Record],
    events: Iterable[Event],
    *,
    stage: str,
    order: str | None,
    paths: Sequence[str],
    cap_chars: int,
    now: str,
    selector_version: int = SELECTOR_VERSION,
    topics: Sequence[str] = (),
    repository: str | None = None,
) -> Selection:
    """The records a stage receives, deterministic and pure.

    Filter: scope (project and team always, order only for this order, session
    never), repository, valid at `now`, status confirmed, and topic overlap when
    topics are given. Rank: decisions first; then an exact path match with the
    order's owns, the exact order, the evidence basis, the newest valid_from, and
    the id. Fill to `cap_chars` in that order, skipping a record that does not
    fit. A mandatory record (topic `policy`) has its room reserved before any
    other and is never truncated: one that cannot fit is a refusal naming it.
    """
    if selector_version != SELECTOR_VERSION:
        raise Bad(
            f"selector_version {selector_version} is unknown; this tac selects "
            f"with {SELECTOR_VERSION}"
        )
    records = list(records)
    derived = derive(records, events, now)
    wanted_topics, owned = set(topics), set(paths)
    chosen: list[Record] = []
    for record in records:
        view = derived[record.id]
        if record.scope == "session":
            continue
        if record.scope == "order" and (order is None or record.order_id != order):
            continue
        if repository is not None and record.repository != repository:
            continue
        if view.status != "confirmed" or not valid_at(record, view, now):
            continue
        if wanted_topics and not wanted_topics & set(record.topic):
            continue
        chosen.append(record)

    def rank(record: Record) -> tuple[int, int, int, int, int, str]:
        return (
            0 if record.kind == "decision" else 1,
            0 if owned & set(record.paths) else 1,
            0 if order is not None and record.order_id == order else 1,
            BASIS_RANK[derived[record.id].basis],
            -_epoch(record.valid_from),
            record.id,
        )

    chosen.sort(key=rank)
    lines = {r.id: render_line(r, derived[r.id].basis) for r in chosen}
    left = cap_chars
    for record in (r for r in chosen if r.mandatory):
        need = len(lines[record.id])
        if need > left:
            raise Bad(
                f"mandatory record {record.id} needs {need} characters and {left} "
                f"of the cap of {cap_chars} remain; a policy record is never "
                "truncated: raise memory.select_cap_chars or shorten it"
            )
        left -= need
    picked = {r.id for r in chosen if r.mandatory}
    for record in (r for r in chosen if not r.mandatory):
        need = len(lines[record.id])
        if need <= left:
            picked.add(record.id)
            left -= need
    kept = [r for r in chosen if r.id in picked]
    text = "".join(lines[r.id] for r in kept)
    return Selection(
        selector_version=selector_version,
        stage=stage,
        order=order,
        now=now,
        cap_chars=cap_chars,
        ids=tuple(r.id for r in kept),
        content_hashes=tuple(r.content_hash for r in kept),
        text=text,
        chars=len(text),
    )
