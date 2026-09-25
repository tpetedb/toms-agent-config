"""`tac inbox watch`: the runner performs the effects the worker inbox asks for."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import click

from tac import inbox
from tac.config import load_config
from tac.effects import Effects
from tac.keychain import system_keychain
from tac.run import gate_recipes
from tac.runner import RunnerError, open_runner, refuse_agent_parent, repo_top
from tac.work import Bad


@click.group("inbox")
def inbox_group() -> None:
    """Effect requests from the worker inbox, re-checked and performed by the runner."""


@inbox_group.command("watch")
@click.option(
    "--repo",
    "repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Any folder inside the repository.",
)
@click.option("--once", is_flag=True, help="Process what is there, then exit.")
@click.option(
    "--interval",
    type=click.FloatRange(min=1.0, max=3600.0),
    default=10.0,
    show_default=True,
    help="Seconds between passes when watching.",
)
def watch_command(repo: Path, once: bool, interval: float) -> None:
    """Host only: validate each new request (schema, policy, approval, lease),
    perform it through the effect journal and append the outcome to
    inbox.done.jsonl; the cursor lives in the controller store."""
    try:
        refuse_agent_parent(os.environ)
        top = repo_top(repo)
        runner = open_runner(top, os.environ, recipes=gate_recipes(top))
        config = load_config(top)
    except (RunnerError, Bad) as exc:
        raise click.ClickException(str(exc)) from None
    effects = Effects(runner, config, system_keychain(runner.store.name))
    effects.reconcile()
    stop = threading.Event()
    while True:
        for outcome in inbox.watch_once(effects, os.environ):
            click.echo(
                f"{outcome.id or '?'}: {outcome.outcome} {outcome.reason}".rstrip()
            )
        if once:
            return
        try:
            # Waits on an event, not a sleep: an interrupt ends the wait at once.
            stop.wait(interval)
        except KeyboardInterrupt:
            click.echo("tac inbox watch stopped")
            return
