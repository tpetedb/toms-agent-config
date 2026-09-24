"""The strict-subset lint for model-facing contracts (build condition C4).

A model-facing contract is a handoff payload a model must produce. Codex takes
it through `--output-schema`, which accepts only the OpenAI strict subset of
JSON Schema, so a schema outside it fails at the API on the first cross-provider
review. Every such contract lives under contracts/handoffs/ and is held here to
that subset: an object root, every property required, `additionalProperties:
false` on every object, nullable written as `[type, "null"]`, none of the
composition or conditional keywords, and a depth of at most 10.

Config and receipt schemas are not model-facing: they keep optional keys and
defaults, and this lint never reads them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

from tac.draft07 import DRAFT_07, later_keywords

MODEL_FACING_DIR = "contracts/handoffs"
MAX_DEPTH = 10
# Keywords the strict subset refuses, whatever they are nested in.
REFUSED = frozenset(
    {
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "dependentRequired",
        "dependentSchemas",
        # draft-07's spelling of the two dependent* keywords.
        "dependencies",
    }
)
_SUBSCHEMA_LISTS = ("anyOf",)
_SUBSCHEMA_MAPS = ("properties", "definitions", "$defs")


def _walk(
    schema: Any, where: str, depth: int
) -> Iterator[tuple[str, int, dict[str, Any]]]:
    if not isinstance(schema, dict):
        return
    yield where, depth, schema
    for key in _SUBSCHEMA_MAPS:
        for name, sub in (schema.get(key) or {}).items():
            # Definitions are reached through $ref; they count from the root.
            inner = depth if key != "properties" else depth + 1
            yield from _walk(sub, f"{where}/{key}/{name}", inner)
    if isinstance(schema.get("items"), dict):
        yield from _walk(schema["items"], f"{where}/items", depth + 1)
    for key in _SUBSCHEMA_LISTS:
        for i, sub in enumerate(schema.get(key) or []):
            yield from _walk(sub, f"{where}/{key}/{i}", depth)


def _is_object(schema: dict[str, Any]) -> bool:
    kind = schema.get("type")
    return kind == "object" or (isinstance(kind, list) and "object" in kind)


def strict_subset_problems(schema: Any) -> list[str]:
    """Each place `schema` leaves the OpenAI strict subset, as `path: reason`."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return ["/: the root must be an object schema"]
    problems = []
    for where, depth, sub in _walk(schema, "", 1):
        at = where or "/"
        if depth > MAX_DEPTH:
            problems.append(f"{at}: nested deeper than {MAX_DEPTH}")
        for key in sorted(REFUSED & set(sub)):
            problems.append(f"{at}: {key} is outside the strict subset")
        if sub.get("nullable") is not None:
            problems.append(f'{at}: write nullable as ["<type>", "null"]')
        if _is_object(sub) or "properties" in sub:
            props = set((sub.get("properties") or {}).keys())
            required = set(sub.get("required") or [])
            for name in sorted(props - required):
                problems.append(f"{at}: property {name} must be required")
            if sub.get("additionalProperties") is not False:
                problems.append(f"{at}: additionalProperties must be false")
    return problems


def draft07_problems(schema: Any) -> list[str]:
    """A contract declares draft-07, is a valid one, and uses nothing later,
    which a draft-07 validator would skip without a word."""
    if not isinstance(schema, dict):
        return ["not a JSON Schema object"]
    problems = []
    if schema.get("$schema") != DRAFT_07:
        problems.append(f"$schema must be {DRAFT_07}")
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as e:
        problems.append(f"not a valid draft-07 schema: {e.message}")
    problems += [f"{p} is not draft-07" for p in later_keywords(schema)]
    return problems


def model_facing(root: Path) -> list[Path]:
    folder = root / MODEL_FACING_DIR
    return sorted(folder.rglob("*.schema.json")) if folder.is_dir() else []


def check_contracts(root: Path) -> list[str]:
    """The strict-subset lint over every model-facing contract, and only those."""
    problems = []
    for path in model_facing(root):
        rel = path.relative_to(root).as_posix()
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            problems.append(f"{rel}: not valid JSON: {e}")
            continue
        problems += [f"{rel}: {p}" for p in draft07_problems(schema)]
        problems += [f"{rel}#{p}" for p in strict_subset_problems(schema)]
    return problems
