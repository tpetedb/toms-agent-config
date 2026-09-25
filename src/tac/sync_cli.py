"""`tac sync` and `tac check`: render the harness files, and judge them."""

from __future__ import annotations

from pathlib import Path

import click

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
def check_command(root: Path | None, staged: bool) -> None:
    """Re-render in memory; exit 1 on any drift, hand edit or stale lock."""
    base = _root(root)
    try:
        problems = check_staged(base) if staged else check_tree(base)
    except Bad as e:
        raise click.ClickException(str(e)) from None
    if problems:
        for line in problems:
            click.echo(line, err=True)
        raise SystemExit(1)
    click.echo("generated files match the lock" + (" (staged)" if staged else ""))
