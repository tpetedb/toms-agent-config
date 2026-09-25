"""Every contract is valid JSON Schema draft-07; the model-facing ones, and only
those, are held to the OpenAI strict subset (build condition C4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from tac.contracts import (
    MODEL_FACING_DIR,
    check_contracts,
    draft07_problems,
    model_facing,
    strict_subset_problems,
)
from tac.draft07 import DRAFT_07, later_keywords
from tac.handoff import (
    REPAIR_TEMPLATE,
    check_templates,
    contract_json_schemas,
    handoff_templates,
    read_handoff_template,
    schema_text,
)
from tests._syncproject import REPO

EXAMPLES = REPO / "tests" / "fixtures" / "handoffs"
ALL = sorted((REPO / "contracts").rglob("*.schema.json"))
FACING = model_facing(REPO)


def rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def test_there_are_contracts_of_both_kinds() -> None:
    assert FACING, "no model-facing contract"
    assert set(ALL) - set(FACING), "no config, receipt or envelope contract"


@pytest.mark.parametrize("path", ALL, ids=rel)
def test_every_contract_is_draft_07_and_nothing_later(path: Path) -> None:
    schema = json.loads(path.read_text("utf-8"))
    Draft7Validator.check_schema(schema)
    assert schema["$schema"] == DRAFT_07
    assert later_keywords(schema) == []


@pytest.mark.parametrize("path", FACING, ids=rel)
def test_every_model_facing_contract_is_in_the_strict_subset(path: Path) -> None:
    assert strict_subset_problems(json.loads(path.read_text("utf-8"))) == []


def test_the_checker_finds_nothing_in_the_shipped_contracts() -> None:
    assert check_contracts(REPO) == []


def test_the_strict_lint_never_reads_a_config_or_envelope_contract() -> None:
    # These keep optional keys and defaults; the lint would reject them, and
    # check_contracts passing proves it never looked.
    loose = [
        p
        for p in set(ALL) - set(FACING)
        if strict_subset_problems(json.loads(p.read_text("utf-8")))
    ]
    assert any(p.name == "envelope.schema.json" for p in loose)
    assert any(p.parent.name == "config" for p in loose)
    assert check_contracts(REPO) == []


# ---------------------------------------------------------------- the subset by keyword

LOOSE = {"type": "object", "properties": {"a": {"type": "string"}}}
TIGHT = {**LOOSE, "required": ["a"], "additionalProperties": False}


def _root(prop: dict) -> dict:
    """A strict root with one property `p` set to `prop`."""
    return {
        "type": "object",
        "properties": {"p": prop},
        "required": ["p"],
        "additionalProperties": False,
    }


# One schema per keyword the subset leaves out, each at depth two, and the
# message that must name it.
OUTSIDE = [
    ("allOf", {"allOf": [{"type": "string"}]}, "/properties/p: allOf"),
    ("not", {"not": {"type": "string"}}, "/properties/p: not"),
    ("if", {"if": {"type": "string"}}, "/properties/p: if"),
    ("then", {"then": {"type": "string"}}, "/properties/p: then"),
    ("else", {"else": {"type": "string"}}, "/properties/p: else"),
    (
        "dependentRequired",
        {**TIGHT, "dependentRequired": {"a": []}},
        "/properties/p: dependentRequired",
    ),
    (
        "dependentSchemas",
        {**TIGHT, "dependentSchemas": {"a": {"type": "object"}}},
        "/properties/p: dependentSchemas",
    ),
    (
        "dependencies",
        {**TIGHT, "dependencies": {"a": ["a"]}},
        "/properties/p: dependencies",
    ),
    ("oneOf", {"oneOf": [{"type": "string"}]}, "/properties/p: oneOf"),
    (
        "patternProperties",
        {**TIGHT, "patternProperties": {"^x": {"type": "string"}}},
        "/properties/p: patternProperties",
    ),
    ("minLength", {"type": "string", "minLength": 1}, "/properties/p: minLength"),
]


@pytest.mark.parametrize(("keyword", "prop", "named"), OUTSIDE, ids=lambda v: v)
def test_each_keyword_outside_the_subset_is_named(
    keyword: str, prop: dict, named: str
) -> None:
    problems = strict_subset_problems(_root(prop))
    assert f"{named} is outside the strict subset" in problems, problems
    # The same keyword at the root is named too, not only when nested.
    top = strict_subset_problems({**_root({"type": "string"}), keyword: prop[keyword]})
    assert f"/: {keyword} is outside the strict subset" in top, top


def test_nullable_is_named() -> None:
    problems = strict_subset_problems(_root({"type": "string", "nullable": True}))
    assert problems == ['/properties/p: write nullable as ["<type>", "null"]']


# A loose object, with no required list and no additionalProperties: false, in
# every position a subschema can sit. Each must be found and named.
HIDDEN = [
    ("oneOf", {"oneOf": [LOOSE, {"type": "string"}]}, "/properties/p/oneOf/0"),
    ("anyOf", {"anyOf": [LOOSE, {"type": "null"}]}, "/properties/p/anyOf/0"),
    ("allOf", {"allOf": [LOOSE]}, "/properties/p/allOf/0"),
    ("tuple items", {"type": "array", "items": [LOOSE]}, "/properties/p/items/0"),
    ("items", {"type": "array", "items": LOOSE}, "/properties/p/items"),
    (
        "additionalProperties",
        {"type": "object", "properties": {}, "additionalProperties": LOOSE},
        "/properties/p/additionalProperties",
    ),
    (
        "patternProperties",
        {**TIGHT, "patternProperties": {"^x": LOOSE}},
        "/properties/p/patternProperties/^x",
    ),
    ("not", {"not": LOOSE}, "/properties/p/not"),
    ("then", {"then": LOOSE}, "/properties/p/then"),
    (
        "definitions",
        {**TIGHT, "definitions": {"d": LOOSE}},
        "/properties/p/definitions/d",
    ),
]


@pytest.mark.parametrize(("where", "prop", "at"), HIDDEN, ids=lambda v: v)
def test_a_loose_object_is_found_wherever_it_sits(
    where: str, prop: dict, at: str
) -> None:
    problems = strict_subset_problems(_root(prop))
    assert f"{at}: property a must be required" in problems, problems
    assert f"{at}: additionalProperties must be false" in problems, problems


def test_tuple_items_and_a_root_anyof_are_named() -> None:
    tuple_items = _root({"type": "array", "items": [{"type": "string"}]})
    assert strict_subset_problems(tuple_items) == [
        "/properties/p: items must be one schema, not a tuple"
    ]
    root = {**_root({"type": "string"}), "anyOf": [{"type": "object"}]}
    assert "/: the root may not be anyOf" in strict_subset_problems(root)


def _nested(levels: int) -> tuple[dict, str]:
    """A string `levels` containers below `p`, alternating array items and an
    object's additionalProperties, and the path of that innermost string."""
    schema: dict = {"type": "string"}
    steps: list[str] = []
    for i in range(levels):
        if i % 2:
            schema = {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": schema,
            }
            steps.append("additionalProperties")
        else:
            schema = {"type": "array", "items": schema}
            steps.append("items")
    return schema, "/properties/p/" + "/".join(reversed(steps))


