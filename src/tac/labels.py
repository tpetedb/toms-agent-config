"""The repository's labels: declared once in .github/labels.yml, planned and
applied through the same Transport as the ruleset.

The file is in the format EndBug/label-sync reads, so it needs no rewrite if
that action is ever used. A dry run reads the public label list with no
credential; only the owner's own gh writes, on the host. Nothing here deletes a
label: one the file does not declare is named as unmanaged and left alone, since
issues may still carry it.
"""

from __future__ import annotations

import re
import tomllib
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tac.github import ApiError, Transport
from tac.work import Bad

LABELS_FILE = ".github/labels.yml"
GITHUB_CONFIG = ".agents/config/github.toml"
TEAMS_CONFIG = ".agents/config/teams.toml"
# GitHub refuses a longer name or description.
MAX_NAME = 50
MAX_DESCRIPTION = 100
COLOR = re.compile(r"^#?([0-9A-Fa-f]{6})$")
KEYS = frozenset({"name", "color", "description"})
PAGE = 100


@dataclass(frozen=True, slots=True)
class Label:
    name: str
    # Six lower-case hex digits without "#", as the API answers it.
    color: str
    description: str = ""

    @property
    def family(self) -> str:
        return self.name.partition("/")[0]

    def body(self) -> dict[str, str]:
        return {"name": self.name, "color": self.color, "description": self.description}


