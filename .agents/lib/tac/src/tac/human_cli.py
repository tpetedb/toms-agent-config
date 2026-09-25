"""`tac human` and `tac approve`: the owner's queue, its page, recaps and approvals."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import click

from tac import approvals, human
from tac.doctor import find_root
from tac.github import agent_identity, gh_transport
from tac.receipts import (
    ReceiptError,
    git,
    load_public_key,
    trusted_key_from_revision,
)
from tac.runner import (
    RunnerError,
    controller_store,
    load_key,
    origin_repository,
    repo_top,
)

# Exit codes of `tac human verify`: 1 is kept for a refusal, as everywhere.
EXIT_WAITING = 3
EXIT_REJECTED = 4

ROOT = click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root. Defaults to the nearest ancestor holding .git.",
)
AT = click.option(
    "--at",
    default=None,
    help="The UTC time to record, YYYY-MM-DDTHH:MM:SSZ; defaults to now.",
)


def _root(root: Path | None) -> Path:
    return root.resolve() if root else find_root(Path.cwd())


def _now(at: str | None) -> dt.datetime:
    if at is None:
        return human.utc_now()
    try:
        return human.parse_utc(at)
    except ValueError as exc:
        raise click.BadParameter(f"{at!r} is not YYYY-MM-DDTHH:MM:SSZ") from exc


def _fail(message: str) -> None:
    click.echo(f"tac: {message}", err=True)
    raise SystemExit(1)


def _paths(root: Path | None) -> human.HumanPaths:
    try:
        return human.human_paths(_root(root))
    except human.HumanError as exc:
        _fail(str(exc))
        raise


def _rerender(paths: human.HumanPaths) -> None:
    try:
        target = human.write_page(paths.root)
    except human.HumanError as exc:
        _fail(f"the item is saved, but the page did not render: {exc}")
        return
    click.echo(f"rendered {target.relative_to(paths.root).as_posix()}")


@click.group("human")
def human_group() -> None:
    """The owner's queue: .human/approvals/<id>.json, rendered into TODO.HUMAN.md."""


