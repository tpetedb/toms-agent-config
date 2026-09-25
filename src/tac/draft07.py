"""JSON Schema draft-07 from a pydantic model: the form every file in contracts/ takes.

Pydantic writes draft 2020-12. A draft-07 validator ignores the keywords it does
not know instead of failing on them, so a schema that slipped one in would check
less than it says; `later_keywords` finds them, and the tests keep it empty.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, JsonValue

DRAFT_07 = "http://json-schema.org/draft-07/schema#"

# Keywords that draft 2019-09 or 2020-12 added or renamed; draft-07 skips them.
LATER_KEYWORDS = frozenset(
    {
        "$anchor",
        "$defs",
        "$dynamicAnchor",
        "$dynamicRef",
        "$recursiveAnchor",
        "$recursiveRef",
        "$vocabulary",
        "dependentRequired",
        "dependentSchemas",
        "maxContains",
        "minContains",
        "prefixItems",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
# Keywords whose value maps names to subschemas, so their keys are names, not keywords.
_NAMED = frozenset({"properties", "patternProperties", "definitions", "dependencies"})


def draft07(model: type[BaseModel]) -> dict[str, JsonValue]:
    """The model's validation schema, with its definitions where draft-07 reads them."""
    schema = model.model_json_schema(ref_template="#/definitions/{model}")
    definitions = schema.pop("$defs", {})
    return {"$schema": DRAFT_07, **schema, "definitions": definitions}


def _subschemas(schema: Any, where: str) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(schema, list):
        for i, item in enumerate(schema):
            yield from _subschemas(item, f"{where}/{i}")
        return
    if not isinstance(schema, dict):
        return
    yield where, schema
    for key, value in schema.items():
        if key in _NAMED and isinstance(value, dict):
            for name, sub in value.items():
                yield from _subschemas(sub, f"{where}/{key}/{name}")
        elif key not in {"enum", "const", "default", "examples", "required"}:
            yield from _subschemas(value, f"{where}/{key}")


def later_keywords(schema: dict[str, Any]) -> list[str]:
    """Each keyword a draft-07 validator would skip, as `path: keyword`."""
    return [
        f"{where or '/'}: {key}"
        for where, sub in _subschemas(schema, "")
        for key in sub
        if key in LATER_KEYWORDS
    ]
