"""The `tac` command line."""

from __future__ import annotations

import json
from pathlib import Path

import click

from tac import __version__
from tac.doctor import CHECKS, exit_code, find_root, render_text, run_checks
from tac.work_cli import work_group


@click.group()
@click.version_option(__version__, prog_name="tac")
def cli() -> None:
    """toms-agent-config: one .agents/ tree that every harness obeys."""


@cli.command()
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the results as JSON.")
def doctor(root: Path | None, as_json: bool) -> None:
    """Run every named check; exit non-zero unless all pass."""
    base = root.resolve() if root else find_root(Path.cwd())
    results = run_checks(base, CHECKS)
    code = exit_code(results)
    if as_json:
        payload = {
            "ok": code == 0,
            "root": base.name,
            "checks": [r.as_dict() for r in results],
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(render_text(results))
    raise SystemExit(code)


cli.add_command(work_group)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
