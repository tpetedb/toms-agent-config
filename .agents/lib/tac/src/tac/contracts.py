"""The strict-subset lint for model-facing contracts (build condition C4).

A model-facing contract is a handoff payload a model must produce. Codex takes
it through `--output-schema`, which accepts only the OpenAI strict subset of
JSON Schema, so a schema outside it fails at the API on the first cross-provider
review. Every such contract lives under contracts/handoffs/ and is held here to
that subset: an object root that is not anyOf, every property required,
`additionalProperties: false` on every object, nullable written as
`[type, "null"]`, only the keywords the subset lists, a depth of at most 10, and
the documented size limits: at most 5000 object properties in all, 120,000
characters across every property name, definition name, enum value and const
value, 1000 enum values in all, and 15,000 characters in one string enum of more
than 250 values. The lint walks every place a subschema can sit, refused
keywords included, so a loose object hidden under one is named too.

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
# The size limits the Structured Outputs guide documents; past any of them the
# API refuses the schema, so the lint refuses it first.
# https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas
MAX_PROPERTIES = 5000
MAX_STRING_TOTAL = 120_000
MAX_ENUM_VALUES = 1000
# A string enum of more than LARGE_ENUM values may hold at most this many characters.
LARGE_ENUM = 250
MAX_LARGE_ENUM_CHARS = 15_000
# The keywords the strict subset lists: the types, enum, const and anyOf, the
# string, number and array constraints, $defs and $ref, plus annotations and the
# object keywords; the guide counts const values in its size limit. Anything else
# is refused at any depth, since the API errors on an unsupported keyword rather
# than ignoring it.
# https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas
SUPPORTED = frozenset(
    {
        "$schema",
        "$id",
        "$ref",
        "$comment",
        "title",
        "description",
        "type",
        "enum",
        "const",
        "anyOf",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "definitions",
        "$defs",
        "pattern",
        "format",
        "multipleOf",
        "maximum",
        "exclusiveMaximum",
        "minimum",
        "exclusiveMinimum",
        "minItems",
        "maxItems",
    }
)
# Positions whose value is one subschema, a list of them, or a map of names to
# them. Each is walked whether the keyword is supported or not.
_ONE = ("additionalProperties", "additionalItems", "not", "if", "then", "else")
_ONE += ("contains", "propertyNames")
_MANY = ("anyOf", "oneOf", "allOf")
_NAMED = ("properties", "patternProperties", "definitions", "$defs")
_NAMED += ("dependencies", "dependentSchemas")
# Positions that hold the value of a property or an array element, so they sit
# one level deeper; a combinator or a definition does not.
_DEEPER = frozenset({"properties", "patternProperties", "additionalProperties"})
_DEEPER |= {"items", "additionalItems", "contains"}


def _walk(
    schema: Any, where: str, depth: int
) -> Iterator[tuple[str, int, dict[str, Any]]]:
    if not isinstance(schema, dict):
        return
    yield where, depth, schema

    def inner(key: str) -> int:
        if key in ("definitions", "$defs"):
            # Definitions are reached through $ref; they count from the root.
            return 1
        return depth + 1 if key in _DEEPER else depth

    for key in _ONE:
        yield from _walk(schema.get(key), f"{where}/{key}", inner(key))
    items = schema.get("items")
    if isinstance(items, list):
        for i, sub in enumerate(items):
            yield from _walk(sub, f"{where}/items/{i}", inner("items"))
    else:
        yield from _walk(items, f"{where}/items", inner("items"))
    for key in _MANY:
        subs = schema.get(key)
        for i, sub in enumerate(subs if isinstance(subs, list) else []):
            yield from _walk(sub, f"{where}/{key}/{i}", inner(key))
    for key in _NAMED:
        subs = schema.get(key)
        for name, sub in (subs if isinstance(subs, dict) else {}).items():
            yield from _walk(sub, f"{where}/{key}/{name}", inner(key))


def _is_object(schema: dict[str, Any]) -> bool:
    kind = schema.get("type")
    return kind == "object" or (isinstance(kind, list) and "object" in kind)


def _text_length(value: Any) -> int:
    # A string counts its characters; any other enum or const value its JSON text.
    return len(value) if isinstance(value, str) else len(json.dumps(value))


def _size_problems(subs: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """The documented totals, summed over every subschema the walk reached."""
    problems = []
    properties = strings = enum_values = 0
    for at, sub in subs:
        props = sub.get("properties")
        names = list(props) if isinstance(props, dict) else []
        properties += len(names)
        strings += sum(len(n) for n in names)
        for key in ("definitions", "$defs"):
            defs = sub.get(key)
            strings += sum(len(n) for n in defs) if isinstance(defs, dict) else 0
        if "const" in sub:
            strings += _text_length(sub["const"])
        enum = sub.get("enum")
        if not isinstance(enum, list):
            continue
        enum_values += len(enum)
        chars = sum(_text_length(v) for v in enum)
        strings += chars
        text_chars = sum(len(v) for v in enum if isinstance(v, str))
        if len(enum) > LARGE_ENUM and text_chars > MAX_LARGE_ENUM_CHARS:
            problems.append(
                f"{at}: an enum of more than {LARGE_ENUM} values holds "
                f"{text_chars} characters, over {MAX_LARGE_ENUM_CHARS}"
            )
    if properties > MAX_PROPERTIES:
        problems.append(
            f"/: {properties} object properties, over {MAX_PROPERTIES} in all"
        )
    if strings > MAX_STRING_TOTAL:
        problems.append(
            f"/: names, enum and const values hold {strings} characters, "
            f"over {MAX_STRING_TOTAL}"
        )
    if enum_values > MAX_ENUM_VALUES:
        problems.append(f"/: {enum_values} enum values, over {MAX_ENUM_VALUES} in all")
    return problems


def strict_subset_problems(schema: Any) -> list[str]:
    """Each place `schema` leaves the OpenAI strict subset, as `path: reason`."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return ["/: the root must be an object schema"]
    problems = []
    if "anyOf" in schema:
        problems.append("/: the root may not be anyOf")
    reached = []
    for where, depth, sub in _walk(schema, "", 1):
        at = where or "/"
        reached.append((at, sub))
        if depth > MAX_DEPTH:
            problems.append(f"{at}: nested deeper than {MAX_DEPTH}")
        for key in sorted(set(sub) - SUPPORTED):
            if key == "nullable":
                problems.append(f'{at}: write nullable as ["<type>", "null"]')
            else:
                problems.append(f"{at}: {key} is outside the strict subset")
        if isinstance(sub.get("items"), list):
            problems.append(f"{at}: items must be one schema, not a tuple")
        if _is_object(sub) or "properties" in sub:
            props = set((sub.get("properties") or {}).keys())
            required = set(sub.get("required") or [])
            for name in sorted(props - required):
                problems.append(f"{at}: property {name} must be required")
            if sub.get("additionalProperties") is not False:
                problems.append(f"{at}: additionalProperties must be false")
    return problems + _size_problems(reached)


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
