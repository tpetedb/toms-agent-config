"""The `tac` command line."""

from __future__ import annotations

import json
from pathlib import Path

import click

from tac import __version__
from tac.config_cli import config_group, explain
from tac.diagrams_cli import diagrams_group
from tac.doctor import CHECKS, exit_code, find_root, render_text, run_checks
from tac.github_cli import github_group
from tac.handoff_cli import handoff_group
from tac.hook_cli import hook_group
from tac.human_cli import approve_command, human_group, recap_command
from tac.inbox_cli import inbox_group
from tac.launch_cli import launch_command
from tac.memory_cli import memory_group, select_command
from tac.pipeline_cli import pipeline_group
from tac.proof_cli import proof_group
from tac.run_cli import run_command
from tac.runner_cli import receipt_group, runner_group
from tac.session_cli import session_group
from tac.sync_cli import check_command, sync_command
from tac.usage_cli import usage_group
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


cli.add_command(config_group)
cli.add_command(explain)
cli.add_command(work_group)
cli.add_command(runner_group)
cli.add_command(receipt_group)
cli.add_command(github_group)
cli.add_command(sync_command)
cli.add_command(check_command)
cli.add_command(handoff_group)
cli.add_command(pipeline_group)
cli.add_command(hook_group)
cli.add_command(human_group)
cli.add_command(approve_command)
cli.add_command(recap_command)
cli.add_command(memory_group)
cli.add_command(select_command)
cli.add_command(session_group)
cli.add_command(run_command)
cli.add_command(launch_command)
cli.add_command(inbox_group)
cli.add_command(usage_group)
cli.add_command(diagrams_group)
cli.add_command(proof_group)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
