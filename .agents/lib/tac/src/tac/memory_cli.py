"""`tac memory add|event|search|index|lint|promote|schema` and `tac select`."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, cast

import click

from tac import memory
from tac.doctor import find_root
from tac.work import Bad

ROOT = click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)
KINDS = click.Choice(
    [
        "observation",
        "decision",
        "lesson",
        "question",
        "contract",
        "component",
        "session-digest",
        "trace-digest",
    ]
)
SCOPES = click.Choice(["project", "team", "order", "session"])


def _root(root: Path | None) -> Path:
    return root.resolve() if root else find_root(Path.cwd())


def _fail(message: str) -> None:
    raise click.ClickException(message)


def _worker_store(root: Path, *, needed: bool) -> Path | None:
    """The worker store the config names; without git and without an absolute
    runtime_store there is none, which only a writer minds."""
    from tac.handoff import worker_store

    try:
        return worker_store(root)
    except Bad:
        if needed:
            raise
        return None


def _stores(root: Path, *, live: bool = True, needed: bool = False) -> memory.Stores:
    store = _worker_store(root, needed=needed) if live else None
    return memory.Stores.at(root, store)


def _now(at: str | None) -> str:
    return at if at is not None else memory.utc_text(memory.utc_now())


@click.group("memory")
def memory_group() -> None:
    """The append-only memory bus: records, events, the index and promotion."""


@memory_group.command("add")
@ROOT
@click.option(
    "--payload",
    default=None,
    help="A JSON object of record fields, or - for stdin; flags given override it.",
)
@click.option("--kind", type=KINDS, default=None)
@click.option("--scope", type=SCOPES, default=None)
@click.option("--statement", default=None, help="The claim, in one plain paragraph.")
@click.option("--topic", "topic", multiple=True, help="A topic; repeat for each.")
@click.option("--path", "paths", multiple=True, help="A path it is about; repeat.")
@click.option("--source-ref", "source_refs", multiple=True, help="A source; repeat.")
@click.option("--order", "order_id", default=None)
@click.option("--run", "run_id", default=None)
@click.option("--commit", "commit_sha", default=None)
@click.option("--supersedes", default=None, help="The record id this one replaces.")
@click.option(
    "--sensitivity", type=click.Choice(["public", "internal", "secret"]), default=None
)
@click.option("--valid-from", default=None, help="UTC; defaults to now.")
@click.option("--valid-to", default=None, help="UTC; defaults to open.")
@click.option("--repository", default=None, help="owner/name; defaults to origin.")
@click.option(
    "--launch",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="The launch record the launcher wrote; the only source of provider, "
    "harness, model, effort and role.",
)
def add_command(
    root: Path | None,
    payload: str | None,
    launch: Path | None,
    repository: str | None,
    **flags: Any,
) -> None:
    """Append one record to the live journal in the worker store."""
    base = _root(root)
    try:
        draft = _payload(payload)
        for name, value in flags.items():
            if value not in (None, ()):
                draft[name] = list(value) if isinstance(value, tuple) else value
        if not repository and "repository" not in draft:
            from tac.runner import RunnerError, origin_repository

            try:
                repository = origin_repository(base)
            except RunnerError as e:
                raise Bad(f"{e}; pass --repository owner/name") from None
        added = memory.add(
            _stores(base, needed=True),
            draft,
            repository=repository or str(draft.get("repository")),
            launch=memory.read_launch(launch) if launch else None,
            now=memory.utc_now(),
        )
    except Bad as e:
        _fail(str(e))
        return
    for note in added.notes:
        click.echo(f"note: {note}", err=True)
    click.echo(f"added {added.record.id} (sequence {added.record.sequence})")
    for event in added.events:
        click.echo(f"event {event.kind}: {event.record} with {event.supersedes_with}")


def _payload(source: str | None) -> dict[str, Any]:
    if source is None:
        return {}
    try:
        text = (
            sys.stdin.read()
            if source == "-"
            else Path(source).read_text(encoding="utf-8")
        )
        data = json.loads(text)
    except OSError as e:
        raise Bad(f"{source}: cannot read the payload: {e.strerror}") from None
    except json.JSONDecodeError as e:
        raise Bad(f"{source}: the payload is not JSON: {e.msg}") from None
    if not isinstance(data, dict):
        raise Bad(f"{source}: the payload is a JSON object of record fields")
    return data


@memory_group.command("event")
@ROOT
@click.argument(
    "kind", type=click.Choice(["supersede", "review", "decay", "correct", "conflict"])
)
@click.option("--record", "record_id", required=True, help="The record it is about.")
@click.option(
    "--with",
    "other",
    default=None,
    help="supersede and correct: the replacing record; conflict: the other side.",
)
@click.option("--note", default=None)
@click.option("--by", default=None, help="Who records it.")
@click.option(
    "--evidence",
    multiple=True,
    help="review only: receipt:<path>, review:<order> or approval:<id>; the basis "
    "is recomputed from it.",
)
def event_command(
    root: Path | None,
    kind: str,
    record_id: str,
    other: str | None,
    note: str | None,
    by: str | None,
    evidence: tuple[str, ...],
) -> None:
    """Append one event to the live journal: the only way a record changes."""
    base = _root(root)
    now = memory.utc_now()
    try:
        stores = _stores(base, needed=True)
        basis = None
        if evidence:
            if kind != "review":
                raise Bad("only a review event takes --evidence")
            found = memory.known(stores).records.get(record_id)
            if found is None:
                raise Bad(f"no record {record_id} in either store")
            basis = memory.recompute_basis(base, found, evidence, now)
        event = memory.append_event(
            stores,
            kind=cast(memory.EventKind, kind),
            record=record_id,
            other=other,
            note=note,
            by=by,
            basis=basis,
            now=now,
        )
    except Bad as e:
        _fail(str(e))
        return
    click.echo(f"event {event.id}: {event.kind} on {event.record}")


@memory_group.command("search")
@ROOT
@click.option("--scope", type=SCOPES, default=None)
@click.option("--repository", default=None)
@click.option("--kind", type=KINDS, default=None)
@click.option("--topic", default=None)
@click.option("--order", default=None)
@click.option(
    "--status",
    type=click.Choice(list(memory.STATUSES)),
    default=None,
)
@click.option("--at", default=None, help="Valid at this UTC time; defaults to now.")
@click.option("--any-time", is_flag=True, help="Skip the validity filter.")
@click.option("--text", default=None, help="A case-insensitive substring.")
@click.option(
    "--store",
    type=click.Choice(["all", "live", "promoted"]),
    default="all",
    show_default=True,
)
@click.option("--json", "as_json", is_flag=True, help="One JSON object per line.")
def search_command(
    root: Path | None,
    scope: str | None,
    repository: str | None,
    kind: str | None,
    topic: str | None,
    order: str | None,
    status: str | None,
    at: str | None,
    any_time: bool,
    text: str | None,
    store: str,
    as_json: bool,
) -> None:
    """Records that match every filter, oldest first."""
    base = _root(root)
    try:
        if at is not None:
            memory.parse_utc(at)
        seen = memory.known(_stores(base, live=store != "promoted"))
        records = (
            seen.promoted.records
            if store == "promoted"
            else seen.live.records
            if store == "live"
            else list(seen.records.values())
        )
        found = memory.search(
            records,
            seen.events,
            at=None if any_time else _now(at),
            scope=scope,
            repository=repository,
            kind=kind,
            topic=topic,
            order=order,
            status=status,
            text=text,
        )
    except Bad as e:
        _fail(str(e))
        return
    for record, view in found:
        if as_json:
            row = record.model_dump(mode="json")
            row["status"] = view.status
            row["confidence"] = {"level": view.level, "basis": view.basis}
            row["valid_to"] = view.valid_to
            click.echo(json.dumps(row, sort_keys=True, ensure_ascii=False))
        else:
            statement = " ".join(record.statement.split())
            click.echo(
                f"{record.id}  {view.status:<11} {record.kind:<14} "
                f"{','.join(record.topic):<24} {statement[:80]}"
            )
    if not as_json:
        click.echo(f"{len(found)} record(s)", err=True)


@memory_group.command("index")
@ROOT
@click.option("--check", is_flag=True, help="Exit 1 when the index is not current.")
def index_command(root: Path | None, check: bool) -> None:
    """Rebuild .agents/memory/_index/ from the promoted records and events."""
    base = _root(root)
    promoted = base / memory.PROMOTED_DIR
    try:
        if check:
            problems = memory.index_problems(promoted)
            for line in problems:
                click.echo(line)
            raise SystemExit(1 if problems else 0)
        changed = memory.write_index(promoted)
    except Bad as e:
        _fail(str(e))
        return
    for name in changed:
        click.echo(f"wrote {memory.PROMOTED_DIR}/{memory.INDEX_DIR}/{name}")
    if not changed:
        click.echo("index current")


@memory_group.command("lint")
@ROOT
@click.option("--promoted-only", is_flag=True, help="Leave the live journal out of it.")
def lint_command(root: Path | None, promoted_only: bool) -> None:
    """Schema, caps, sequence, event targets, the secrets scan and the index,
    over both stores; one line per finding, exit 1 on any."""
    base = _root(root)
    try:
        problems = memory.lint(_stores(base, live=not promoted_only))
    except Bad as e:
        _fail(str(e))
        return
    for line in problems:
        click.echo(line)
    if problems:
        raise SystemExit(1)
    click.echo("memory holds: records, events and index are clean")


@memory_group.command("promote")
@ROOT
@click.argument("record_id")
@click.option(
    "--evidence",
    multiple=True,
    help="receipt:<path>, review:<order> or approval:<id>; repeat for each.",
)
def promote_command(
    root: Path | None, record_id: str, evidence: tuple[str, ...]
) -> None:
    """Host or runner only: copy a live record into .agents/memory/records/
    when its recomputed basis, derived status and the secrets scan allow it."""
    base = _root(root)
    try:
        done = memory.promote(
            base,
            _stores(base, needed=True),
            record_id,
            evidence,
            now=memory.utc_now(),
            environ=os.environ,
        )
    except Bad as e:
        _fail(str(e))
        return
    click.echo(
        f"promoted {done.record.id} on basis {done.basis} to "
        f"{done.path.relative_to(base).as_posix()}"
    )


@memory_group.command("schema")
@ROOT
@click.option("--write", is_flag=True, help="Write the record and session contracts.")
def schema_command(root: Path | None, write: bool) -> None:
    """Print, or write, the memory record contract (model-facing, strict subset)
    and the session event contract, generated from their models."""
    from tac.session import SESSION_CONTRACT, session_json_schema

    schemas = {
        memory.RECORD_CONTRACT: memory.record_json_schema(),
        SESSION_CONTRACT: session_json_schema(),
    }
    if not write:
        for rel, body in schemas.items():
            click.echo(f"# {rel}")
            click.echo(memory.schema_text(body), nl=False)
        return
    base = _root(root)
    for rel, body in schemas.items():
        path, text = base / rel, memory.schema_text(body)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            click.echo(f"wrote {rel}")


def order_scope(root: Path, order_id: str | None) -> tuple[str, ...]:
    """The order's owns from work/orders/<id>/order.toml, or nothing when the
    order has no folder here."""
    from tac import work

    if order_id is None or not (root / work.ORDERS / order_id / "order.toml").is_file():
        return ()
    return work.find(order_id, root).owns


@click.command("select")
@ROOT
@click.option("--stage", required=True, help="The stage the prompt is for.")
@click.option("--order", "order_id", default=None, help="The order, if any.")
@click.option("--topic", "topics", multiple=True, help="Limit to these topics.")
@click.option("--at", default=None, help="Select as of this UTC time.")
@click.option("--json", "as_json", is_flag=True, help="Print the whole selection.")
def select_command(
    root: Path | None,
    stage: str,
    order_id: str | None,
    topics: tuple[str, ...],
    at: str | None,
    as_json: bool,
) -> None:
    """What a stage prompt receives from the promoted memory, deterministic,
    capped at memory.select_cap_chars; a mandatory policy record that does not
    fit is a refusal."""
    from tac.config import load_config

    base = _root(root)
    try:
        now = _now(at)
        memory.parse_utc(now)
        cap = load_config(base).knobs.memory.select_cap_chars
        store = memory.read_promoted(base / memory.PROMOTED_DIR)
        chosen = memory.select(
            store.records,
            store.events,
            stage=stage,
            order=order_id,
            paths=order_scope(base, order_id),
            cap_chars=cap,
            now=now,
            topics=topics,
        )
    except Bad as e:
        _fail(str(e))
        return
    if as_json:
        click.echo(json.dumps(chosen.as_dict(), indent=2, sort_keys=True))
        return
    click.echo(chosen.text, nl=False)
    click.echo(
        f"selector {chosen.selector_version}: {len(chosen.ids)} record(s), "
        f"{chosen.chars} of {chosen.cap_chars} characters",
        err=True,
    )
