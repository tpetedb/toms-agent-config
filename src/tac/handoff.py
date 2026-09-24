"""`tac handoff`: every prompt one stage hands the next goes through a declared
template and a contract, and every result through a validator (design section 5).

A handoff template lives under templates/handoffs/ (or templates/human/ for the
owner-facing ones) and names its contracts on its first line:

    {# writes: build-report; reads: order-spec #}

`render` takes the payloads the template reads, each either an upstream envelope
that passed or the pipeline's entry payload, checks each against its contract,
renders the prompt in a sandboxed Jinja environment with StrictUndefined and no
loader, and writes a dispatch envelope that binds the template, the contract the
stage must write and the rendered prompt by sha256. Every payload and skill is
fenced in a named tag as reference data, so nothing inside one reads as an
instruction or can close another's tag.

`validate` takes a stage's result and its dispatch envelope. A result without
one is refused: nothing enters a run that was not dispatched through `render`.
A result that fails its contract gets one repair pass, rendered from
repair.md.j2 with the diagnostics; a second failure ends as a fixed human item,
never as a third try.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from jinja2 import StrictUndefined, Template, TemplateError, nodes
from jinja2.sandbox import SandboxedEnvironment
from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from tac.contracts import MODEL_FACING_DIR
from tac.draft07 import draft07
from tac.receipts import ID_PATTERN, ORDER_PATTERN
from tac.work import Bad

TEMPLATE_ROOT = "templates"
# The folders a handoff template may come from; a path anywhere else is refused.
TEMPLATE_DIRS = ("handoffs", "human")
REPAIR_TEMPLATE = "handoffs/repair.md.j2"
SKILLS_DIR = ".agents/skills"
ENVELOPE_CONTRACT = "contracts/envelope.schema.json"
HUMAN_ITEM_CONTRACT = "contracts/human-item.schema.json"
ENVELOPE_VERSION = 1

NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
TEMPLATE_PATH = re.compile(r"^(handoffs|human)/[a-z][a-z0-9-]*\.md\.j2$")
HEADER = re.compile(
    r"^\{#\s*writes:\s*([a-z][a-z0-9-]*)\s*;\s*reads:\s*([a-z0-9, -]*?)\s*-?#\}\s*$"
)
SHA256 = r"^[0-9a-f]{64}$"
UTC_SECONDS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
# The most of a failed result the repair prompt quotes back.
REPAIR_QUOTE_CHARS = 8000
EVIDENCE_CHARS = 200

Status = Literal["dispatched", "pass", "degraded", "escalated"]
ACCEPTED: frozenset[str] = frozenset({"pass", "degraded"})


# ---------------------------------------------------------------- models


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class Finding(_Model):
    """One validation finding, in the diagnostic shape the design names."""

    code: str
    severity: Literal["error", "warning"]
    message: str
    subject: str
    evidence: str
    # The name the design gives it; tools that read diagnostics expect camelCase.
    supportedFixes: tuple[str, ...]


class Validation(_Model):
    checks: tuple[str, ...] = ()
    errors: tuple[Finding, ...] = ()
    repair_rounds: int = Field(default=0, ge=0, le=1)
    # The sha256 of the repair prompt, so its dispatch is bound like the first.
    repair_input_sha256: str | None = Field(default=None, pattern=SHA256)


class Tokens(_Model):
    input: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    output: int | None = None


class Confidence(_Model):
    level: Literal["low", "medium", "high"]
    basis: Literal["test-receipt", "reviewed", "single-source", "inferred", "owner"]


class InputRef(_Model):
    path: str
    sha256: str = Field(pattern=SHA256)


class Envelope(_Model):
    """A stage's record: what it was given, bound by hash, and what it produced."""

    envelope_version: Literal[1] = ENVELOPE_VERSION
    contract: str = Field(alias="schema", pattern=NAME.pattern)
    stage: str = Field(pattern=ID_PATTERN)
    run_id: str = Field(pattern=ID_PATTERN)
    order_id: str | None = Field(default=None, pattern=ORDER_PATTERN)
    role: str | None = Field(default=None, pattern=NAME.pattern)
    topic: tuple[str, ...] = ()
    provider: str | None = None
    harness: str | None = None
    model: str | None = None
    effort: str | None = None
    created: str = Field(pattern=UTC_SECONDS)
    modified: str = Field(pattern=UTC_SECONDS)
    inputs: tuple[InputRef, ...]
    template: str = Field(pattern=TEMPLATE_PATH.pattern)
    template_sha256: str = Field(pattern=SHA256)
    schema_sha256: str = Field(pattern=SHA256)
    rendered_input_sha256: str = Field(pattern=SHA256)
    payload: dict[str, JsonValue] | None = None
    payload_sha256: str | None = Field(default=None, pattern=SHA256)
    validation: Validation = Validation()
    tokens: Tokens = Tokens()
    cost_estimate_usd: float | None = None
    confidence: Confidence = Confidence(level="low", basis="inferred")
    status: Status

    @model_validator(mode="after")
    def _payload_matches_status(self) -> Self:
        if self.status in ACCEPTED:
            if self.payload is None or self.payload_sha256 != payload_digest(
                self.payload
            ):
                raise ValueError(
                    f"status {self.status} needs a payload whose payload_sha256 "
                    "matches it"
                )
        elif self.payload is not None or self.payload_sha256 is not None:
            raise ValueError(f"status {self.status} carries no payload")
        return self

    def to_json(self) -> str:
        body = self.model_dump(mode="json", by_alias=True)
        return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


