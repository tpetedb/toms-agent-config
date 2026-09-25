"""The human loop: the owner's queue, the page rendered from it, and recaps.

Design section 7. `.human/approvals/<id>.json` is the single source of truth:
one item per question, one-time step or approval, in the approval shape of the
OpenAI Agents SDK run state (pipeline, stage, agent, tool, arguments, question,
status, asked_at, decided_at, decided_by), so a paused stage resumes exactly
where it stopped. TODO.HUMAN.md is rendered from those items and the fixed text
in `.human/todo.toml`, byte for byte, so a hand edit is a red
`tac human render --check`. An item's own status is a hint for the page, never
consent: what counts as an approval is decided in tac.approvals.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from jsonschema import Draft7Validator
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from tac.config_schema import Human
from tac.draft07 import draft07
from tac.handoff import HumanItem
from tac.receipts import SHA_PATTERN
from tac.work import KNOBS_FILE

TODO_SOURCE = ".human/todo.toml"
ITEM_CONTRACT = "contracts/human-approval.schema.json"
RECAP_CONTRACT = "contracts/handoffs/recap.schema.json"
DEFAULT_TODO = "TODO.HUMAN.md"
DEFAULT_RECAP_DIR = ".human/recap"
DEFAULT_APPROVALS_DIR = ".human/approvals"
# The lines under an item sit inside its list entry.
INDENT = " " * 6
MAX_RECAP_WORDS = 400
UTC_SECONDS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
# A file name and a word in the page: never a path, never a dot segment.
ITEM_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
# A GitHub login: letters, digits and single hyphens, at most 39 characters.
LOGIN = r"^[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}$"
QUESTION_ID = re.compile(r"^Q(\d+)$")
# Escalations from `tac handoff validate` get their own numbers, apart from the
# board's questions, and land in their own section of the page.
ESCALATION_ID = re.compile(r"^H(\d+)$")
ESCALATION_TOPIC = "escalations"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
EM_DASH = "\u2014"

ItemKind = Literal["question", "approval", "step"]
Status = Literal["pending", "answered", "approved", "rejected"]
Basis = Literal[
    "owner", "owner-via-chief", "github-review", "github-comment", "host-signed"
]
Decision = Literal["approved", "rejected"]


class HumanError(Exception):
    """The queue or its page cannot be read or written; the message says why."""


def _one_line(value: str) -> str:
    """Page text: one line, printable, and in house style (no em dash)."""
    if not value.strip():
        raise ValueError("is empty")
    if EM_DASH in value:
        raise ValueError("holds an em dash; the house style has none")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("holds a line break or a control character")
    return value


Line = Annotated[str, AfterValidator(_one_line)]
Utc = Annotated[str, Field(pattern=UTC_SECONDS)]
Login = Annotated[str, Field(pattern=LOGIN)]
Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def utc_text(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


# ---------------------------------------------------------------- the models


class Answer(_Model):
    """One answer under an item, oldest first. A record, not consent."""

    at: Utc
    by: Login
    basis: Basis
    text: Line
    link: str | None = Field(default=None, pattern=r"^https://github\.com/\S+$")


class ApprovalRecord(_Model):
    """What `tac approve` signs: the decision bound to the item, its action and
    arguments, the revision it is for, the repository and an expiry."""

    schema_version: Literal[1] = 1
    approval_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    item_id: str = Field(pattern=ITEM_ID)
    decision: Decision
    action_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    revision: str | None = Field(default=None, pattern=SHA_PATTERN)
    decided_by: Login
    decided_at: Utc
    expires_at: Utc
    key_id: str = Field(pattern=r"^[0-9a-f]{16}$")


class SignedApproval(_Model):
    # extra="forbid" also refuses a record that tries to carry its own key.
    approval: ApprovalRecord
    signature: str


class Item(_Model):
    """One entry of the owner's queue, and the approval shape of section 7."""

    item_version: Literal[1] = 1
    id: str = Field(pattern=ITEM_ID)
    kind: ItemKind
    # The section of TODO.HUMAN.md, by its id in .human/todo.toml.
    topic: Name
    # The place within the section; ties fall back to the id.
    rank: int = Field(ge=0)
    title: Line | None = None
    question: Line
    details: tuple[Line, ...] = ()
    options: tuple[Line, ...] = ()
    # The agents.env variable the owner puts a secret under; never the value.
    env_var: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    recommendation: Line | None = None
    # Where the recommendation comes from when it is not the board's.
    recommendation_source: Line | None = None
    blocks: tuple[Line, ...] = ()
    waits: Line | None = None
    notes: tuple[Line, ...] = ()
    # The run state a paused stage resumes from.
    pipeline: Name | None = None
    stage: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
    )
    agent: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
    )
    tool: Line | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    # What an approval is for: the commit, and the pull request whose head it is.
    revision: str | None = Field(default=None, pattern=SHA_PATTERN)
    pull_request: int | None = Field(default=None, ge=1)
    expires_at: Utc | None = None
    status: Status = "pending"
    asked_at: Utc
    decided_at: Utc | None = None
    decided_by: Login | None = None
    answers: tuple[Answer, ...] = ()
    approval: SignedApproval | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        decided = self.decided_at is not None or self.decided_by is not None
        if self.status == "pending" and decided:
            raise ValueError("a pending item has no decided_at or decided_by")
        if self.status != "pending" and (
            self.decided_at is None or self.decided_by is None
        ):
            raise ValueError(f"status {self.status} needs decided_at and decided_by")
        if self.status in ("approved", "rejected") and self.kind != "approval":
            raise ValueError(f"only an approval item can be {self.status}")
        if self.status == "answered" and self.kind == "approval":
            raise ValueError("an approval item is approved or rejected, never answered")
        if self.approval is not None and self.kind != "approval":
            raise ValueError("only an approval item carries a signed approval")
        if self.recommendation_source is not None and self.recommendation is None:
            raise ValueError("recommendation_source without a recommendation")
        return self

    @property
    def open(self) -> bool:
        return self.status == "pending"

    def to_json(self) -> str:
        body = self.model_dump(mode="json")
        return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