def test_depth_counts_through_array_items_and_additional_properties() -> None:
    # The root is depth 1 and p is depth 2, so nine containers under p put the
    # innermost string at 11, one past the limit, and eight put it at 10.
    deep, at = _nested(9)
    assert "/items/" in at and "/additionalProperties/" in at
    problems = strict_subset_problems(_root(deep))
    too_deep = [p for p in problems if "nested deeper" in p]
    assert too_deep == [f"{at}: nested deeper than 10"], problems
    at_limit, _ = _nested(8)
    problems = strict_subset_problems(_root(at_limit))
    assert not [p for p in problems if "nested deeper" in p], problems


def _flat(names: list[str]) -> dict:
    """A strict root whose properties are `names`, each a string."""
    return {
        "type": "object",
        "properties": {n: {"type": "string"} for n in names},
        "required": names,
        "additionalProperties": False,
    }


def test_more_than_5000_object_properties_are_refused() -> None:
    # The root's own properties count, and so do a nested object's.
    at_limit = _flat([f"p{i}" for i in range(4999)])
    at_limit["properties"]["p0"] = _flat(["q"])
    assert strict_subset_problems(at_limit) == []
    over = _flat([f"p{i}" for i in range(5000)])
    over["properties"]["p0"] = _flat(["q"])
    problems = strict_subset_problems(over)
    assert problems == ["/: 5001 object properties, over 5000 in all"], problems


def test_names_enum_and_const_values_share_a_120000_character_budget() -> None:
    # A property name, a definition name, an enum value and a const value all
    # count: 4 + 6 + 3 + 1 characters beside one long name.
    def schema(long: int) -> dict:
        root = _flat(["x" * long, "unit"])
        root["properties"]["unit"] = {"enum": ["abc"]}
        root["definitions"] = {"shared": {"type": "string"}}
        root["properties"]["x" * long] = {"const": "k"}
        return root

    budget = 120_000 - (4 + 6 + 3 + 1)
    # The whole list: const is in the subset, so nothing else may be named.
    problems = strict_subset_problems(schema(budget))
    assert problems == [], problems
    problems = strict_subset_problems(schema(budget + 1))
    assert problems == [
        "/: names, enum and const values hold 120001 characters, over 120000"
    ], problems


def test_const_is_in_the_strict_subset() -> None:
    # The Structured Outputs guide counts const values in its size limit, and
    # the official openai-node parser emits const in strict schemas.
    assert strict_subset_problems(_root({"const": "x"})) == []
    assert strict_subset_problems(_root({"type": "integer", "const": 3})) == []