class HumanArguments(_Model):
    envelope: str
    contract: str = Field(pattern=NAME.pattern)


class HumanItem(_Model):
    """The fixed outcome of a result that failed its contract after the repair:
    a pending item in the approval shape of design section 7, which `tac human`
    renders into TODO.HUMAN.md. Its question and options never carry model text;
    the findings travel as data."""

    item_version: Literal[1] = 1
    id: str = Field(pattern=r"^handoff-[A-Za-z0-9._:-]{1,160}$")
    kind: Literal["handoff-escalation"] = "handoff-escalation"
    run_id: str = Field(pattern=ID_PATTERN)
    stage: str = Field(pattern=ID_PATTERN)
    order_id: str | None = Field(default=None, pattern=ORDER_PATTERN)
    agent: str | None = None
    tool: Literal["tac handoff validate"] = "tac handoff validate"
    arguments: HumanArguments
    question: str
    options: tuple[Literal["rerun", "amend-contract", "cancel"], ...]
    recommendation: Literal["rerun", "amend-contract", "cancel"]
    status: Literal["pending"] = "pending"
    asked_at: str = Field(pattern=UTC_SECONDS)
    decided_at: None = None
    decided_by: None = None
    errors: tuple[Finding, ...]

    def to_json(self) -> str:
        body = self.model_dump(mode="json")
        return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def contract_json_schemas() -> dict[str, dict[str, JsonValue]]:
    """The envelope and human item contracts, repo path to schema."""
    return {
        ENVELOPE_CONTRACT: draft07(Envelope),
        HUMAN_ITEM_CONTRACT: draft07(HumanItem),
    }


def schema_text(schema: object) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------- hashing


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def payload_digest(payload: Mapping[str, Any]) -> str:
    """The sha256 of a payload's canonical JSON, the same whatever its spacing."""
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return digest(body.encode("utf-8"))


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


# ---------------------------------------------------------------- contracts


@dataclass(frozen=True, slots=True)
class Contract:
    name: str
    rel: str
    raw: bytes
    schema: dict[str, Any]

    @property
    def sha256(self) -> str:
        return digest(self.raw)

    def findings(self, payload: Any) -> list[Finding]:
        errors = Draft7Validator(self.schema).iter_errors(payload)
        found = [
            Finding(
                code=f"schema.{e.validator}",
                severity="error",
                message=str(e.message)[:500],
                subject="/" + "/".join(str(p) for p in e.absolute_path),
                evidence=json.dumps(e.instance, ensure_ascii=False)[:EVIDENCE_CHARS],
                supportedFixes=("repair",),
            )
            for e in errors
        ]
        return sorted(found, key=lambda f: (f.subject, f.code, f.message))


