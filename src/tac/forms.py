"""Judge .github/ISSUE_TEMPLATE/ and the pull request template before GitHub does.

GitHub rejects a malformed issue form only when someone opens the chooser, and
drops a label that does not exist without a word, so both are checked here
against the published syntax:
https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms
and https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-githubs-form-schema
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from tac.labels import GITHUB_CONFIG, Label, labels_problems, load_labels
from tac.work import Bad

FORMS_DIR = ".github/ISSUE_TEMPLATE"
CHOOSER = f"{FORMS_DIR}/config.yml"
PR_TEMPLATE = ".github/pull_request_template.md"
TOP_REQUIRED = frozenset({"name", "description", "body"})
TOP_OPTIONAL = frozenset({"title", "labels", "assignees", "type", "projects"})
# type: (required attributes, optional attributes, allowed validations)
ELEMENTS: Mapping[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {
    "markdown": (frozenset({"value"}), frozenset(), frozenset()),
    "textarea": (
        frozenset({"label"}),
        frozenset({"description", "placeholder", "value", "render"}),
        frozenset({"required"}),
    ),
    "input": (
        frozenset({"label"}),
        frozenset({"description", "placeholder", "value"}),
        frozenset({"required"}),
    ),
    "dropdown": (
        frozenset({"label", "options"}),
        frozenset({"description", "multiple", "default"}),
        frozenset({"required"}),
    ),
    "checkboxes": (
        frozenset({"label", "options"}),
        frozenset({"description"}),
        frozenset({"required"}),
    ),
    "upload": (
        frozenset({"label"}),
        frozenset({"description"}),
        frozenset({"required", "accept"}),
    ),
}
ELEMENT_ID = re.compile(r"^[A-Za-z0-9_-]+$")
CHOOSER_KEYS = frozenset({"blank_issues_enabled", "contact_links"})
LINK_KEYS = frozenset({"name", "url", "about"})
# The first line a hook and a reader look for, as work/templates/report.md has it.
TAG_LINE = re.compile(r"\Aorder: (<id>|[a-z0-9-]+)\n")
EM_DASH = "—"


def _yaml(path: Path, rel: str) -> tuple[Any, list[str]]:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), []
    except (OSError, yaml.YAMLError) as e:
        return None, [f"{rel}: not readable YAML: {e.__class__.__name__}"]


def _element_problems(
    where: str, element: Any, ids: set[str], labels: set[str]
) -> list[str]:
    if not isinstance(element, dict):
        return [f"{where}: a mapping with type and attributes"]
    kind = element.get("type")
    if not isinstance(kind, str) or kind not in ELEMENTS:
        return [f"{where}: type {kind!r} is not one of {sorted(ELEMENTS)}"]
    required, optional, validations = ELEMENTS[kind]
    problems = [
        f"{where}: unknown key {key!r}"
        for key in sorted(set(element) - {"type", "id", "attributes", "validations"})
    ]
    if "id" in element:
        eid = element["id"]
        if kind == "markdown":
            problems.append(f"{where}: a markdown element takes no id")
        elif not isinstance(eid, str) or not ELEMENT_ID.match(eid):
            problems.append(f"{where}: id is letters, digits, - and _")
        elif eid in ids:
            problems.append(f"{where}: id {eid} is used twice")
        else:
            ids.add(eid)
    attributes = element.get("attributes")
    if not isinstance(attributes, dict):
        return [*problems, f"{where}: attributes is a mapping"]
    missing = sorted(required - set(attributes))
    if missing:
        problems.append(f"{where}: {kind} needs attributes {missing}")
    extra = sorted(set(attributes) - required - optional)
    if extra:
        problems.append(f"{where}: {kind} takes no attributes {extra}")
    label = attributes.get("label")
    if label is not None:
        if not isinstance(label, str) or not label.strip():
            problems.append(f"{where}: label is text")
        elif label in labels:
            problems.append(f"{where}: label {label!r} is used twice")
        else:
            labels.add(label)
    checks = element.get("validations", {})
    if not isinstance(checks, dict):
        problems.append(f"{where}: validations is a mapping")
    else:
        bad = sorted(set(checks) - validations)
        if bad:
            problems.append(f"{where}: {kind} takes no validations {bad}")
        if "required" in checks and not isinstance(checks["required"], bool):
            problems.append(f"{where}: validations.required is true or false")
    problems += _options_problems(where, kind, attributes)
    return problems


def _options_problems(
    where: str, kind: str, attributes: Mapping[str, Any]
) -> list[str]:
    options = attributes.get("options")
    if kind == "dropdown":
        if (
            not isinstance(options, list)
            or not options
            or not all(isinstance(o, str) and o for o in options)
        ):
            return [f"{where}: dropdown options are a non-empty list of text"]
        if len(set(options)) != len(options):
            return [f"{where}: dropdown options are distinct"]
        default = attributes.get("default")
        if default is not None and (
            not isinstance(default, int) or not 0 <= default < len(options)
        ):
            return [f"{where}: default is the index of an option"]
    if kind == "checkboxes":
        if not isinstance(options, list) or not options:
            return [f"{where}: checkboxes options are a non-empty list"]
        for option in options:
            if (
                not isinstance(option, dict)
                or not isinstance(option.get("label"), str)
                or set(option) - {"label", "required"}
            ):
                return [f"{where}: each checkbox is a label and an optional required"]
    return []


def form_problems(
    rel: str, data: Any, declared: Sequence[Label]
) -> tuple[list[str], dict[str, Any]]:
    """The problems of one issue form, and its elements by id."""
    if not isinstance(data, dict):
        return [f"{rel}: a mapping with name, description and body"], {}
    problems = [f"{rel}: missing {key}" for key in sorted(TOP_REQUIRED - set(data))]
    problems += [
        f"{rel}: unknown top-level key {key!r}"
        for key in sorted(set(data) - TOP_REQUIRED - TOP_OPTIONAL)
    ]
    for key in ("name", "description"):
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            problems.append(f"{rel}: {key} is text")
    names = {label.name for label in declared}
    wanted = data.get("labels", [])
    if isinstance(wanted, str):
        wanted = [w.strip() for w in wanted.split(",") if w.strip()]
    if not isinstance(wanted, list):
        problems.append(f"{rel}: labels is a list")
        wanted = []
    for label in wanted:
        if label not in names:
            # GitHub would drop it silently rather than create it.
            problems.append(f"{rel}: label {label!r} is not in .github/labels.yml")
    body = data.get("body")
    elements: dict[str, Any] = {}
    if not isinstance(body, list) or not body:
        return [*problems, f"{rel}: body is a non-empty list"], elements
    ids: set[str] = set()
    field_labels: set[str] = set()
    for i, element in enumerate(body):
        problems += _element_problems(f"{rel}: body[{i}]", element, ids, field_labels)
        if isinstance(element, dict) and isinstance(element.get("id"), str):
            elements[element["id"]] = element
    if all(isinstance(e, dict) and e.get("type") == "markdown" for e in body):
        problems.append(f"{rel}: body needs a field that is not markdown")
    return problems, elements


def load_forms(root: Path) -> tuple[dict[str, Any], list[str]]:
    """Every form under FORMS_DIR by stem, config.yml aside, and read problems."""
    forms: dict[str, Any] = {}
    problems: list[str] = []
    folder = root / FORMS_DIR
    for path in sorted(folder.glob("*.y*ml")):
        if path.name in ("config.yml", "config.yaml"):
            continue
        rel = f"{FORMS_DIR}/{path.name}"
        if path.suffix != ".yml":
            problems.append(f"{rel}: forms end in .yml here")
        data, bad = _yaml(path, rel)
        problems += bad
        forms[path.stem] = data
    return forms, problems


def issues_problems(root: Path, declared: Sequence[Label]) -> list[str]:
    """Every form parses as an issue form, names only declared labels, is the set
    github.toml [issues] forms lists, and config.yml matches blank_issues."""
    try:
        issues = tomllib.loads((root / GITHUB_CONFIG).read_text("utf-8")).get(
            "issues", {}
        )
    except (OSError, tomllib.TOMLDecodeError) as e:
        return [f"{GITHUB_CONFIG}: cannot read: {e.__class__.__name__}"]
    forms, problems = load_forms(root)
    listed = set(issues.get("forms") or ())
    if set(forms) != listed:
        problems.append(
            f"{FORMS_DIR}: the forms are {sorted(forms)}, but {GITHUB_CONFIG} "
            f"[issues] forms says {sorted(listed)}"
        )
    seen_names: dict[str, str] = {}
    for stem, data in forms.items():
        rel = f"{FORMS_DIR}/{stem}.yml"
        if data is None:
            continue
        found, _ = form_problems(rel, data, declared)
        problems += found
        name = data.get("name") if isinstance(data, dict) else None
        if isinstance(name, str):
            if name in seen_names:
                problems.append(f"{rel}: name {name!r} is also {seen_names[name]}'s")
            seen_names[name] = stem
    problems += chooser_problems(root, bool(issues.get("blank_issues", False)))
    return problems


def chooser_problems(root: Path, blank_issues: bool) -> list[str]:
    data, problems = _yaml(root / CHOOSER, CHOOSER)
    if problems:
        return problems
    if not isinstance(data, dict):
        return [f"{CHOOSER}: a mapping"]
    problems = [
        f"{CHOOSER}: unknown key {key!r}" for key in sorted(set(data) - CHOOSER_KEYS)
    ]
    if data.get("blank_issues_enabled") is not blank_issues:
        problems.append(
            f"{CHOOSER}: blank_issues_enabled is not {str(blank_issues).lower()}, "
            f"as {GITHUB_CONFIG} [issues] blank_issues says"
        )
    for i, link in enumerate(data.get("contact_links") or []):
        if not isinstance(link, dict) or set(link) != LINK_KEYS:
            problems.append(f"{CHOOSER}: contact_links[{i}] is name, url and about")
        elif not str(link["url"]).startswith("https://"):
            problems.append(f"{CHOOSER}: contact_links[{i}] url is https")
    return problems


def pull_request_problems(root: Path) -> list[str]:
    """The template opens with the tag line and carries the acceptance section
    and the owner's one merge command."""
    try:
        text = (root / PR_TEMPLATE).read_text(encoding="utf-8")
    except OSError:
        return [f"{PR_TEMPLATE}: missing"]
    problems: list[str] = []
    if not TAG_LINE.match(text):
        problems.append(f"{PR_TEMPLATE}: the first line is the tag line `order: <id>`")
    for heading in ("## What and why", "## Acceptance", "## For the owner"):
        if f"\n{heading}\n" not in text:
            problems.append(f"{PR_TEMPLATE}: no {heading!r} section")
    if "--squash --match-head-commit" not in text:
        problems.append(
            f"{PR_TEMPLATE}: the merge command is squash, pinned to the head"
        )
    return problems


def em_dash_problems(root: Path) -> list[str]:
    paths = [root / PR_TEMPLATE, *sorted((root / FORMS_DIR).glob("*.yml"))]
    return [
        f"{p.relative_to(root).as_posix()}: an em dash"
        for p in paths
        if p.is_file() and EM_DASH in p.read_text(encoding="utf-8")
    ]


def lint(root: Path) -> list[str]:
    """Everything `tac github lint` judges: labels, forms, chooser, PR template."""
    try:
        declared = load_labels(root)
    except Bad as e:
        return [str(e)]
    return [
        *labels_problems(root, declared),
        *issues_problems(root, declared),
        *pull_request_problems(root),
        *em_dash_problems(root),
    ]
