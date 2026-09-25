"""`tac work`: the work-order commands the `just work-*` recipes call.

Exit codes: 0 when the order holds, 1 when it does not yet, 2 when a file it
depends on cannot be read or a git answer cannot be had.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

import click

from tac import work
from tac.work import Bad


def _guarded(fn: Callable[..., int]) -> Callable[..., None]:
    """Every command exits with its own code; a Bad file is exit 2 and a line
    that names it, never a traceback."""

    @wraps(fn)
    def run(*args: Any, **kwargs: Any) -> None:
        try:
            code = fn(*args, **kwargs)
        except Bad as e:
            click.echo(f"work: {e}", err=True)
            code = 2
        except FileNotFoundError as e:
            click.echo(f"work: {e.filename}: not found", err=True)
            code = 2
        sys.exit(code)

    return run


def _root() -> Path:
    return work.repo_root(Path.cwd())


BASE = click.option(
    "--base",
    default=None,
    help="The branch to measure against. Defaults to [work] base in teams.toml.",
)


@click.group(name="work")
def work_group() -> None:
    """Work orders: owned files, command criteria, a review by someone else."""


@work_group.command()
@click.argument("oid", metavar="ID")
@click.option("--team", required=True)
@click.option("--title", required=True)
@click.option("--goal", default="")
@click.option("--branch", default="", help="Defaults to the current branch.")
@_guarded
def new(oid: str, team: str, title: str, goal: str, branch: str) -> int:
    """Scaffold a draft order on this branch."""
    root = _root()
    path = work.new_order(root, oid, team, title, goal, branch)
    click.echo(
        f"wrote {path.relative_to(root).as_posix()}, a draft: it loads once owns, "
        f"builder and a criterion are filled in, and guards nothing until then"
    )
    return 0


@work_group.command()
@_guarded
def validate() -> int:
    """Every order here is readable and no two active orders own the same file."""
    root = _root()
    teams = work.load_teams(root)
    orders = work.orders_in(root, teams)
    drafts: list[str] = []
    live = work.active(root, drafts)
    # A builder's check judges its own order: a clash between two orders in
    # other worktrees is theirs to settle. A checkout building none sees all.
    here = work.git(root, "branch", "--show-current")
    mine = {o.id for o in orders if o.branch == here} or None
    clash = work.collisions(live, mine)
    for line in clash:
        click.echo(f"collision: {line}")
    for line in work.collisions(live):
        if line not in clash:
            click.echo(f"collision elsewhere, not this branch's: {line}")
    for line in drafts:
        click.echo(f"draft elsewhere, not judged: {line}")
    for team in teams.teams:
        if team.expired():
            click.echo(f"warning: team {team.id} expired on {team.expires}")
    for order in orders:
        for line in work.online(order):
            click.echo(f"warning: {line}")
        if order.unknown:
            click.echo(f"warning: {order.id}: unknown keys {', '.join(order.unknown)}")
    click.echo(
        f"{len(orders)} orders read, {len(clash)} collisions this checkout answers for"
    )
    return 1 if clash else 0


@work_group.command()
@click.argument("oid", metavar="ID")
@BASE
@click.option("--force", is_flag=True, help="Measure again even if cached.")
@_guarded
def check(oid: str, base: str | None, force: bool) -> int:
    """Ownership, then every criterion's command."""
    root = _root()
    result = work.run_check(work.find(oid, root), work.base_of(root, base), force)
    click.echo(work.show(result))
    return 0 if result["ok"] else 1


@work_group.command()
@click.argument("oid", metavar="ID")
@BASE
@_guarded
def packet(oid: str, base: str | None) -> int:
    """What a reviewer reads, and the commit to put in review.toml."""
    root = _root()
    ref = work.base_of(root, base)
    order = work.find(oid, root)
    click.echo(work.show(work.run_check(order, ref)))
    click.echo("\nwhat changed:")
    click.echo(work.git(root, "diff", "--stat", f"{ref}...HEAD"))
    click.echo(f'\nreviewed = "{work.git(root, "rev-parse", "HEAD")}"')
    click.echo(
        f"write your ruling to {order.rel}review.toml (work/templates/review.toml)"
    )
    return 0