def load_contract(root: Path, name: str) -> Contract:
    """A model-facing contract by name, refused unless it is valid draft-07."""
    if not NAME.match(name):
        raise Bad(f"contract {name!r}: not a contract name")
    rel = f"{MODEL_FACING_DIR}/{name}.schema.json"
    path = root / rel
    if not path.is_file():
        raise Bad(f"contract {name}: no {rel}")
    raw = path.read_bytes()
    try:
        schema = json.loads(raw)
        Draft7Validator.check_schema(schema)
    except (json.JSONDecodeError, SchemaError) as e:
        raise Bad(f"{rel}: not a valid draft-07 schema: {e}") from None
    return Contract(name, rel, raw, schema)


# ---------------------------------------------------------------- templates


@dataclass(frozen=True, slots=True)
class HandoffTemplate:
    rel: str
    raw: bytes
    body: str
    writes: str
    reads: tuple[str, ...]

    @property
    def sha256(self) -> str:
        return digest(self.raw)


def read_handoff_template(root: Path, rel: str) -> HandoffTemplate:
    """A handoff template, refused unless it sits in an allowed folder and its
    first line names the contract it writes and the ones it reads."""
    if not TEMPLATE_PATH.match(rel):
        raise Bad(
            f"template {rel!r}: a handoff template is "
            f"{' or '.join(f'{d}/<name>.md.j2' for d in TEMPLATE_DIRS)}"
        )
    path = root / TEMPLATE_ROOT / rel
    if not path.is_file():
        raise Bad(f"template {rel}: no {TEMPLATE_ROOT}/{rel}")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if rel == REPAIR_TEMPLATE:
        return HandoffTemplate(rel, raw, text, "", ())
    first, _, _ = text.partition("\n")
    match = HEADER.match(first)
    if match is None:
        raise Bad(
            f"{TEMPLATE_ROOT}/{rel}: the first line must be "
            "{# writes: <contract>; reads: <contract>[, <contract>] #}"
        )
    reads = tuple(r.strip() for r in match.group(2).split(",") if r.strip())
    for name in (match.group(1), *reads):
        if not NAME.match(name):
            raise Bad(f"{TEMPLATE_ROOT}/{rel}: {name!r} is not a contract name")
    return HandoffTemplate(rel, raw, text, match.group(1), reads)


def handoff_templates(root: Path) -> list[str]:
    base = root / TEMPLATE_ROOT
    return sorted(
        p.relative_to(base).as_posix()
        for d in TEMPLATE_DIRS
        if (base / d).is_dir()
        for p in (base / d).glob("*.md.j2")
    )


def check_templates(root: Path) -> list[str]:
    """Every handoff template names contracts that exist and renders in the
    sandbox; the checker's view, so `tac check` catches a broken one."""
    problems: list[str] = []
    for rel in handoff_templates(root):
        try:
            template = read_handoff_template(root, rel)
            for name in (template.writes, *template.reads):
                if name:
                    load_contract(root, name)
            _compile(template)
        except Bad as e:
            problems.append(str(e))
    return problems


def _environment() -> SandboxedEnvironment:
    # No loader: a template can neither include another nor read any file.
    return SandboxedEnvironment(
        undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True
    )


def fence(text: str, tags: Iterable[str]) -> str:
    """Data may not open or close any tag the prompt fences data in."""
    names = sorted(set(tags), key=len, reverse=True)
    if not names:
        return text
    pattern = re.compile(r"<(/?)\s*(" + "|".join(map(re.escape, names)) + r")\b")
    return pattern.sub(lambda m: f"&lt;{m.group(1)}{m.group(2)}", text)


# Nodes that reach another template; with no loader they could only fail late.
_REACHING = (nodes.Include, nodes.Import, nodes.FromImport, nodes.Extends)


def _compile(template: HandoffTemplate) -> Template:
    env = _environment()
    where = f"{TEMPLATE_ROOT}/{template.rel}"
    try:
        tree = env.parse(template.body)
    except TemplateError as e:
        raise Bad(f"{where}: {e}") from None
    if any(True for _ in tree.find_all(_REACHING)):
        raise Bad(
            f"{where}: a handoff template renders alone: no include, import or extends"
        )
    return env.from_string(template.body)


def _render(template: HandoffTemplate, context: Mapping[str, Any]) -> str:
    compiled = _compile(template)
    try:
        return compiled.render(**context)
    except TemplateError as e:
        raise Bad(f"{TEMPLATE_ROOT}/{template.rel}: {e}") from None


