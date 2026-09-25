"""`tac config show|check|schema` and `tac explain`: the effective configuration."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import click

from tac.adapters import claude_session, provider_of
from tac.config import (
    CONFIG_SCHEMAS,
    CONTRACTS_DIR,
    Config,
    Entry,
    config_json_schemas,
    floor_check,
    load_config,
    show_value,
)
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


def _seats(config: Config, role: str) -> list[str]:
    """What a role runs on, seat by seat, from config/models.toml, and for a role
    the owner starts by hand the exact command that starts it."""
    spec = config.models.roles.get(role)
    if spec is None:
        return []
    claude = provider_of(config, "claude")
    argv = claude_session(config, role)
    lines: list[str] = []
    for pid, found in spec.seats().items():
        harness = config.models.providers[pid].harness
        entry = config.entries[f"models.roles.{role}.{pid}.model"]
        lines.append(
            f"roles.{role} on {pid}, through {harness}  # {entry.source}:{entry.line}"
        )
        lines.append(f"  model:     {found.model}")
        effort = found.effort or "none, the model takes no effort parameter"
        lines.append(f"  effort:    {effort}")
        if harness == "claude":
            lines.append(f"  ultracode: {str(found.ultracode).lower()}")
        if pid == claude and argv is not None:
            by = "  (just chief)" if role == config.knobs.governance.chief else ""
            lines.append(f"  launch:    {shlex.join(argv)}{by}")
        lines += [f"  {text}".rstrip() for text in entry.comment.splitlines()]
        lines.append("")
    return lines


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
    # A container profile loads only with a marker; name it, so the owner sees
    # what the check took as proof.
    where = (
        "" if config.profile.on_host else f", inside a container: {config.container}"
    )
    click.echo(
        f"config holds: profile {config.profile.name}, kind "
        f"{config.knobs.project.kind}, {len(config.entries)} values{where}"
    )


def _schema_text(schema: object) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


@config_group.command("schema")
@ROOT
@click.option(
    "--write",
    is_flag=True,
    help=f"Write every schema to {CONTRACTS_DIR}/ and remove any it no longer has.",
)
@click.argument("name", required=False, type=click.Choice(sorted(CONFIG_SCHEMAS)))
def schema(root: Path | None, write: bool, name: str | None) -> None:
    """Print a config file's JSON Schema draft-07, generated from its model, or
    write them all to contracts/config/<name>.schema.json."""
    schemas = config_json_schemas()
    if not write:
        if name is None:
            raise click.UsageError("name a schema, or pass --write")
        click.echo(_schema_text(schemas[name]), nl=False)
        return
    if name is not None:
        raise click.UsageError("--write writes every schema; drop the name")
    folder = (root.resolve() if root else find_root(Path.cwd())) / CONTRACTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    wanted = {f"{n}.schema.json": s for n, s in schemas.items()}
    for stale in sorted(folder.glob("*.schema.json")):
        if stale.name not in wanted:
            stale.unlink()
            click.echo(f"removed {CONTRACTS_DIR}/{stale.name}")
    for file, body in wanted.items():
        path, text = folder / file, _schema_text(body)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            click.echo(f"wrote {CONTRACTS_DIR}/{file}")


@config_group.command("launch-command")
@ROOT
@click.option("--json", "as_json", is_flag=True, help="Print the argv as JSON.")
@click.argument("role", required=False)
def launch_command(root: Path | None, as_json: bool, role: str | None) -> None:
    """Print the command that starts ROLE's own Claude Code session, as
    config/models.toml seats it; nothing is started. ROLE defaults to the chief
    [governance] names. `just chief` runs what this prints."""
    config = _load(root)
    name = role or config.knobs.governance.chief
    argv = claude_session(config, name)
    if argv is None:
        raise click.ClickException(
            f"{name!r} is not a role the owner starts as a Claude Code session: "
            'it needs a charter with runs = "host-session" and a seat on the '
            "provider that runs through claude"
        )
    click.echo(json.dumps(argv) if as_json else shlex.join(argv))


@click.command("explain")
@ROOT
@click.option("--json", "as_json", is_flag=True, help="Print the entries as JSON.")
@click.argument("key", required=False)
def explain(root: Path | None, as_json: bool, key: str | None) -> None:
    """A key's effective value, the file and line it came from, when it applies,
    and its comment. A table prints every key under it; no key prints them all.
    roles.<role> first prints the model, effort and ultracode of each of the
    role's seats in config/models.toml, and the command that starts it."""
    config = _load(root)
    entries = list(config.entries.values()) if key is None else config.explain(key)
    if not entries:
        hint = config.close_to(key or "")
        more = f"; did you mean {', '.join(hint)}" if hint else ""
        raise click.ClickException(f"no key {key!r} in the configuration{more}")
    if as_json:
        click.echo(json.dumps([e.as_dict() for e in entries], indent=2))
        return
    role = (key or "").removeprefix("roles.")
    if key == f"roles.{role}":
        for text in _seats(config, role):
            click.echo(text)
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
