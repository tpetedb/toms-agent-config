"""The request tac github apply sends, and every recorded reply the tests serve,
checked against the OpenAPI description GitHub publishes.

apply is never run live, so this is the check of its body before the owner's
first real run. The schemas are cut by scripts/github_openapi_subset.py.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tac.github import desired_ruleset
from tests._github_fixtures import FIXTURES, load

SUBSET = json.loads((FIXTURES / "openapi_rulesets.json").read_text(encoding="utf-8"))
SCHEMAS: dict[str, Any] = SUBSET["schemas"]


def closed(schema: Any) -> Any:
    """The schema with every object closed, so a key GitHub does not declare,
    which it would reject or silently drop, fails here instead."""
    if isinstance(schema, list):
        return [closed(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: closed(v) for k, v in schema.items()}
    if "properties" in out and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


def errors(schema: Any, instance: Any) -> list[str]:
    validator = Draft202012Validator(schema)
    return [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator.iter_errors(instance)
    ]


def declared(schema: Any, rule: str) -> set[str]:
    """The parameter names the description declares for one rule type."""
    for variant in schema["properties"]["rules"]["items"]["oneOf"]:
        if variant["properties"]["type"].get("enum") == [rule]:
            return set(variant["properties"].get("parameters", {}).get("properties"))
    raise AssertionError(f"no rule {rule!r} in the description")


def test_the_subset_names_its_source() -> None:
    assert SUBSET["_source"].startswith("github/rest-api-description")
    assert len(SUBSET["_commit"]) == 40


@pytest.mark.parametrize("name", ["ruleset_create", "ruleset_update"])
def test_the_applied_ruleset_matches_the_published_request(name: str) -> None:
    assert errors(closed(SCHEMAS[name]), desired_ruleset()) == []


def test_the_two_keys_to_watch_are_declared_by_github() -> None:
    schema = SCHEMAS["ruleset_create"]
    assert "allowed_merge_methods" in declared(schema, "pull_request")
    [check] = [
        v
        for v in schema["properties"]["rules"]["items"]["oneOf"]
        if v["properties"]["type"].get("enum") == ["required_status_checks"]
    ]
    item = check["properties"]["parameters"]["properties"]["required_status_checks"]
    assert item["items"]["properties"]["integration_id"]["type"] == "integer"


def test_the_pull_request_rule_sends_every_required_parameter() -> None:
    [rule] = [r for r in desired_ruleset()["rules"] if r["type"] == "pull_request"]
    [variant] = [
        v
        for v in SCHEMAS["ruleset_create"]["properties"]["rules"]["items"]["oneOf"]
        if v["properties"]["type"].get("enum") == ["pull_request"]
    ]
    required = set(variant["properties"]["parameters"]["required"])
    assert required <= set(rule["parameters"])


def test_an_undeclared_key_is_caught() -> None:
    body = copy.deepcopy(desired_ruleset())
    body["rules"][3]["parameters"]["bypass_for_admins"] = False
    assert errors(closed(SCHEMAS["ruleset_create"]), body)


@pytest.mark.parametrize(
    ("fixture", "schema"),
    [
        ("ruleset_holds", "ruleset"),
        ("ruleset_tag", "ruleset"),
        ("rulesets_list", "ruleset_list"),
        ("rulesets_list_empty", "ruleset_list"),
        ("collaborator_write", "collaborator_permission"),
        ("collaborator_admin", "collaborator_permission"),
        ("repo", "repository"),
    ],
)
def test_each_recorded_reply_has_the_published_shape(fixture: str, schema: str) -> None:
    assert errors(SCHEMAS[schema], load(fixture)) == []
