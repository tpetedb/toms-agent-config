"""`tac session log|purge`: the session journal, run by the launcher."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import click

from tac import memory, session
from tac.doctor import find_root
from tac.work import Bad

ROOT = click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)


def _store(root: Path | None) -> Path:
    from tac.handoff import worker_store

    return worker_store(root.resolve() if root else find_root(Path.cwd()))


@click.group("session")
def session_group() -> None:
    """The per-session journal in the worker store, and its one purge path."""


@session_group.command("log")
@ROOT
@click.option(
    "--from",
    "source",
    default=None,
    help="A JSON object of event fields, or - for stdin; flags given override it.",
)
@click.option("--session-id", default=None)
@click.option("--event", default=None, help="What happened, e.g. start or tool.")
@click.option("--ts", default=None, help="UTC; defaults to now.")
@click.option("--provider", default=None)
@click.option("--harness", default=None)
@click.option("--model", default=None)
@click.option("--effort", default=None)
@click.option("--role", default=None)
@click.option("--tool", default=None)
@click.option("--topic", multiple=True)
@click.option("--cost-usd", type=float, default=None)
@click.option("--tokens-input", type=int, default=None)
@click.option("--tokens-output", type=int, default=None)
@click.option("--tokens-cache-read", type=int, default=None)
@click.option("--tokens-cache-write", type=int, default=None)
def log_command(root: Path | None, source: str | None, **flags: Any) -> None:
    """Append one event to <worker store>/sessions/<UTC date>/<id>.jsonl and
    refresh that day's _index.json."""
    try:
        data = _source(source)
        tokens = dict(data.get("tokens") or {})
        for name in session.TOKEN_FIELDS:
            value = flags.pop(f"tokens_{name}")
            if value is not None:
                tokens[name] = value
        if tokens:
            data["tokens"] = tokens
        for name, value in flags.items():
            if value not in (None, ()):
                data[name] = list(value) if isinstance(value, tuple) else value
        data.setdefault("ts", memory.utc_text(memory.utc_now()))
        event = session.parse_event(data)
        path = session.log(_store(root), event)
    except Bad as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"logged {event.event} to {path.parent.name}/{path.name}")


def _source(source: str | None) -> dict[str, Any]:
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
        raise Bad(f"{source}: cannot read it: {e.strerror}") from None
    except json.JSONDecodeError as e:
        raise Bad(f"{source}: not JSON: {e.msg}") from None
    if not isinstance(data, dict):
        raise Bad(f"{source}: a session event is a JSON object")
    return data


@session_group.command("purge")
@ROOT
@click.argument("session_id")
@click.option("--reason", required=True, help="Why; it goes in the audit line.")
@click.option("--by", default=None, help="Who purges; defaults to the login name.")
def purge_command(
    root: Path | None, session_id: str, reason: str, by: str | None
) -> None:
    """The one audited purge: move the session under sessions/_purged/ and
    record who, when, why and the sha256 of what moved. Nothing is deleted."""
    try:
        done = session.purge(
            _store(root),
            session_id,
            reason=reason,
            by=by or os.environ.get("USER") or "unknown",
            now=memory.utc_now(),
        )
    except Bad as e:
        raise click.ClickException(str(e)) from None
    for path in done.moved:
        click.echo(f"purged {session_id} to {path.parent.name}/{path.name}")
