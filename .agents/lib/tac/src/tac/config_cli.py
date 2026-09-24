"""`tac config show|check` and `tac explain`: the effective configuration, read-only."""

from __future__ import annotations

import json
from pathlib import Path

import click

from tac.config import Config, Entry, floor_check, load_config, show_value
from tac.doctor import find_root
from tac.work import Bad

ROOT = click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)


def _load(root: Path | None) -> Config:
    base = root.resolve() if root else find_root(Path.cwd())
    try:
        return load_config(base)
    except Bad as e:
        raise click.ClickException(str(e)) from None


def _line(entry: Entry) -> str:
    return f"{entry.path} = {show_value(entry.value)}"


@click.group("config")
def config_group() -> None:
    """The configuration under .agents/: see it, check it."""


@config_group.command("show")
@ROOT
@click.option("--json", "as_json", is_flag=True, help="Print the values as JSON.")
def show(root: Path | None, as_json: bool) -> None:
    """Print every effective value and the file it came from."""
    config = _load(root)
    if as_json:
        click.echo(json.dumps(config.tree(), indent=2, ensure_ascii=False))
        return
    for entry in config.entries.values():
        click.echo(f"{_line(entry)}    # {entry.source}:{entry.line}")
    for note in config.notes:
        click.echo(f"note: {note}", err=True)


@config_group.command("check")
@ROOT
@click.option(
    "--base",
    default=None,
    help="A git revision whose floor applies; the candidate may only tighten it.",
)
def check(root: Path | None, base: str | None) -> None:
    """Load and cross-check every file; exit 1 naming each problem."""
    config = _load(root)
    try:
        problems, notes = floor_check(config.root, base) if base else ([], [])
    except Bad as e:
        raise click.ClickException(str(e)) from None
    for note in notes:
        click.echo(f"note: {note}", err=True)
    if problems:
        raise click.ClickException("\n".join(problems))
    click.echo(
        f"config holds: profile {config.profile.name}, kind "
        f"{config.knobs.project.kind}, {len(config.entries)} values"
    )


@click.command("explain")
@ROOT
@click.option("--json", "as_json", is_flag=True, help="Print the entries as JSON.")
@click.argument("key", required=False)
def explain(root: Path | None, as_json: bool, key: str | None) -> None:
    """A key's effective value, the file and line it came from, when it applies,
    and its comment. A table prints every key under it; no key prints them all."""
    config = _load(root)
    entries = list(config.entries.values()) if key is None else config.explain(key)
    if not entries:
        hint = config.close_to(key or "")
        more = f"; did you mean {', '.join(hint)}" if hint else ""
        raise click.ClickException(f"no key {key!r} in the configuration{more}")
    if as_json:
        click.echo(json.dumps([e.as_dict() for e in entries], indent=2))
        return
    table = config.tables.get(key or "")
    if table is not None and table.comment:
        click.echo(f"[{key}]  # {table.source}:{table.line}")
        for text in table.comment.splitlines():
            click.echo(f"  {text}".rstrip())
        click.echo()
    for entry in entries:
        click.echo(_line(entry))
        click.echo(f"  from:    {entry.source}:{entry.line}")
        click.echo(f"  applies: {entry.applies}")
        for text in entry.comment.splitlines():
            click.echo(f"  {text}".rstrip())
        if len(entries) > 1:
            click.echo()