@work_group.command()
@click.argument("oid", metavar="ID")
@BASE
@_guarded
def review(oid: str, base: str | None) -> int:
    """The review stage's gate: current, by someone else, other provider, accept."""
    root = _root()
    why = work.review_gate(work.find(oid, root), work.base_of(root, base))
    for line in why:
        click.echo(f"not yet: {line}")
    if not why:
        click.echo(f"{oid}: reviewed by someone else, current, accepted.")
    return 1 if why else 0


@work_group.command()
@click.argument("oid", metavar="ID")
@BASE
@_guarded
def accept(oid: str, base: str | None) -> int:
    """Checks, review and sign-offs together: is it ready to land."""
    root = _root()
    why = work.acceptance(work.find(oid, root), work.base_of(root, base))
    for line in why:
        click.echo(f"not yet: {line}")
    if not why:
        click.echo(f"{oid}: checked, reviewed, signed off. Ready to land.")
    return 1 if why else 0


@work_group.command()
@click.argument("oid", metavar="ID")
@BASE
@_guarded
def repair(oid: str, base: str | None) -> int:
    """One bounded builder turn on the failing criteria, then the check again."""
    root = _root()
    result, note = work.repair(work.find(oid, root), work.base_of(root, base))
    click.echo(f"repair: {note}")
    click.echo(work.show(result))
    return 0 if result["ok"] else 1


@work_group.command()
@click.argument("goal")
@_guarded
def plan(goal: str) -> int:
    """A goal as launch groups: needs first, disjoint files, the agent budget."""
    groups, blocked = work.plan(goal, _root())
    for n, group in enumerate(groups, 1):
        click.echo(f"group {n}")
        for o in group:
            click.echo(f"  {o.id:<28} {o.team:<9} {', '.join(o.owns)}")
    if blocked:
        click.echo("blocked, until what they need has landed")
        for oid, waits in blocked.items():
            click.echo(f"  {oid} blocked by {', '.join(waits)}")
    return 0


@work_group.command()
@_guarded
def board() -> int:
    """Every active order in every worktree."""
    root = _root()
    done = work.landed(root)
    click.echo(f"{'order':<28} {'team':<9} {'branch':<34} {'checks':<8} review")
    rows = work.active(root)
    for order in rows:
        ok = (work.last_result(order) or {}).get("ok")
        checks = {True: "pass", False: "FAIL", None: "not run"}[ok]
        reviewed = "yes" if (order.dir / "review.toml").is_file() else "no"
        click.echo(
            f"{order.id:<28} {order.team:<9} {order.branch:<34} {checks:<8} {reviewed}"
        )
    click.echo(f"{len(rows)} active, {len(done)} landed")
    return 0


@work_group.command()
@BASE
@click.option("--head", default="", help="The pull request's branch.")
@_guarded
def ci(base: str | None, head: str) -> int:
    """On a pull request: the orders it carries are readable, reviewed, inside.

    The orders it carries are those whose folder it touches and those written
    for its branch, so leaving the folder alone hides nothing. An order is built
    when the pull request touches a file it owns, or when it names a builder and
    the pull request ships code; one with no builder and none of its files
    touched is a plan, and a plan needs no review."""
    root = _root()
    ref = work.base_of(root, base)
    files = work.diff_names(root, ref)
    orders = {o.id: o for o in work.orders_in(root, work.load_teams(root))}
    prefix = work.ORDERS + "/"
    carried = {n.split("/")[2] for n in files if n.startswith(prefix)}
    head = (
        head
        or os.environ.get("GITHUB_HEAD_REF")
        or work.git(root, "branch", "--show-current")
    )
    done = work.landed(root, ref)
    carried |= {
        o.id for o in orders.values() if head and o.branch == head and o.id not in done
    }
    # A reference order is the format's worked example: read above, never judged.
    carried = {oid for oid in carried if oid in orders and not orders[oid].reference}
    ships_code = any(not n.startswith("work/") for n in files)
    bad: list[str] = []
    for oid in sorted(carried):
        order = orders[oid]
        touches = any(work.covers(o, n) for n in files for o in order.owns)
        built = touches or (bool(order.builder) and ships_code)
        if built:
            bad += [f"{oid}: {w}" for w in work.acceptance(order, ref, run=False)]
        if not built and order.branch != head:
            continue
        try:
            own = work.own_files(order, ref, head)
        except Bad as e:
            bad.append(f"{oid}: {e}")
            continue
        bad += [
            f"{oid}: {s} is outside what it owns" for s in own if not order.may_touch(s)
        ]
    for line in bad:
        click.echo(f"work: {line}")
    click.echo(f"work: {len(carried)} orders in this pull request, {len(bad)} problems")
    return 1 if bad else 0