def parse_labels(text: str, source: str = LABELS_FILE) -> tuple[Label, ...]:
    """Every label the file declares; Bad names the first entry that is wrong."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise Bad(f"{source}: not YAML: {e}") from None
    if not isinstance(data, list) or not data:
        raise Bad(f"{source}: a non-empty list of labels")
    labels: list[Label] = []
    seen: set[str] = set()
    for i, entry in enumerate(data):
        where = f"{source}: label {i + 1}"
        if not isinstance(entry, dict):
            raise Bad(f"{where}: a mapping with name, color and description")
        unknown = sorted(set(entry) - KEYS)
        if unknown:
            # aliases would rename a label, which tac never does.
            raise Bad(f"{where}: unknown keys {unknown}; allowed: {sorted(KEYS)}")
        name, color = entry.get("name"), entry.get("color")
        description = entry.get("description", "")
        if not isinstance(name, str) or not 0 < len(name.strip()) <= MAX_NAME:
            raise Bad(f"{where}: name is 1 to {MAX_NAME} characters")
        if name != name.strip():
            raise Bad(f"{where}: {name!r} has spaces at its ends")
        match = COLOR.match(color) if isinstance(color, str) else None
        if match is None:
            raise Bad(f"{where} ({name}): color is six hex digits, as a string")
        if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
            raise Bad(
                f"{where} ({name}): description is text of at most "
                f"{MAX_DESCRIPTION} characters"
            )
        # GitHub treats label names without regard to case.
        if name.casefold() in seen:
            raise Bad(f"{where}: {name} is declared twice")
        seen.add(name.casefold())
        labels.append(Label(name, match.group(1).lower(), description))
    return tuple(labels)


def load_labels(root: Path) -> tuple[Label, ...]:
    path = root / LABELS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise Bad(f"{LABELS_FILE}: cannot read: {e.__class__.__name__}") from None
    return parse_labels(text)


def _toml(root: Path, rel: str) -> dict[str, Any]:
    try:
        return tomllib.loads((root / rel).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise Bad(f"{rel}: cannot read: {e.__class__.__name__}") from None


def team_ids(root: Path) -> tuple[str, ...]:
    """The teams of teams.toml, in file order."""
    teams = _toml(root, TEAMS_CONFIG).get("teams") or {}
    return tuple(teams) if isinstance(teams, dict) else ()


def labels_problems(root: Path, labels: Sequence[Label]) -> list[str]:
    """Where the declared labels and the configuration disagree: every label is
    in a family github.toml names, every family has one, state/* is exactly the
    order states and team/* exactly the teams."""
    config = _toml(root, GITHUB_CONFIG).get("labels") or {}
    families = tuple(config.get("families") or ())
    states = tuple(config.get("states") or ())
    problems: list[str] = []
    by_family: dict[str, set[str]] = {}
    for label in labels:
        family, slash, value = label.name.partition("/")
        if not slash or not value or family not in families:
            problems.append(
                f"{LABELS_FILE}: {label.name} is not <family>/<name> with a "
                f"family of {GITHUB_CONFIG} [labels] families {list(families)}"
            )
            continue
        by_family.setdefault(family, set()).add(value)
    for family in families:
        if family not in by_family:
            problems.append(f"{LABELS_FILE}: the family {family}/* has no label")
    wanted = {"state": set(states), "team": set(team_ids(root))}
    sources = {"state": f"{GITHUB_CONFIG} [labels] states", "team": TEAMS_CONFIG}
    for family, names in wanted.items():
        if family not in families:
            continue
        have = by_family.get(family, set())
        if have != names:
            problems.append(
                f"{LABELS_FILE}: {family}/* is {sorted(have)}, but {sources[family]} "
                f"says {sorted(names)}"
            )
    return problems


@dataclass(frozen=True, slots=True)
class LabelPlan:
    create: tuple[Label, ...]
    # The declared label and the name it has on GitHub now, which may differ in case.
    update: tuple[tuple[Label, str], ...]
    same: tuple[str, ...]
    unmanaged: tuple[str, ...]

    @property
    def settled(self) -> bool:
        return not self.create and not self.update

    def lines(self) -> list[str]:
        out = [f"create {label.name} #{label.color}" for label in self.create]
        out += [f"update {label.name} #{label.color}" for label, _ in self.update]
        out += [f"keep   {name}" for name in self.same]
        out += [
            f"leave  {name} (not declared; tac never deletes)"
            for name in self.unmanaged
        ]
        return out


def plan_labels(
    declared: Sequence[Label], current: Sequence[Mapping[str, Any]]
) -> LabelPlan:
    """What applying the file would change on GitHub; it never deletes."""
    live = {
        str(item.get("name", "")).casefold(): item
        for item in current
        if isinstance(item.get("name"), str)
    }
    create: list[Label] = []
    update: list[tuple[Label, str]] = []
    same: list[str] = []
    for label in declared:
        item = live.pop(label.name.casefold(), None)
        if item is None:
            create.append(label)
            continue
        now = (
            str(item.get("name")),
            str(item.get("color") or "").lower(),
            item.get("description") or "",
        )
        if now == (label.name, label.color, label.description):
            same.append(label.name)
        else:
            update.append((label, now[0]))
    unmanaged = sorted(str(item.get("name")) for item in live.values())
    return LabelPlan(tuple(create), tuple(update), tuple(same), tuple(unmanaged))


def fetch_labels(transport: Transport, repo: str) -> list[Mapping[str, Any]]:
    """Every label on the repository, page by page."""
    found: list[Mapping[str, Any]] = []
    page = 1
    while True:
        batch = transport.call(
            "GET", f"repos/{repo}/labels?per_page={PAGE}&page={page}"
        )
        if not isinstance(batch, list):
            raise ApiError(None, f"repos/{repo}/labels: not a list")
        found += [item for item in batch if isinstance(item, Mapping)]
        if len(batch) < PAGE:
            return found
        page += 1


def label_path(repo: str, name: str) -> str:
    # A label name holds "/", which must not split the API path.
    return f"repos/{repo}/labels/{urllib.parse.quote(name, safe='')}"


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    ok: bool
    lines: tuple[str, ...]


def apply_labels(
    transport: Transport, repo: str, declared: Sequence[Label]
) -> ApplyOutcome:
    """Create and update to match the file, then read back and judge again."""
    lines: list[str] = []
    try:
        plan = plan_labels(declared, fetch_labels(transport, repo))
        for label in plan.create:
            transport.call("POST", f"repos/{repo}/labels", label.body())
            lines.append(f"created {label.name}")
        for label, now in plan.update:
            body = {
                "new_name": label.name,
                "color": label.color,
                "description": label.description,
            }
            transport.call("PATCH", label_path(repo, now), body)
            lines.append(f"updated {label.name}")
        after = plan_labels(declared, fetch_labels(transport, repo))
    except ApiError as e:
        return ApplyOutcome(False, (*lines, f"refused: {e}"))
    lines += [f"left {name} (not declared)" for name in after.unmanaged]
    if not after.settled:
        return ApplyOutcome(
            False, (*lines, "read back: the labels still differ from the file")
        )
    lines.append(f"labels hold: {len(after.same)} match {LABELS_FILE}")
    return ApplyOutcome(True, tuple(lines))
