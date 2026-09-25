"""`tac sync` and `tac check`: render the harness files, and judge them."""

from __future__ import annotations

import os
from pathlib import Path

import click

from tac import commits
from tac.config import load_config
from tac.doctor import find_root
from tac.sync import check_staged, check_tree, sync
from tac.work import Bad

ROOT = click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)


def _root(root: Path | None) -> Path:
    return root.resolve() if root else find_root(Path.cwd())


@click.command("sync")
@ROOT
@click.option(
    "--no-links",
    is_flag=True,
    help="Skip the per-skill links under .claude/skills/.",
)
def sync_command(root: Path | None, no_links: bool) -> None:
    """Render every harness file from .agents/ and write generated.lock."""
    try:
        changed = sync(_root(root), links=not no_links)
    except Bad as e:
        raise click.ClickException(str(e)) from None
    for line in changed:
        click.echo(line)
    if not changed:
        click.echo("nothing to write: every generated file is current")


@click.command("check")
@ROOT
@click.option(
    "--staged",
    is_flag=True,
    help="Judge the index, what a commit would hold, not the working tree.",
)
@click.option(
    "--commit-msg",
    "message",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Judge this commit message file against [commits], as the commit-msg "
    "hook does, instead of the generated files.",
)
def check_command(root: Path | None, staged: bool, message: Path | None) -> None:
    """Re-render in memory; exit 1 on any drift, hand edit or stale lock."""
    base = _root(root)
    if staged and message:
        raise click.UsageError("--staged and --commit-msg judge different things")
    try:
        if message:
            problems = _commit_problems(base, message)
        else:
            problems = check_staged(base) if staged else check_tree(base)
    except Bad as e:
        raise click.ClickException(str(e)) from None
    if problems:
        for line in problems:
            click.echo(line, err=True)
        raise SystemExit(1)
    if message:
        click.echo("the commit message keeps [commits]")
        return
    click.echo("generated files match the lock" + (" (staged)" if staged else ""))


def _commit_problems(root: Path, message: Path) -> list[str]:
    found = commits.rules(load_config(root))
    if found is None:
        return []
    text = message.read_text(encoding="utf-8", errors="replace")
    agent = commits.from_agent(os.environ)
    return [
        f"commit message: {p}" for p in commits.check_message(text, found, agent=agent)
    ]