@human_group.command("ask")
@ROOT
@AT
@click.option(
    "--id", "item_id", default=None, help="The item id; defaults to the next Q number."
)
@click.option(
    "--kind",
    type=click.Choice(["question", "approval", "step"]),
    default="question",
    show_default=True,
    help="question: a decision; approval: consent to an action; step: a one-time task.",
)
@click.option(
    "--topic",
    default=None,
    help="The section of the page, by its id in .human/todo.toml.",
)
@click.option("--title", default=None, help="A few words naming the item.")
@click.option(
    "--question", default=None, help="The exact question or action, one line."
)
@click.option("--option", "options", multiple=True, help="One option; repeat for each.")
@click.option("--recommendation", default=None, help="The board's recommendation.")
@click.option(
    "--recommendation-source",
    default=None,
    help="Where the recommendation comes from when it is not the board's.",
)
@click.option("--blocks", multiple=True, help="What the item blocks; repeat for each.")
@click.option("--waits", default=None, help="The milestone that waits on the answer.")
@click.option(
    "--env-var", default=None, help="The agents.env name a secret goes under."
)
@click.option(
    "--detail", "details", multiple=True, help="A line shown before the recommendation."
)
@click.option("--pipeline", default=None, help="The pipeline of the paused run.")
@click.option("--stage", default=None, help="The stage that waits on the answer.")
@click.option("--agent", default=None, help="The agent that asks.")
@click.option("--tool", default=None, help="The tool or command an approval allows.")
@click.option(
    "--arguments",
    "arguments_json",
    default=None,
    help="The tool's arguments as a JSON object; an approval binds them.",
)
@click.option("--revision", default=None, help="The commit an approval is for.")
@click.option(
    "--pr",
    "pull_request",
    type=int,
    default=None,
    help="The pull request whose head it is.",
)
@click.option(
    "--expires-in",
    "expires_in",
    type=click.IntRange(min=1),
    default=None,
    help="Hours until the question lapses; a lapse is never consent.",
)
@click.option(
    "--from-handoff",
    "from_handoff",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Queue the <envelope>.human.json `tac handoff validate` wrote; it carries "
    "its own question, options and recommendation, so no other item flag is taken.",
)
def human_ask(
    root: Path | None,
    at: str | None,
    item_id: str | None,
    kind: str,
    topic: str,
    title: str | None,
    question: str,
    options: tuple[str, ...],
    recommendation: str | None,
    recommendation_source: str | None,
    blocks: tuple[str, ...],
    waits: str | None,
    env_var: str | None,
    details: tuple[str, ...],
    pipeline: str | None,
    stage: str | None,
    agent: str | None,
    tool: str | None,
    arguments_json: str | None,
    revision: str | None,
    pull_request: int | None,
    expires_in: int | None,
    from_handoff: Path | None,
) -> None:
    """Add an item with its recommendation, what it blocks and the milestone
    that waits on it, then render the page."""
    paths = _paths(root)
    now = _now(at)
    if from_handoff is not None:
        ctx = click.get_current_context()
        taken = sorted(
            f"--{name.replace('_', '-')}"
            for name, value in ctx.params.items()
            if name not in {"root", "at", "item_id", "from_handoff", "kind"}
            and value not in (None, ())
        )
        if taken or kind != "question":
            _fail(
                "--from-handoff carries the whole item; drop "
                + ", ".join(taken or ["--kind"])
            )
        _ask_from_handoff(paths, from_handoff, item_id)
        return
    if topic is None or question is None:
        _fail("pass --topic and --question, or --from-handoff")
        return
    if kind != "step":
        missing = [
            flag
            for flag, value in (
                ("--recommendation", recommendation),
                ("--blocks", blocks),
                ("--waits", waits),
            )
            if not value
        ]
        if missing:
            _fail(f"a {kind} needs {', '.join(missing)}")
    if kind == "approval" and (tool is None or revision is None):
        _fail("an approval binds an action and a revision: pass --tool and --revision")
    arguments: object = {}
    if arguments_json is not None:
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError as exc:
            _fail(f"--arguments is not JSON: {exc}")
        if not isinstance(arguments, dict):
            _fail("--arguments must be a JSON object")
    try:
        items = human.load_items(paths)
        body = {
            "id": item_id or human.next_question_id(items),
            "kind": kind,
            "topic": topic,
            "rank": human.next_rank(items, topic),
            "title": title,
            "question": question,
            "details": list(details),
            "options": list(options),
            "env_var": env_var,
            "recommendation": recommendation,
            "recommendation_source": recommendation_source,
            "blocks": list(blocks),
            "waits": waits,
            "pipeline": pipeline,
            "stage": stage,
            "agent": agent,
            "tool": tool,
            "arguments": arguments,
            "revision": revision,
            "pull_request": pull_request,
            "expires_at": (
                human.utc_text(now + dt.timedelta(hours=expires_in))
                if expires_in
                else None
            ),
            "asked_at": human.utc_text(now),
        }
        try:
            item = human.Item.model_validate(body)
        except human.ValidationError as exc:
            raise human.HumanError(human.first_error(exc)) from exc
        path = human.ask(paths, item)
    except human.HumanError as exc:
        _fail(str(exc))
        return
    click.echo(f"asked {item.id} in {path.relative_to(paths.root).as_posix()}")
    _rerender(paths)


def _ask_from_handoff(
    paths: human.HumanPaths, source: Path, item_id: str | None
) -> None:
    try:
        item = human.item_from_handoff(
            source.read_text(encoding="utf-8"),
            source.name,
            human.load_items(paths),
            item_id=item_id,
        )
        path = human.ask(paths, item)
    except (human.HumanError, OSError) as exc:
        _fail(str(exc))
        return
    click.echo(f"asked {item.id} in {path.relative_to(paths.root).as_posix()}")
    _rerender(paths)