# ---------------------------------------------------------------- render


@dataclass(frozen=True, slots=True)
class Given:
    """One payload a stage reads, and the file it came from."""

    contract: str
    payload: dict[str, Any]
    ref: InputRef


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise Bad(f"{path.name}: not readable JSON: {e}") from None


def read_envelope(path: Path) -> Envelope:
    """An envelope file, refused unless it matches the envelope contract."""
    if not path.is_file():
        raise Bad(
            f"{path.name}: no envelope; a handoff without its envelope is refused"
        )
    data = read_json(path)
    try:
        return Envelope.model_validate(data)
    except ValueError as e:
        raise Bad(f"{path.name}: not a valid envelope: {e}") from None


def upstream(root: Path, path: Path) -> Given:
    """A payload from an upstream stage: its envelope passed, its payload is the
    one it hashed, and it still meets its contract."""
    envelope = read_envelope(path)
    if envelope.status not in ACCEPTED:
        raise Bad(
            f"{path.name}: stage {envelope.stage} is {envelope.status}; only a "
            "payload that passed its contract may be handed on"
        )
    assert envelope.payload is not None
    problems = load_contract(root, envelope.contract).findings(envelope.payload)
    if problems:
        raise Bad(
            f"{path.name}: its payload no longer meets contract "
            f"{envelope.contract}: {problems[0].subject} {problems[0].message}"
        )
    ref = InputRef(path=_rel(root, path), sha256=digest(path.read_bytes()))
    return Given(envelope.contract, dict(envelope.payload), ref)


def entry(root: Path, path: Path, contract: str) -> Given:
    """The pipeline's entry payload, which no stage produced, checked against
    the contract the template reads."""
    data = read_json(path)
    if not isinstance(data, dict):
        raise Bad(f"{path.name}: an entry payload is a JSON object")
    problems = load_contract(root, contract).findings(data)
    if problems:
        raise Bad(
            f"{path.name}: does not meet contract {contract}: "
            f"{problems[0].subject} {problems[0].message}"
        )
    ref = InputRef(path=_rel(root, path), sha256=digest(path.read_bytes()))
    return Given(contract, data, ref)


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    text: str
    ref: InputRef


def read_skill(root: Path, name: str) -> Skill:
    if not NAME.match(name):
        raise Bad(f"skill {name!r}: not a skill name")
    path = root / SKILLS_DIR / name / "SKILL.md"
    if not path.is_file():
        raise Bad(f"skill {name}: no {SKILLS_DIR}/{name}/SKILL.md")
    raw = path.read_bytes()
    ref = InputRef(path=_rel(root, path), sha256=digest(raw))
    return Skill(name, raw.decode("utf-8"), ref)


@dataclass(frozen=True, slots=True)
class Stage:
    """Who a prompt is for: written by the tool from the launch, never a model."""

    id: str
    run_id: str
    order_id: str | None = None
    role: str | None = None
    topic: tuple[str, ...] = ()
    provider: str | None = None
    harness: str | None = None
    model: str | None = None
    effort: str | None = None

    def check(self) -> None:
        for label, value, pattern in (
            ("stage", self.id, ID_PATTERN),
            ("run id", self.run_id, ID_PATTERN),
            ("order id", self.order_id, ORDER_PATTERN),
            ("role", self.role, NAME.pattern),
        ):
            if value is not None and not re.match(pattern, value):
                raise Bad(f"{label} {value!r} does not match {pattern}")


@dataclass(frozen=True, slots=True)
class Rendered:
    prompt: str
    envelope: Envelope


def _given_order(template: HandoffTemplate, given: Sequence[Given]) -> list[Given]:
    """Every contract the template reads is given, and nothing else is."""
    names = [g.contract for g in given]
    missing = [r for r in template.reads if r not in names]
    extra = sorted({n for n in names if n not in template.reads})
    if missing or extra:
        why = []
        if missing:
            why.append(f"missing {', '.join(missing)}")
        if extra:
            why.append(f"not read by it: {', '.join(extra)}")
        raise Bad(
            f"{TEMPLATE_ROOT}/{template.rel} reads "
            f"{', '.join(template.reads) or 'nothing'}; {'; '.join(why)}"
        )
    order = {name: i for i, name in enumerate(template.reads)}
    return sorted(given, key=lambda g: order[g.contract])


