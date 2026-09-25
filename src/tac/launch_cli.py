"""`tac launch`: start a role's Claude Code or Codex session as the runner would."""

from __future__ import annotations

import os
import shlex
import sys
import uuid
from pathlib import Path

import click

from tac import launch
from tac.config import load_config
from tac.doctor import find_root
from tac.work import Bad


@click.command("launch")
@click.argument("harness", type=click.Choice(launch.HARNESSES))
@click.option("--role", default=None, help="The role charter the session runs as.")
@click.option(
    "--order", "order_id", default=None, help="Start in this order's worktree."
)
@click.option("--headless", is_flag=True, help="Run one non-interactive turn.")
@click.option(
    "--contract", default=None, help="Headless: the contract the result meets."
)
@click.option(
    "--prompt-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Headless: the rendered prompt, sent on stdin.",
)
@click.option("--session-id", default=None, help="Headless: the session id to use.")
@click.option("--max-turns", type=click.IntRange(min=1), default=50, show_default=True)
@click.option(
    "--max-budget-usd", type=click.FloatRange(min=0.01), default=5.0, show_default=True
)
@click.option(
    "--print",
    "print_only",
    is_flag=True,
    help="Print the command and exit 0, even when the client is not installed.",
)
@click.option(
    "--allow-drift",
    is_flag=True,
    help="Start although generated files differ from origin/main; interactive only.",
)
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)
def launch_command(
    harness: launch.Harness,
    role: str | None,
    order_id: str | None,
    headless: bool,
    contract: str | None,
    prompt_file: Path | None,
    session_id: str | None,
    max_turns: int,
    max_budget_usd: float,
    print_only: bool,
    allow_drift: bool,
    root: Path | None,
) -> None:
    """Build and start HARNESS for a role: `claude --agent <role>` with the seat's
    effort, or a headless `claude -p` / `codex exec` bound to a contract. The
    generated surfaces are compared with origin/main's lock first."""
    base = root.resolve() if root else find_root(Path.cwd())
    spec = None
    if headless:
        if contract is None or prompt_file is None:
            raise click.UsageError("--headless needs --contract and --prompt-file")
        spec = launch.Headless(
            contract=contract,
            prompt_file=prompt_file,
            session_id=session_id or str(uuid.uuid4()),
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
        )
    try:
        config = load_config(base)
        planned = launch.plan(
            base,
            config,
            harness,
            role=role,
            order_id=order_id,
            headless=spec,
            environ=os.environ,
            search_path=os.environ.get("PATH", ""),
            allow_drift=allow_drift,
        )
    except (Bad, launch.LaunchError) as e:
        raise click.ClickException(str(e)) from None
    if print_only:
        click.echo(shlex.join(launch.shown(planned.argv)))
        if not planned.installed:
            click.echo(f"note: {harness} is not installed on PATH", err=True)
        for problem in planned.drift:
            click.echo(f"note: drift: {problem}", err=True)
        return
    try:
        launch.refuse_drift(planned, allow_drift)
        launch.write_records(base, config, planned)
        done = launch.run(planned, capture=False)
    except (Bad, launch.LaunchError, OSError) as e:
        raise click.ClickException(str(e)) from None
    sys.exit(done.returncode)