@human_group.command("answer")
@ROOT
@AT
@click.argument("item_id")
@click.option("--text", required=True, help="The owner's answer, one line.")
@click.option(
    "--by",
    "by",
    default=None,
    help="The owner's login; defaults to the repository owner.",
)
@click.option(
    "--basis",
    type=click.Choice(["owner", "owner-via-chief", "github-comment", "github-review"]),
    default="owner-via-chief",
    show_default=True,
    help="How the answer reached the queue.",
)
@click.option(
    "--link", default=None, help="The GitHub comment or review, when there is one."
)
@click.option(
    "--keep-open", is_flag=True, help="Record the answer but leave the item open."
)
def human_answer(
    root: Path | None,
    at: str | None,
    item_id: str,
    text: str,
    by: str | None,
    basis: str,
    link: str | None,
    keep_open: bool,
) -> None:
    """Record the owner's answer under the item, then render the page.

    A question or a step closes; an approval stays pending, since only the
    owner's GitHub review or `tac approve` on the host counts as consent.
    """
    paths = _paths(root)
    now = _now(at)
    try:
        who = by or origin_repository(paths.root).split("/")[0]
    except RunnerError as exc:
        _fail(f"{exc}; pass --by")
        return
    try:
        try:
            entry = human.Answer.model_validate(
                {
                    "at": human.utc_text(now),
                    "by": who,
                    "basis": basis,
                    "text": text,
                    "link": link,
                }
            )
        except human.ValidationError as exc:
            raise human.HumanError(human.first_error(exc)) from exc
        item = human.answer(
            paths,
            item_id,
            entry,
            agent_identity=agent_identity(paths.root),
            close=not keep_open,
        )
    except human.HumanError as exc:
        _fail(str(exc))
        return
    if item.kind == "approval":
        click.echo(
            f"recorded under {item.id}; it stays pending until the owner's review "
            "or `tac approve` on the host"
        )
    else:
        click.echo(f"recorded under {item.id}; it is {item.status}")
    _rerender(paths)


@human_group.command("render")
@ROOT
@click.option(
    "--check", is_flag=True, help="Write nothing; exit 1 when the page differs."
)
def human_render(root: Path | None, check: bool) -> None:
    """Render TODO.HUMAN.md from .human/approvals/ and .human/todo.toml."""
    base = _root(root)
    try:
        paths, text = human.render_page(base)
    except human.HumanError as exc:
        _fail(str(exc))
        return
    if check:
        current = (
            paths.todo_file.read_text(encoding="utf-8")
            if paths.todo_file.is_file()
            else ""
        )
        if current != text:
            _fail(
                f"{paths.todo} differs from its render; run `tac human render` and "
                "never edit it by hand"
            )
        click.echo(f"{paths.todo} matches its render")
        return
    paths.todo_file.write_text(text, encoding="utf-8")
    click.echo(f"rendered {paths.todo}")


@human_group.command("recap")
@ROOT
@AT
@click.option(
    "--from",
    "source",
    type=click.File("r", encoding="utf-8"),
    required=True,
    help=f"A payload in the recap contract, {human.RECAP_CONTRACT}; - reads stdin.",
)
@click.option(
    "--slug",
    default=None,
    help="The name after the time; defaults to one from the title.",
)
def human_recap(
    root: Path | None, at: str | None, source: object, slug: str | None
) -> None:
    """Write .human/recap/<UTC basic datetime>-<slug>.md: shipped, in flight,
    decisions, then what the owner must do, under 400 words."""
    paths = _paths(root)
    now = _now(at)
    try:
        recap = human.read_recap(paths.root, source.read())  # type: ignore[attr-defined]
        path = human.write_recap(paths, recap, now, slug)
    except human.HumanError as exc:
        _fail(str(exc))
        return
    click.echo(f"wrote {path.relative_to(paths.root).as_posix()}")


