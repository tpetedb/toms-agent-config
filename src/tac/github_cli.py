"""`tac github`: the ruleset and the labels, applied by the owner on the host,
and the lint of the issue forms and the pull request template."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import click

from tac.doctor import find_root
from tac.forms import lint
from tac.github import (
    AGENTS_CONFIG,
    CODEOWNERS,
    AnonymousTransport,
    ApiError,
    agent_identity,
    apply_ruleset,
    desired_ruleset,
    gh_transport,
)
from tac.labels import (
    apply_labels,
    fetch_labels,
    labels_problems,
    load_labels,
    plan_labels,
)
from tac.runner import RunnerError, agent_session, origin_repository
from tac.work import Bad

LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
# The name goes into an API path, so a dot segment is never a repository.
REPO_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.\.?$)[A-Za-z0-9_.-]{1,100}$"
)


ROOT = click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)


@click.group("github")
def github_group() -> None:
    """The GitHub gate: ruleset, labels and forms (design section 8)."""


def _refuse_agent_session(what: str) -> None:
    reason = agent_session(os.environ)
    if reason is not None:
        raise click.ClickException(
            f"refused inside an agent session ({reason}): {what} uses the "
            "owner's token and runs only in the owner's own terminal on the host"
        )


def _repository(base: Path, repo: str | None) -> str:
    if repo is None:
        try:
            repo = origin_repository(base)
        except RunnerError as exc:
            raise click.ClickException(str(exc)) from exc
    if not REPO_NAME.match(repo):
        raise click.ClickException(f"not owner/name: {repo!r}")
    return repo


@github_group.command("apply")
@click.option(
    "--repo",
    "repo",
    default=None,
    help="owner/name; defaults to the origin remote of this checkout.",
)
@click.option(
    "--bot",
    default=None,
    help=(
        "The machine account agents push as: write access, never admin. Defaults "
        f"to [governance] agent_identity in {AGENTS_CONFIG}, else tac-bot (Q14)."
    ),
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
def github_apply(
    repo: str | None, bot: str | None, dry_run: bool, root: Path | None
) -> None:
    """Check the agent identity, then create or update the ruleset and judge it.

    Bypass is off for everyone, the owner included. Uses the owner's own gh
    login, so it runs only in the owner's terminal, never in an agent session.
    """
    body = desired_ruleset()
    if dry_run:
        click.echo(json.dumps(body, indent=2))
        return
    _refuse_agent_session("tac github apply")
    base = root.resolve() if root else find_root(Path.cwd())
    if bot is None:
        bot = agent_identity(base)
    if not LOGIN.match(bot):
        raise click.ClickException(f"not a GitHub login: {bot!r}")
    if not (base / CODEOWNERS).is_file():
        raise click.ClickException(
            f"{CODEOWNERS} is missing; code-owner review would require nobody"
        )
    repo = _repository(base, repo)
    transport = gh_transport()
    if transport is None:
        raise click.ClickException("gh is not installed; install it and log in")
    outcome = apply_ruleset(transport, repo, bot, body)
    for line in outcome.lines:
        click.echo(line)
    raise SystemExit(0 if outcome.ok else 1)


@github_group.command("labels")
@click.option(
    "--repo",
    "repo",
    default=None,
    help="owner/name; defaults to the origin remote of this checkout.",
)
@click.option(
    "--dry-run/--apply",
    "dry_run",
    default=True,
    help=(
        "--dry-run (the default) reads the public label list with no credential "
        "and prints the plan; --apply creates and updates with the owner's gh."
    ),
)
@ROOT
def github_labels(repo: str | None, dry_run: bool, root: Path | None) -> None:
    """Bring the repository's labels to .github/labels.yml; never deletes one.

    --apply is the owner's step: it uses the owner's own gh login, so it runs
    only in the owner's terminal on the host, never in an agent session.
    """
    base = root.resolve() if root else find_root(Path.cwd())
    try:
        declared = load_labels(base)
    except Bad as e:
        raise click.ClickException(str(e)) from None
    problems = labels_problems(base, declared)
    if problems:
        raise click.ClickException("\n".join(problems))
    if dry_run:
        repo = _repository(base, repo)
        try:
            plan = plan_labels(declared, fetch_labels(AnonymousTransport(), repo))
        except ApiError as e:
            click.echo(f"could not read the labels of {repo} ({e}); declared:")
            for label in declared:
                click.echo(f"  {label.name} #{label.color}  {label.description}")
            return
        for line in plan.lines():
            click.echo(line)
        click.echo(
            "nothing to apply"
            if plan.settled
            else "dry run: nothing changed; the owner applies with --apply"
        )
        return
    _refuse_agent_session("tac github labels --apply")
    repo = _repository(base, repo)
    transport = gh_transport()
    if transport is None:
        raise click.ClickException("gh is not installed; install it and log in")
    outcome = apply_labels(transport, repo, declared)
    for line in outcome.lines:
        click.echo(line)
    raise SystemExit(0 if outcome.ok else 1)


@github_group.command("lint")
@ROOT
def github_lint(root: Path | None) -> None:
    """Judge the labels, the issue forms, config.yml and the pull request
    template offline; exit 1 naming each problem."""
    base = root.resolve() if root else find_root(Path.cwd())
    problems = lint(base)
    if problems:
        for line in problems:
            click.echo(line, err=True)
        raise SystemExit(1)
    click.echo("labels, issue forms and the pull request template hold")
