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