def render(
    root: Path,
    template_rel: str,
    stage: Stage,
    given: Sequence[Given],
    skills: Sequence[str] = (),
    *,
    now: str | None = None,
) -> Rendered:
    """The prompt for one stage and its dispatch envelope."""
    stage.check()
    if template_rel == REPAIR_TEMPLATE:
        raise Bad(f"{REPAIR_TEMPLATE} is rendered by tac handoff validate, not render")
    template = read_handoff_template(root, template_rel)
    writes = load_contract(root, template.writes)
    ordered = _given_order(template, given)
    loaded = [read_skill(root, name) for name in skills]
    tags = [
        "contract",
        *(g.contract for g in ordered),
        *(f"skill-{s.name}" for s in loaded),
    ]
    inputs = [
        {
            "contract": g.contract,
            "tag": g.contract,
            "text": fence(
                json.dumps(g.payload, indent=2, sort_keys=True, ensure_ascii=False),
                tags,
            ),
        }
        for g in ordered
    ]
    context = {
        "stage": {
            "id": stage.id,
            "run_id": stage.run_id,
            "order_id": stage.order_id or "none",
            "role": stage.role or "none",
        },
        "inputs": inputs,
        "skills": [
            {"name": s.name, "tag": f"skill-{s.name}", "text": fence(s.text, tags)}
            for s in loaded
        ],
        "contract": {
            "name": writes.name,
            "schema": json.dumps(writes.schema, indent=2, ensure_ascii=False),
        },
    }
    prompt = _render(template, context)
    when = now or utc_now()
    envelope = Envelope.model_validate(
        {
            "schema": writes.name,
            "stage": stage.id,
            "run_id": stage.run_id,
            "order_id": stage.order_id,
            "role": stage.role,
            "topic": stage.topic,
            "provider": stage.provider,
            "harness": stage.harness,
            "model": stage.model,
            "effort": stage.effort,
            "created": when,
            "modified": when,
            "inputs": [g.ref for g in ordered] + [s.ref for s in loaded],
            "template": template.rel,
            "template_sha256": template.sha256,
            "schema_sha256": writes.sha256,
            "rendered_input_sha256": digest(prompt.encode("utf-8")),
            "status": "dispatched",
        }
    )
    return Rendered(prompt, envelope)


# ---------------------------------------------------------------- validate


Outcome = Literal["pass", "degraded", "repair", "escalated"]


@dataclass(frozen=True, slots=True)
class Validated:
    outcome: Outcome
    envelope: Envelope
    repair_prompt: str | None = None
    human: HumanItem | None = None


def parse_result(text: str) -> tuple[object | None, list[Finding]]:
    try:
        return json.loads(text), []
    except json.JSONDecodeError as e:
        return None, [
            Finding(
                code="json.parse",
                severity="error",
                message=f"not valid JSON: {e.msg} at line {e.lineno} column {e.colno}",
                subject="/",
                evidence=text[:EVIDENCE_CHARS],
                supportedFixes=("repair",),
            )
        ]


def _repair_prompt(
    root: Path,
    envelope: Envelope,
    contract: Contract,
    errors: Sequence[Finding],
    text: str,
) -> str:
    template = read_handoff_template(root, REPAIR_TEMPLATE)
    tags = ("invalid-output", "contract-errors", "contract")
    quoted = text if len(text) <= REPAIR_QUOTE_CHARS else text[:REPAIR_QUOTE_CHARS]
    return _render(
        template,
        {
            "stage": {
                "id": envelope.stage,
                "run_id": envelope.run_id,
                "order_id": envelope.order_id or "none",
                "role": envelope.role or "none",
            },
            "contract": {
                "name": contract.name,
                "schema": json.dumps(contract.schema, indent=2, ensure_ascii=False),
            },
            "errors": [
                {
                    "code": e.code,
                    "subject": fence(e.subject, tags),
                    "message": fence(e.message, tags),
                }
                for e in errors
            ],
            "output": fence(quoted, tags),
            "truncated": len(text) > REPAIR_QUOTE_CHARS,
        },
    )