@human_group.command("verify")
@ROOT
@click.argument("item_id")
@click.option(
    "--base",
    default="origin/main",
    show_default=True,
    help="The trusted revision whose runner.pub judges a signed approval.",
)
@click.option(
    "--pub",
    "pub",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="A separately provisioned runner.pub, in place of --base.",
)
@click.option(
    "--head",
    default="HEAD",
    show_default=True,
    help="The revision about to be acted on.",
)
@click.option(
    "--repo",
    "repository",
    default=None,
    help="owner/name; defaults to the origin remote.",
)
def human_verify(
    root: Path | None,
    item_id: str,
    base: str,
    pub: Path | None,
    head: str,
    repository: str | None,
) -> None:
    """Whether the item carries a verified answer. Exit 0 approved, 3 waiting,
    4 rejected, 1 refused: a forged, foreign, self-authored or stale approval."""
    paths = _paths(root)
    now = human.utc_now()
    try:
        item = human.load_item(paths, item_id)
        repository = repository or origin_repository(paths.root)
        done = git(paths.root, "rev-parse", "--verify", "--quiet", f"{head}^{{commit}}")
        if done.returncode != 0:
            raise human.HumanError(f"{head!r} is not a commit here")
        head_sha = done.stdout.strip()
        bot = agent_identity(paths.root)
        if item.approval is None and item.pull_request is not None:
            transport = gh_transport()
            if transport is None:
                raise human.HumanError("gh is not installed; install it and log in")
            verdict = approvals.verify_github(
                item,
                transport,
                repository=repository,
                head=head_sha,
                agent_identity=bot,
                now=now,
            )
        else:
            trusted = (
                load_public_key(pub.read_text(encoding="utf-8"))
                if pub is not None
                else trusted_key_from_revision(paths.root, base)
            )
            verdict = approvals.verify_signed(
                item,
                trusted,
                repository=repository,
                head=head_sha,
                agent_identity=bot,
                now=now,
            )
    except (human.HumanError, RunnerError, ReceiptError) as exc:
        _fail(str(exc))
        return
    click.echo(f"{item.id}: {verdict.state}: {verdict.reason}")
    codes = {"approved": 0, "waiting": EXIT_WAITING, "rejected": EXIT_REJECTED}
    raise SystemExit(codes.get(verdict.state, 1))


@human_group.command("schema")
@ROOT
@click.option("--write", is_flag=True, help=f"Write {human.ITEM_CONTRACT}.")
def human_schema(root: Path | None, write: bool) -> None:
    """Print, or write, the item contract generated from its model."""
    text = human.schema_text(human.item_json_schema())
    if not write:
        click.echo(text, nl=False)
        return
    path = _root(root) / human.ITEM_CONTRACT
    if not path.is_file() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
        click.echo(f"wrote {human.ITEM_CONTRACT}")


@click.command("approve")
@ROOT
@click.argument("item_id")
@click.option("--reject", is_flag=True, help="Sign a rejection instead of an approval.")
@click.option(
    "--expires-in",
    "expires_in",
    type=click.IntRange(min=1, max=24 * 30),
    default=approvals.DEFAULT_TTL_HOURS,
    show_default=True,
    help="Hours the approval holds, never past the item's own expiry.",
)
def approve_command(
    root: Path | None, item_id: str, reject: bool, expires_in: int
) -> None:
    """Host only: sign the owner's decision on an approval item with the runner key.

    Refused inside a sandbox and inside an agent session. The signature binds
    the item, its action and arguments, the revision, the repository and an
    expiry; the record goes into the item and a copy into the controller store.
    """
    reason = approvals.host_refusal(os.environ)
    if reason is not None:
        _fail(
            f"refused: {reason}. tac approve signs the owner's consent and runs "
            "only in the owner's own terminal on the host"
        )
    paths = _paths(root)
    try:
        top = repo_top(paths.root)
        store = controller_store(top, os.environ)
        private = load_key(store)
        repository = origin_repository(top)
        item = approvals.approve(
            paths,
            item_id,
            private=private,
            store=store,
            repository=repository,
            decided_by=repository.split("/")[0],
            agent_identity=agent_identity(paths.root),
            decision="rejected" if reject else "approved",
            now=human.utc_now(),
            ttl_hours=expires_in,
        )
    except (approvals.ApprovalError, human.HumanError, RunnerError) as exc:
        _fail(str(exc))
        return
    record = item.approval.approval if item.approval else None
    click.echo(
        f"{item.id} {item.status}, signed with key "
        f"{record.key_id if record else '?'} until "
        f"{record.expires_at if record else '?'}"
    )
    _rerender(paths)


# Design sections 7 and 13 name a top-level `tac recap`; it is the same command.
recap_command = human_recap