class Section(_Model):
    id: Name
    heading: Line


class TodoFile(_Model):
    """`.human/todo.toml`: the fixed text of the page and its sections."""

    schema_version: Literal[1]
    title: Line
    intro: tuple[Line, ...]
    sections: tuple[Section, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [s.id for s in self.sections]
        twice = sorted({i for i in ids if ids.count(i) > 1})
        if twice:
            raise ValueError(f"sections: {', '.join(twice)} named twice")
        return self


def item_json_schema() -> dict[str, JsonValue]:
    """The item file as JSON Schema draft-07, generated from the model."""
    return draft07(Item)


def schema_text(schema: object) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "item"
    return f"{where}: {first['msg']}"


# ---------------------------------------------------------------- where things live


@dataclass(frozen=True, slots=True)
class HumanPaths:
    root: Path
    todo: str
    recap_dir: str
    approvals_dir: str

    @property
    def todo_file(self) -> Path:
        return self.root / self.todo

    @property
    def approvals(self) -> Path:
        return self.root / self.approvals_dir

    @property
    def recaps(self) -> Path:
        return self.root / self.recap_dir

    def item_file(self, item_id: str) -> Path:
        if not re.match(ITEM_ID, item_id):
            raise HumanError(f"not an item id: {item_id!r}")
        return self.approvals / f"{item_id}.json"


def human_paths(root: Path) -> HumanPaths:
    """The [human] paths of the knob file, or the defaults where it has none."""
    table: Mapping[str, Any] = {}
    knobs = root / KNOBS_FILE
    if knobs.is_file():
        try:
            table = tomllib.loads(knobs.read_text(encoding="utf-8")).get("human") or {}
        except tomllib.TOMLDecodeError as exc:
            raise HumanError(f"{KNOBS_FILE} does not parse: {exc}") from exc
    if table:
        try:
            human = Human.model_validate(table)
        except ValidationError as exc:
            raise HumanError(f"{KNOBS_FILE} [human]: {first_error(exc)}") from exc
        return HumanPaths(root, human.todo, human.recap_dir, human.approvals_dir)
    return HumanPaths(root, DEFAULT_TODO, DEFAULT_RECAP_DIR, DEFAULT_APPROVALS_DIR)


def load_todo(root: Path) -> TodoFile:
    path = root / TODO_SOURCE
    if not path.is_file():
        raise HumanError(f"{TODO_SOURCE} is missing: it holds the page's fixed text")
    try:
        return TodoFile.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
    except tomllib.TOMLDecodeError as exc:
        raise HumanError(f"{TODO_SOURCE} does not parse: {exc}") from exc
    except ValidationError as exc:
        raise HumanError(f"{TODO_SOURCE}: {first_error(exc)}") from exc


def parse_item(text: str, where: str) -> Item:
    try:
        return Item.model_validate_json(text)
    except ValidationError as exc:
        raise HumanError(f"{where}: {first_error(exc)}") from exc


def load_item(paths: HumanPaths, item_id: str) -> Item:
    path = paths.item_file(item_id)
    if not path.is_file():
        raise HumanError(f"no item {item_id} in {paths.approvals_dir}")
    item = parse_item(
        path.read_text(encoding="utf-8"), f"{paths.approvals_dir}/{path.name}"
    )
    if item.id != item_id:
        raise HumanError(f"{path.name} holds item {item.id}, not {item_id}")
    return item


def load_items(paths: HumanPaths) -> list[Item]:
    """Every item, sorted by id; one error names every file that does not read."""
    if not paths.approvals.is_dir():
        return []
    items: list[Item] = []
    problems: list[str] = []
    for path in sorted(paths.approvals.iterdir()):
        if path.name.startswith("."):
            continue
        where = f"{paths.approvals_dir}/{path.name}"
        if path.suffix != ".json" or not path.is_file():
            problems.append(f"{where}: only <id>.json files belong here")
            continue
        try:
            item = parse_item(path.read_text(encoding="utf-8"), where)
        except HumanError as exc:
            problems.append(str(exc))
            continue
        if item.id != path.stem:
            problems.append(f"{where}: holds item {item.id}; the file name is its id")
            continue
        items.append(item)
    if problems:
        raise HumanError("\n".join(problems))
    return sorted(items, key=lambda i: i.id)


def write_item(paths: HumanPaths, item: Item) -> Path:
    path = paths.item_file(item.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(item.to_json(), encoding="utf-8")
    return path


def next_question_id(items: Iterable[Item]) -> str:
    numbers = [int(m.group(1)) for i in items if (m := QUESTION_ID.match(i.id))]
    return f"Q{max(numbers, default=0) + 1}"


def next_escalation_id(items: Iterable[Item]) -> str:
    numbers = [int(m.group(1)) for i in items if (m := ESCALATION_ID.match(i.id))]
    return f"H{max(numbers, default=0) + 1}"


def next_rank(items: Iterable[Item], topic: str) -> int:
    ranks = [i.rank for i in items if i.topic == topic]
    return max(ranks, default=0) + 10


# ---------------------------------------------------------------- the page


def _latest(items: Sequence[Item]) -> str | None:
    stamps: list[str] = []
    for item in items:
        stamps.append(item.asked_at)
        if item.decided_at:
            stamps.append(item.decided_at)
        stamps.extend(a.at for a in item.answers)
        if item.approval is not None:
            stamps.append(item.approval.approval.decided_at)
    return max(stamps) if stamps else None


def provenance(paths: HumanPaths, items: Sequence[Item]) -> str:
    """When and from what the page was made, from the items alone, so the same
    queue always renders the same bytes."""
    latest = _latest(items)
    count = len(items)
    still = sum(1 for i in items if i.open)
    noun = "item" if count == 1 else "items"
    when = f", as of {latest[:10]}" if latest else ""
    return (
        f"Rendered by `tac human render` from {count} {noun} in "
        f"`{paths.approvals_dir}/` and `{TODO_SOURCE}`, {still} open{when}. "
        "Never edit this file by hand: `tac human render --check` fails on any "
        "difference."
    )


def item_lines(item: Item) -> list[str]:
    box = " " if item.open else "x"
    head = (
        f"{item.id} {item.title}: {item.question}"
        if item.title
        else (f"{item.id} {item.question}")
    )
    out = [f"- [{box}] {head}"]
    out += [INDENT + line for line in item.details]
    if item.options:
        out.append(INDENT + "Options: " + "; ".join(item.options) + ".")
    if item.env_var:
        out.append(
            INDENT + f"Secret: put it in agents.env as `{item.env_var}`, never in "
            "chat or the repository."
        )
    parts: list[str] = []
    if item.recommendation:
        label = "Recommendation"
        if item.recommendation_source:
            label += f" ({item.recommendation_source})"
        parts.append(f"{label}: {item.recommendation}")
    if item.blocks:
        parts.append("Blocks: " + "; ".join(item.blocks) + ".")
    if item.waits:
        parts.append(f"Waits: {item.waits}.")
    if parts:
        out.append(INDENT + " ".join(parts))
    out += [INDENT + line for line in item.notes]
    for answer in item.answers:
        link = f" ({answer.link})" if answer.link else ""
        out.append(
            INDENT + f"Answered {answer.at[:10]} by {answer.by}, {answer.basis}: "
            f"{answer.text}{link}"
        )
    if item.approval is not None:
        record = item.approval.approval
        what = f"revision {record.revision[:12]}" if record.revision else "no revision"
        out.append(
            INDENT + f"{record.decision.capitalize()} {record.decided_at[:10]} by "
            f"{record.decided_by}, signed on the host with key {record.key_id}, "
            f"for {what}, until {record.expires_at}."
        )
    return out


def render(paths: HumanPaths, todo: TodoFile, items: Sequence[Item]) -> str:
    """TODO.HUMAN.md: the title, the fixed paragraphs, the provenance line, then
    one section per topic with its items in rank order. Empty sections are left
    out; an item whose topic no section names is refused."""
    known = {s.id for s in todo.sections}
    stray = sorted(f"{i.id} ({i.topic})" for i in items if i.topic not in known)
    if stray:
        raise HumanError(
            f"items name a topic {TODO_SOURCE} has no section for: {', '.join(stray)}"
        )
    lines = [f"# {todo.title}", ""]
    for paragraph in todo.intro:
        lines += [paragraph, ""]
    lines += [provenance(paths, items), ""]
    for section in todo.sections:
        members = sorted(
            (i for i in items if i.topic == section.id), key=lambda i: (i.rank, i.id)
        )
        if not members:
            continue
        lines += [f"## {section.heading}", ""]
        for item in members:
            lines += item_lines(item)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_page(root: Path) -> tuple[HumanPaths, str]:
    paths = human_paths(root)
    return paths, render(paths, load_todo(root), load_items(paths))


def write_page(root: Path) -> Path:
    paths, text = render_page(root)
    paths.todo_file.write_text(text, encoding="utf-8")
    return paths.todo_file


# ---------------------------------------------------------------- asking and answering


def ask(paths: HumanPaths, item: Item) -> Path:
    """Add a new item; an id already in the queue is never overwritten."""
    if paths.item_file(item.id).exists():
        raise HumanError(f"item {item.id} exists; answer it or choose another id")
    todo = load_todo(paths.root)
    if item.topic not in {s.id for s in todo.sections}:
        names = ", ".join(s.id for s in todo.sections)
        raise HumanError(f"no section {item.topic!r} in {TODO_SOURCE} ({names})")
    if item.status != "pending" or item.answers or item.approval is not None:
        raise HumanError("a new item is pending, with no answer and no approval")
    return write_item(paths, item)


def answer(
    paths: HumanPaths,
    item_id: str,
    entry: Answer,
    *,
    agent_identity: str,
    close: bool,
) -> Item:
    """Record the owner's answer under the item.

    A question or a step closes on the answer unless told to stay open. An
    approval never does: only the owner's own GitHub review or a record signed
    by `tac approve` on the host counts, so its answer is history, not consent.
    """
    if entry.by.lower() == agent_identity.lower():
        raise HumanError(
            f"{entry.by} is the agent identity: what it writes is agent output, "
            "never the owner's answer"
        )
    item = load_item(paths, item_id)
    update: dict[str, Any] = {"answers": (*item.answers, entry)}
    if close and item.kind != "approval" and item.open:
        update |= {"status": "answered", "decided_at": entry.at, "decided_by": entry.by}
    changed = item.model_copy(update=update)
    # Validate the whole item again: model_copy does not.
    changed = Item.model_validate(changed.model_dump())
    write_item(paths, changed)
    return changed


def item_from_handoff(
    raw: str, where: str, items: Sequence[Item], *, item_id: str | None = None
) -> Item:
    """The pending item `tac handoff validate` wrote for a result that failed its
    contract after the repair, as a question in the owner's queue.

    Only the fixed text of the escalation reaches the page: the question, its
    three options and the validator's default. The findings may quote model
    output, so they stay in the envelope and the page gives only their count.
    The envelope's folder is the worker store, outside the repository, so only
    its file name is kept. The same escalation is never queued twice.
    """
    try:
        source = HumanItem.model_validate_json(raw)
    except ValidationError as exc:
        raise HumanError(f"{where}: {first_error(exc)}") from exc
    for item in items:
        if item.arguments.get("handoff_item") == source.id:
            raise HumanError(f"{source.id} is already in the queue as {item.id}")
    arguments: dict[str, JsonValue] = {
        "handoff_item": source.id,
        "run_id": source.run_id,
        "contract": source.arguments.contract,
        "envelope": Path(source.arguments.envelope).name,
    }
    if source.order_id is not None:
        arguments["order_id"] = source.order_id
    count = len(source.errors)
    noun = "finding" if count == 1 else "findings"
    body = {
        "id": item_id or next_escalation_id(items),
        "kind": "question",
        "topic": ESCALATION_TOPIC,
        "rank": next_rank(items, ESCALATION_TOPIC),
        "title": f"handoff {source.stage}",
        "question": source.question,
        "details": [
            f"{count} {noun} against contract {source.arguments.contract} after "
            f"the repair pass; they stay in the envelope "
            f"`{arguments['envelope']}` of run {source.run_id}, never on this page."
        ],
        "options": list(source.options),
        "recommendation": source.recommendation,
        "recommendation_source": "the handoff validator's default, not the board's",
        "blocks": [f"stage {source.stage} of run {source.run_id}"],
        "waits": (
            f"order {source.order_id}" if source.order_id else f"run {source.run_id}"
        ),
        "stage": source.stage,
        "agent": source.agent,
        "tool": source.tool,
        "arguments": arguments,
        "asked_at": source.asked_at,
    }
    try:
        return Item.model_validate(body)
    except ValidationError as exc:
        raise HumanError(f"{where}: {first_error(exc)}") from exc


# ---------------------------------------------------------------- recaps


class Recap(_Model):
    """The recap contract (contracts/handoffs/recap.schema.json) as a model."""

    title: Line
    shipped: tuple[Line, ...]
    in_flight: tuple[Line, ...]
    decisions: tuple[Line, ...]
    owner_actions: tuple[Line, ...]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].strip("-")
    return slug or "recap"


def recap_file_name(at: dt.datetime, slug: str) -> str:
    """<UTC basic datetime>-<slug>.md, ISO 8601 basic format, so names sort by time."""
    if not SLUG.match(slug):
        raise HumanError(f"not a slug: {slug!r} (lowercase letters, digits, hyphens)")
    return f"{at.astimezone(dt.UTC):%Y%m%dT%H%M%SZ}-{slug}.md"


def _bullets(values: Sequence[str]) -> list[str]:
    return [f"- {v}" for v in values] if values else ["- nothing"]


def recap_markdown(
    recap: Recap, open_items: Sequence[Item], at: dt.datetime, todo: str
) -> str:
    """Shipped first, then in flight, then decisions, then what only the owner
    can do, with the ids still open in the queue."""
    owner = list(recap.owner_actions)
    if open_items:
        ids = ", ".join(i.id for i in open_items)
        owner.append(f"Open in {todo}: {ids}.")
    lines = [
        f"# Recap: {recap.title}",
        "",
        f"Written {utc_text(at)} by `tac human recap`.",
        "",
        "## Shipped",
        "",
        *_bullets(recap.shipped),
        "",
        "## In flight",
        "",
        *_bullets(recap.in_flight),
        "",
        "## Decisions",
        "",
        *_bullets(recap.decisions),
        "",
        "## For the owner",
        "",
        *_bullets(owner),
    ]
    return "\n".join(lines) + "\n"


def word_count(text: str) -> int:
    return len(re.findall(r"[^\s#*`-]+", text))


def write_recap(
    paths: HumanPaths, recap: Recap, at: dt.datetime, slug: str | None
) -> Path:
    """Write one recap under the recap folder; an existing file is never replaced."""
    items = load_items(paths)
    text = recap_markdown(recap, [i for i in items if i.open], at, paths.todo)
    words = word_count(text)
    if words > MAX_RECAP_WORDS:
        raise HumanError(
            f"the recap runs to {words} words; keep it under {MAX_RECAP_WORDS}"
        )
    name = recap_file_name(at, slug or slugify(recap.title))
    path = paths.recaps / name
    if path.exists():
        raise HumanError(f"{paths.recap_dir}/{name} exists; recaps are never replaced")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def read_recap(root: Path, raw: str) -> Recap:
    """A recap payload, held to the recap contract and then to the model."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HumanError(f"the recap is not JSON: {exc}") from exc
    schema = json.loads((root / RECAP_CONTRACT).read_text(encoding="utf-8"))
    errors = sorted(Draft7Validator(schema).iter_errors(data), key=str)
    if errors:
        where = "/".join(map(str, errors[0].absolute_path)) or "recap"
        raise HumanError(
            f"the recap breaks its contract at {where}: {errors[0].message}"
        )
    try:
        return Recap.model_validate(data)
    except ValidationError as exc:
        raise HumanError(f"the recap: {first_error(exc)}") from exc