QUESTION = (
    "Stage {stage} of run {run} returned a result that fails contract {contract} "
    "after its one repair pass. Rerun the stage, amend the contract or its "
    "template, or cancel the run."
)


def validate(
    root: Path,
    envelope: Envelope,
    result_text: str,
    *,
    envelope_path: str,
    now: str | None = None,
) -> Validated:
    """Judge a stage's result against the contract its dispatch named."""
    if envelope.status != "dispatched":
        raise Bad(
            f"stage {envelope.stage} is already {envelope.status}; a result is "
            "judged once, against the envelope of its dispatch"
        )
    contract = load_contract(root, envelope.contract)
    if contract.sha256 != envelope.schema_sha256:
        raise Bad(
            f"contract {contract.name} changed since stage {envelope.stage} was "
            "dispatched; render the stage again"
        )
    when = now or utc_now()
    payload, errors = parse_result(result_text)
    checks = ("json", f"schema:{contract.name}")
    if not errors:
        if isinstance(payload, dict):
            errors = contract.findings(payload)
        else:
            errors = contract.findings(payload) or [
                Finding(
                    code="schema.type",
                    severity="error",
                    message="the result must be a JSON object",
                    subject="/",
                    evidence=result_text[:EVIDENCE_CHARS],
                    supportedFixes=("repair",),
                )
            ]
    rounds = envelope.validation.repair_rounds
    base = envelope.model_dump(by_alias=True)
    if not errors:
        assert isinstance(payload, dict)
        status: Status = "pass" if rounds == 0 else "degraded"
        done = Envelope.model_validate(
            {
                **base,
                "modified": when,
                "payload": payload,
                "payload_sha256": payload_digest(payload),
                "validation": {
                    **base["validation"],
                    "checks": checks,
                    "errors": (),
                },
                "status": status,
            }
        )
        return Validated(status, done)
    if rounds == 0:
        prompt = _repair_prompt(root, envelope, contract, errors, result_text)
        again = Envelope.model_validate(
            {
                **base,
                "modified": when,
                "validation": {
                    "checks": checks,
                    "errors": errors,
                    "repair_rounds": 1,
                    "repair_input_sha256": digest(prompt.encode("utf-8")),
                },
            }
        )
        return Validated("repair", again, repair_prompt=prompt)
    escalated = Envelope.model_validate(
        {
            **base,
            "modified": when,
            "validation": {**base["validation"], "checks": checks, "errors": errors},
            "status": "escalated",
        }
    )
    human = HumanItem(
        id=f"handoff-{envelope.run_id}-{envelope.stage}",
        run_id=envelope.run_id,
        stage=envelope.stage,
        order_id=envelope.order_id,
        agent=envelope.role,
        arguments=HumanArguments(envelope=envelope_path, contract=contract.name),
        question=QUESTION.format(
            stage=envelope.stage, run=envelope.run_id, contract=contract.name
        ),
        options=("rerun", "amend-contract", "cancel"),
        recommendation="rerun",
        asked_at=when,
        errors=tuple(errors),
    )
    return Validated("escalated", escalated, human=human)


# ---------------------------------------------------------------- store


def worker_runs(root: Path) -> Path:
    """Where envelopes live: runs/ in the worker store (design section 2.1)."""
    from tac.config import load_config
    from tac.runner import RunnerError, git_common_dir

    config = load_config(root)
    store = config.knobs.memory.runtime_store
    try:
        base = (
            git_common_dir(root) / config.runtime.paths.worker_store
            if store == "git-common-dir"
            else Path(store)
        )
    except RunnerError as e:
        raise Bad(str(e)) from None
    return base / "runs"


def next_envelope_path(runs: Path, run_id: str, stage: str) -> Path:
    """runs/<run>/NN-<stage>.json, numbered after the envelopes already there."""
    folder = runs / run_id
    taken = [
        int(p.name[:2])
        for p in (folder.glob("[0-9][0-9]-*.json") if folder.is_dir() else [])
        if not p.name.endswith((".human.json",))
    ]
    return folder / f"{max(taken, default=0) + 1:02d}-{stage}.json"


def sibling(envelope_path: Path, suffix: str) -> Path:
    return envelope_path.with_name(envelope_path.name.removesuffix(".json") + suffix)
