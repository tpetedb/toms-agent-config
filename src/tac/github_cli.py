"""`tac github`: apply the default-branch ruleset, run by the owner on the host."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import click

from tac.doctor import find_root
from tac.github import (
    CODEOWNERS,
    apply_ruleset,
    desired_ruleset,
    gh_transport,
)
from tac.runner import RunnerError, agent_session, origin_repository

LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
# The name goes into an API path, so a dot segment is never a repository.
REPO_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.\.?$)[A-Za-z0-9_.-]{1,100}$"
)
# Q14's recommendation; the owner names the real account with --bot.
DEFAULT_BOT = "tac-bot"


@click.group("github")
def github_group() -> None:
    """The GitHub gate: the ruleset on the default branch (design section 8)."""


@github_group.command("apply")
@click.option(
    "--repo",
    "repo",
    default=None,
    help="owner/name; defaults to the origin remote of this checkout.",
)
@click.option(
    "--bot",
    default=DEFAULT_BOT,
    show_default=True,
    help="The machine account agents push as: write access, never admin.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the ruleset that would be applied; call nothing.",
)
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)
def github_apply(repo: str | None, bot: str, dry_run: bool, root: Path | None) -> None:
    """Check the agent identity, then create or update the ruleset and judge it.

    Bypass is off for everyone, the owner included. Uses the owner's own gh
    login, so it runs only in the owner's terminal, never in an agent session.
    """
    body = desired_ruleset()
    if dry_run:
        click.echo(json.dumps(body, indent=2))
        return
    reason = agent_session(os.environ)
    if reason is not None:
        raise click.ClickException(
            f"refused inside an agent session ({reason}): tac github apply uses the "
            "owner's token and runs only in the owner's own terminal on the host"
        )
    if not LOGIN.match(bot):
        raise click.ClickException(f"not a GitHub login: {bot!r}")
    base = root.resolve() if root else find_root(Path.cwd())
    if not (base / CODEOWNERS).is_file():
        raise click.ClickException(
            f"{CODEOWNERS} is missing; code-owner review would require nobody"
        )
    if repo is None:
        try:
            repo = origin_repository(base)
        except RunnerError as exc:
            raise click.ClickException(str(exc)) from exc
    if not REPO_NAME.match(repo):
        raise click.ClickException(f"not owner/name: {repo!r}")
    transport = gh_transport()
    if transport is None:
        raise click.ClickException("gh is not installed; install it and log in")
    outcome = apply_ruleset(transport, repo, bot, body)
    for line in outcome.lines:
        click.echo(line)
    raise SystemExit(0 if outcome.ok else 1)
