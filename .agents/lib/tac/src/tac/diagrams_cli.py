"""`tac diagrams render` and `tac diagrams check`: the pinned render loop and
the judgement of the sources against its lock (docs/DESIGN.md section 14)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import click

from tac import diagrams
from tac.doctor import find_root
from tac.work import Bad

ROOT = click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)


def _root(root: Path | None) -> Path:
    return root.resolve() if root else find_root(Path.cwd())


@click.group("diagrams")
def diagrams_group() -> None:
    """The diagram inventory, the house subset and the pinned render loop."""


@diagrams_group.command("render")
@ROOT
@click.option(
    "--out",
    "out",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Where the SVGs go; a fresh scratch folder by default. Never committed.",
)
def render_command(root: Path | None, out: Path | None) -> None:
    """Render every diagram with the mermaid-cli mise.toml pins, then refresh
    docs/diagrams/render.lock. Needs node 22.13 or later and the network on
    the first run; exits 1 on the first render that fails."""
    base = _root(root)
    try:
        if out is not None:
            diagrams.render(base, out.resolve(), echo=click.echo)
            return
        with tempfile.TemporaryDirectory(prefix="tac-diagrams-") as tmp:
            diagrams.render(base, Path(tmp), echo=click.echo)
    except Bad as e:
        raise click.ClickException(str(e)) from None


@diagrams_group.command("check")
@ROOT
def check_command(root: Path | None) -> None:
    """The inventory matches github.toml, every .mmd keeps the house subset, and
    every source hashes to what render.lock recorded. Needs no node."""
    try:
        problems = diagrams.check(_root(root))
    except Bad as e:
        raise click.ClickException(str(e)) from None
    if problems:
        for line in problems:
            click.echo(line, err=True)
        raise SystemExit(1)
    click.echo("the diagrams match the inventory and render.lock")