def test_more_than_1000_enum_values_in_all_are_refused() -> None:
    # Counted across every enum, so two enums of 500 are at the limit.
    def schema(second: int) -> dict:
        root = _flat(["a", "b"])
        root["properties"]["a"] = {"enum": [f"a{i}" for i in range(500)]}
        root["properties"]["b"] = {"enum": [f"b{i}" for i in range(second)]}
        return root

    assert strict_subset_problems(schema(500)) == []
    assert strict_subset_problems(schema(501)) == [
        "/: 1001 enum values, over 1000 in all"
    ]


def test_a_string_enum_over_250_values_holds_at_most_15000_characters() -> None:
    def enum(count: int, chars: int) -> dict:
        # `count` distinct values whose lengths sum to `chars`.
        values = [f"{i:03d}" for i in range(count)]
        values[0] += "z" * (chars - 3 * count)
        return _root({"enum": values})

    assert strict_subset_problems(enum(251, 15_000)) == []
    # At 250 values the rule does not apply, however long they are.
    assert strict_subset_problems(enum(250, 15_001)) == []
    assert strict_subset_problems(enum(251, 15_001)) == [
        "/properties/p: an enum of more than 250 values holds 15001 characters,"
        " over 15000"
    ]


def test_a_contract_hiding_a_loose_object_under_one_of_fails_the_check(
    tmp_path: Path,
) -> None:
    folder = tmp_path / MODEL_FACING_DIR
    folder.mkdir(parents=True)
    signoff = json.loads((REPO / MODEL_FACING_DIR / "signoff.schema.json").read_text())
    first = next(iter(signoff["properties"]))
    signoff["properties"][first] = {"oneOf": [LOOSE, {"type": "string"}]}
    (folder / "signoff.schema.json").write_text(json.dumps(signoff))
    problems = check_contracts(tmp_path)
    at = f"{MODEL_FACING_DIR}/signoff.schema.json#/properties/{first}"
    assert f"{at}: oneOf is outside the strict subset" in problems
    assert f"{at}/oneOf/0: property a must be required" in problems
    assert f"{at}/oneOf/0: additionalProperties must be false" in problems


def test_a_model_facing_contract_outside_draft_07_is_named(tmp_path: Path) -> None:
    folder = tmp_path / MODEL_FACING_DIR
    folder.mkdir(parents=True)
    strict = json.loads((REPO / MODEL_FACING_DIR / "signoff.schema.json").read_text())
    (folder / "ok.schema.json").write_text(json.dumps(strict))
    later = {**strict, "properties": {**strict["properties"], "x": {"$defs": {}}}}
    later["required"] = [*strict["required"], "x"]
    (folder / "later.schema.json").write_text(json.dumps(later))
    undeclared = {k: v for k, v in strict.items() if k != "$schema"}
    (folder / "bare.schema.json").write_text(json.dumps(undeclared))
    broken = {**strict, "properties": {**strict["properties"], "team": {"type": 3}}}
    (folder / "broken.schema.json").write_text(json.dumps(broken))
    problems = check_contracts(tmp_path)
    at = f"{MODEL_FACING_DIR}/"
    assert f"{at}bare.schema.json: $schema must be {DRAFT_07}" in problems
    assert f"{at}later.schema.json: /properties/x: $defs is not draft-07" in problems
    assert any(p.startswith(f"{at}broken.schema.json: not a valid") for p in problems)
    assert not any(p.startswith(f"{at}ok.schema.json") for p in problems)
    assert draft07_problems([]) == ["not a JSON Schema object"]


@pytest.mark.parametrize("path", FACING, ids=rel)
def test_every_model_facing_contract_has_an_example_that_meets_it(path: Path) -> None:
    name = path.name.removesuffix(".schema.json")
    example = json.loads((EXAMPLES / f"{name}.json").read_text("utf-8"))
    validator = Draft7Validator(json.loads(path.read_text("utf-8")))
    assert list(validator.iter_errors(example)) == []
    # Every key is required, so dropping any one fails; an unknown key fails too.
    first = sorted(example)[0]
    assert list(validator.iter_errors({k: v for k, v in example.items() if k != first}))
    assert list(validator.iter_errors({**example, "extra": 1}))


def test_the_envelope_and_human_item_contracts_match_the_models() -> None:
    for path, body in contract_json_schemas().items():
        committed = (REPO / path).read_text("utf-8")
        assert committed == schema_text(body), "run: uv run tac handoff schema --write"


def test_every_template_names_contracts_that_exist() -> None:
    assert check_templates(REPO) == []


def test_every_model_facing_contract_is_read_or_written_by_a_template() -> None:
    named: set[str] = set()
    for template in handoff_templates(REPO):
        if template == REPAIR_TEMPLATE:
            continue
        read = read_handoff_template(REPO, template)
        named |= {read.writes, *read.reads}
    facing = {p.name.removesuffix(".schema.json") for p in FACING}
    assert facing - named == set()
