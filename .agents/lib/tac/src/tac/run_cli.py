"""`tac run`: run a pipeline for an order as the trusted runner, or resume one."""

from __future__ import annotations

import os
from pathlib import Path

import click

from tac import run as runs
from tac.config import load_config
from tac.effects import Effects
from tac.keychain import system_keychain
from tac.runner import RunnerError, open_runner, refuse_agent_parent, repo_top
from tac.work import Bad, find

# Exit statuses, so a script can branch without parsing: done, waiting on the
# owner, blocked (usage, the agent budget, a missing sandbox), failed.
EXIT = {
    "complete": 0,
    "failed": 1,
    "waiting-human": 3,
    "blocked": 4,
    "cancelled": 5,
    "running": 1,
}


@click.command("run")
@click.argument("pipeline")
@click.option(
    "--order", "order_id", required=True, help="The work order the run is for."
)
@click.option(
    "--resume", "resume_id", default=None, help="Continue this run from its checkpoint."
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Check the pipeline and print the stage order; run nothing.",
)
@click.option(
    "--harness",
    type=click.Choice(["claude", "codex"]),
    default="claude",
    show_default=True,
    help="The building team's harness; a review stage runs on the other.",
)
@click.option(
    "--entry",
    "entries",
    multiple=True,
    help="An entry payload as CONTRACT=FILE, such as issue=issue.json.",
)
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Any folder inside the repository.",
)
def run_command(
    pipeline: str,
    order_id: str,
    resume_id: str | None,
    dry_run: bool,
    harness: str,
    entries: tuple[str, ...],
    repo: Path,
) -> None:
    """Host only: run PIPELINE for an order, stage by stage, checkpointing every
    transition in the controller store. Exit 0 complete, 3 waiting on the owner,
    4 blocked, 1 failed or refused."""
    try:
        top = repo_top(repo)
        config = load_config(top)
        chosen = runs.runnable(top, config, pipeline)
    except (Bad, RunnerError, runs.RunError) as exc:
        raise click.ClickException(str(exc)) from None
    if dry_run:
        for line in runs.plan_lines(chosen):
            click.echo(line)
        return
    given: dict[str, str] = {}
    for spec in entries:
        name, sep, file = spec.partition("=")
        if not sep or not Path(file).is_file():
            raise click.UsageError(f"--entry takes CONTRACT=FILE, got {spec!r}")
        given[name] = str(Path(file).resolve())
    try:
        refuse_agent_parent(os.environ)
        runner = open_runner(top, os.environ, recipes=runs.gate_recipes(top))
        try:
            order = find(order_id, top)
        except Bad:
            # An order the intake stage has yet to write: effects will refuse.
            order = None
        ctx = runs.Context(
            root=top,
            config=config,
            runner=runner,
            effects=Effects(runner, config, system_keychain(runner.store.name)),
            pipeline=chosen,
            order=order,
            environ=os.environ,
            search_path=os.environ.get("PATH", ""),
            out=click.echo,
        )
        state = (
            runs.resume(ctx, resume_id)
            if resume_id
            else runs.fresh(ctx, harness, given, order_id)  # pyright: ignore[reportArgumentType]
        )
        click.echo(f"run {state.run_id}")
        state = runs.advance(ctx, state)
    except (Bad, RunnerError, runs.RunError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"run {state.run_id}: {state.status}")
    raise SystemExit(EXIT[state.status])