@work_group.command()
@_guarded
def sweep() -> int:
    """At a release: remove the orders that landed."""
    gone = work.sweep(_root())
    click.echo(f"swept {len(gone)} landed orders: {', '.join(gone) or 'none'}")
    return 0


@work_group.command()
@click.argument("oid", metavar="ID")
@_guarded
def post(oid: str) -> int:
    """Write or refresh the order's one status comment on its issue."""
    root = _root()
    order = work.find(oid, root)
    issue = work.issue_of(order)
    try:
        ruling = work.load_review(order)
    except Bad:
        ruling = None
    body = work.status_comment(order, work.last_result(order), ruling)
    mine = [c for c in work.comments(root, issue) if work.is_status(c, order.id)]
    if mine and mine[0]["body"].strip() == body.strip():
        click.echo(f"#{issue}: status of {order.id} is current")
    elif mine:
        path = f"repos/{{owner}}/{{repo}}/issues/comments/{mine[0]['id']}"
        work.gh(root, path, "-X", "PATCH", body=body)
        click.echo(f"#{issue}: status of {order.id} updated")
    else:
        work.gh(root, f"repos/{{owner}}/{{repo}}/issues/{issue}/comments", body=body)
        click.echo(f"#{issue}: status of {order.id} posted")
    return 0


@work_group.command()
@click.argument("oid", metavar="ID")
@_guarded
def thread(oid: str) -> int:
    """Read the order's issue: owner and collaborators only."""
    root = _root()
    order = work.find(oid, root)
    number = work.issue_of(order)
    issue = json.loads(work.gh(root, f"repos/{{owner}}/{{repo}}/issues/{number}"))
    if issue.get("author_association") in work.TRUSTED:
        click.echo(
            f"=== #{number} {issue.get('title', '')}\n"
            + str(issue.get("body") or "").strip()
        )
    keep, held = work.trusted(work.comments(root, number))
    for c in keep:
        click.echo(f"--- {c['user']['login']} at {c['created_at']}")
        click.echo(c["body"].strip())
    click.echo(
        f"--- {len(keep)} comments; {held} from people who cannot push here, not shown"
    )
    return 0


@work_group.command()
@click.argument("oid", metavar="ID")
@click.option("--role", required=True)
@click.argument("text")
@_guarded
def say(oid: str, role: str, text: str) -> int:
    """Say something on the order's issue in a role."""
    root = _root()
    order = work.find(oid, root)
    body = f"**{role}** on order `{order.id}`:\n\n{text.strip()}\n"
    path = f"repos/{{owner}}/{{repo}}/issues/{work.issue_of(order)}/comments"
    work.gh(root, path, body=body)
    click.echo(f"#{order.issue}: said as {role}")
    return 0


@work_group.command()
@click.argument("which", type=click.Choice(["pre-tool", "stop"]))
def hook(which: str) -> None:
    """What a harness hook calls with the event on stdin. Whatever arrives, it
    answers 0 or 2: a traceback would read as a broken harness to every agent
    in the session."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
        if not isinstance(event, dict) or not isinstance(
            event.get("tool_input") or {}, dict
        ):
            sys.exit(0)
        handler = work.hook_pre_tool if which == "pre-tool" else work.hook_stop
        code, why = handler(event)
    except Exception as e:  # a hook must not fall over
        click.echo(f"work hook let go: {type(e).__name__}: {e}", err=True)
        sys.exit(0)
    if why:
        click.echo(why, err=True)
    sys.exit(code)


if __name__ == "__main__":
    work_group()
