"""The standards registry, its floor, and the waivers that lift a gate for a while.

The project writes `.agents/config/standards.toml`; the owner keeps
`.agents/standards.floor.toml`, which a project may only tighten. The effective
standard is max(floor, project), key by key, and the rule for each key lives in
`FLOOR_RULES` below, the floor's schema: a boolean gate holds if either side
holds it, an enum takes the stricter value of an ordered list, a set is the
union, and a number takes the stricter bound. A key the floor does not know is
the project's alone. A change to the floor file is judged against the base
revision's floor by `loosened`, so the floor itself only ever tightens.
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from tac.work import Bad, IsoDate, Strs, Whole, as_tuple, validation_error

FLOOR_FILE = ".agents/standards.floor.toml"
STANDARDS_FILE = ".agents/config/standards.toml"

RuleKind = Literal["bool", "enum", "set", "max", "min"]


@dataclass(frozen=True, slots=True)
class Rule:
    """How one floor key compares with the registry key it bounds."""

    key: str
    path: tuple[str, ...]
    kind: RuleKind
    # enum: the allowed values, loosest first. bool: the stricter value.
    order: tuple[str, ...] = ()
    strict: bool = False

    @property
    def where(self) -> str:
        return ".".join(self.path)

    def check(self, value: object, side: str) -> None:
        """Refuse a value this rule cannot compare, naming the side it came from."""
        ok = {
            "bool": isinstance(value, bool),
            "enum": isinstance(value, str) and value in self.order,
            "set": isinstance(value, (list, tuple))
            and all(isinstance(v, str) for v in value),
            "max": isinstance(value, int) and not isinstance(value, bool),
            "min": isinstance(value, int) and not isinstance(value, bool),
        }[self.kind]
        if not ok:
            raise Bad(f"{side}: {self.key} = {value!r}: {self.expects()}")

    def expects(self) -> str:
        return {
            "bool": "true or false",
            "enum": "one of " + " < ".join(self.order),
            "set": "a list of strings",
            "max": "a whole number, an upper bound",
            "min": "a whole number, a lower bound",
        }[self.kind]

    def stricter(self, floor: Any, project: Any) -> Any:
        if self.kind == "bool":
            return self.strict if self.strict in (floor, project) else project
        if self.kind == "enum":
            return max(floor, project, key=self.order.index)
        if self.kind == "set":
            return [*floor, *(v for v in project if v not in floor)]
        if self.kind == "max":
            return min(floor, project)
        return max(floor, project)

    def looser(self, value: Any, bound: Any) -> bool:
        """True when `value` asks less than `bound` does."""
        if self.kind == "set":
            return not set(bound) <= set(value)
        return self.stricter(bound, value) != value


FLOOR_RULES: tuple[Rule, ...] = (
    Rule("tests", ("quality", "tests"), "enum", ("optional", "required")),
    Rule("lint", ("quality", "lint"), "enum", ("optional", "required")),
    Rule("secrets", ("quality", "secrets"), "enum", ("none", "gitleaks")),
    Rule(
        "changelog", ("changelog", "format"), "enum", ("none", "keepachangelog-1.1.0")
    ),
    Rule("versioning", ("versioning", "scheme"), "enum", ("none", "semver-2.0.0")),
    Rule("dates", ("dates", "format"), "enum", ("any", "iso-8601")),
    Rule("em_dashes", ("code", "markdown", "em_dashes"), "bool", strict=False),
    Rule("commit_max_subject", ("commits", "max_subject"), "max"),
    Rule("trailers_required", ("commits", "trailers_required"), "set"),
)
RULES: Mapping[str, Rule] = {r.key: r for r in FLOOR_RULES}
# What a waiver may lift: a floor key, or a checker `tac check` runs from the registry.
GATES = frozenset(RULES) | {
    "types",
    "format",
    "commits",
    "dependencies",
    "licences",
    "data",
    "diagrams",
    "shell",
    "sql",
    "markdown",
    "yaml",
    "toml",
    "actions",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Waiver(_Model):
    """One gate lifted until a day, with the owner's approval."""

    gate: str
    reason: Annotated[str, Field(min_length=1)]
    expires: IsoDate
    # The owner is the only approver that counts; anything else fails to load.
    approved_by: Literal["owner"]

    def expired(self, today: dt.date) -> bool:
        return today > self.expires


class FloorFile(_Model):
    schema_version: Literal[1]
    floor: dict[str, Any]
    waivers: Annotated[tuple[Waiver, ...], BeforeValidator(as_tuple)] = ()


@dataclass(frozen=True, slots=True)
class Floor:
    values: Mapping[str, Any]
    waivers: tuple[Waiver, ...]


def utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def parse_floor(data: Mapping[str, Any], where: str) -> Floor:
    """Validate the floor file: known keys, comparable values, well-formed waivers."""
    try:
        parsed = FloorFile.model_validate(dict(data))
    except ValidationError as e:
        raise validation_error(Path(where), e) from None
    unknown = sorted(set(parsed.floor) - set(RULES))
    if unknown:
        raise Bad(
            f"{where}: [floor] unknown key {', '.join(unknown)}; "
            f"known: {', '.join(RULES)}"
        )
    for key, value in parsed.floor.items():
        RULES[key].check(value, f"{where}: [floor]")
    for waiver in parsed.waivers:
        if waiver.gate not in GATES:
            raise Bad(
                f"{where}: waiver for unknown gate {waiver.gate!r}; "
                f"known: {', '.join(sorted(GATES))}"
            )
    return Floor(dict(parsed.floor), parsed.waivers)


def waiver_problems(floor: Floor, where: str, today: dt.date) -> list[str]:
    """An expired waiver fails: lifting a gate is always for a stated while."""
    return [
        f"{where}: the waiver for {w.gate} expired on {w.expires.isoformat()} "
        f"({w.reason}); remove it, or the owner renews it with a new date"
        for w in floor.waivers
        if w.expired(today)
    ]


def loosened(base: Floor, head: Floor) -> list[str]:
    """Every way `head` asks less than `base`: a dropped key or a looser value."""
    problems = []
    for key, bound in base.values.items():
        if key not in head.values:
            problems.append(f"floor key {key} was dropped (it was {bound!r})")
        elif RULES[key].looser(head.values[key], bound):
            problems.append(
                f"floor key {key} was loosened from {bound!r} to "
                f"{head.values[key]!r}; the floor may only be tightened"
            )
    return problems


def get_path(data: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = data
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _set(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


@dataclass(frozen=True, slots=True)
class Applied:
    """The effective registry and, per bounded path, which side set it."""

    values: dict[str, Any]
    from_floor: Mapping[str, Any]  # path -> the project value the floor overrode
    waived: tuple[Waiver, ...]


def apply_floor(registry: Mapping[str, Any], floor: Floor, where: str) -> Applied:
    """max(floor, project) for every key the floor schema knows."""
    values = copy.deepcopy(dict(registry))
    overridden: dict[str, Any] = {}
    for rule in FLOOR_RULES:
        project = get_path(registry, rule.path)
        if project is not None:
            rule.check(project, f"{where}: [{'.'.join(rule.path[:-1])}]")
        if rule.key not in floor.values:
            continue
        bound = floor.values[rule.key]
        if project is None:
            _set(values, rule.path, bound)
            overridden[rule.where] = None
            continue
        effective = rule.stricter(bound, project)
        same = (
            list(effective) == list(project)
            if rule.kind == "set"
            else effective == project
        )
        if not same:
            _set(values, rule.path, effective)
            overridden[rule.where] = project
    return Applied(values, overridden, floor.waivers)


# ---------------------------------------------------------------- the registry


class Versioning(_Model):
    scheme: str
    source: str


FragmentType = Literal["added", "changed", "deprecated", "removed", "fixed", "security"]


class Changelog(_Model):
    format: str
    fragments: str
    types: Annotated[tuple[FragmentType, ...], BeforeValidator(as_tuple)]
    tool: str


class Commits(_Model):
    convention: Literal["what-and-why", "conventional-1.0.0"]
    max_subject: Whole
    trailers_required: Strs


class Dates(_Model):
    format: str
    timezone: str
    basic_in_filenames: bool


class Diagrams(_Model):
    tool: Literal["mermaid"]
    flowcharts: str
    architecture: str
    validate_: str = Field(alias="validate")


class PythonCode(_Model):
    formatter: str
    linter: str
    types: str
    line_length: Whole


class ShellCode(_Model):
    linter: str
    dialect: str


class Linter(_Model):
    linter: str


class MarkdownCode(_Model):
    linter: str
    em_dashes: bool


class Formatter(_Model):
    formatter: str


class Code(_Model):
    python: PythonCode
    shell: ShellCode
    sql: Linter
    markdown: MarkdownCode
    yaml: Linter
    toml: Formatter
    actions: Linter


class Quality(_Model):
    tests: str
    lint: str
    secrets: str
    dependencies: str
    licences: str
    data: str


class Registry(_Model):
    """config/standards.toml as written; the floor is applied after."""

    schema_version: Literal[1]
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]
    versioning: Versioning
    changelog: Changelog
    commits: Commits
    dates: Dates
    diagrams: Diagrams
    code: Code
    quality: Quality
