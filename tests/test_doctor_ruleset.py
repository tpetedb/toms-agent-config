"""Build condition C5: `tac doctor` fetches each active branch ruleset by id and
fails on a missing or non-empty bypass_actors, a tag-only ruleset, and a
ruleset without code-owner review or required checks."""

from __future__ import annotations

from typing import Any

import pytest

from tac.doctor import Status, ruleset_status
from tac.github import OWNER_STEP, desired_ruleset, judge_rulesets, ruleset_problems
from tests._github_fixtures import (
    REPO,
    FixtureTransport,
    Reply,
    load,
    rulesets,
    standard,
)


def judged(transport: FixtureTransport) -> tuple[Status, str]:
    return ruleset_status(judge_rulesets(transport, REPO), "fixture")


def rule(body: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(r for r in body["rules"] if r["type"] == kind)


def test_a_holding_ruleset_passes() -> None:
    status, detail = judged(standard())
    assert status is Status.PASS
    assert "ruleset 101 'tac-default-branch' holds" in detail


def test_each_active_branch_ruleset_is_fetched_by_id_and_tags_are_not() -> None:
    transport = standard()
    report = judge_rulesets(transport, REPO)
    fetched = [path for _, path, _ in transport.calls]
    assert f"repos/{REPO}/rulesets/101" in fetched
    assert f"repos/{REPO}/rulesets/102" not in fetched
    assert report.fetched == (101,)


def test_no_ruleset_fails_and_names_the_owner_step() -> None:
    status, detail = judged(FixtureTransport(rulesets([], {})))
    assert status is Status.FAIL
    assert "no active branch ruleset" in detail
    assert OWNER_STEP in detail
    assert "tac github apply" in detail


def test_a_tag_only_ruleset_does_not_count() -> None:
    listed = [s for s in load("rulesets_list") if s["target"] == "tag"]
    status, detail = judged(
        FixtureTransport(rulesets(listed, {102: load("ruleset_tag")}))
    )
    assert status is Status.FAIL
    assert "only tag rulesets, which do not count" in detail


def test_a_ruleset_only_in_evaluate_mode_does_not_count() -> None:
    listed = load("rulesets_list")
    for summary in listed:
        summary["enforcement"] = "evaluate"
    status, detail = judged(FixtureTransport(rulesets(listed, {})))
    assert status is Status.FAIL
    assert "no active branch ruleset" in detail


def test_a_missing_bypass_actors_fails_with_the_owner_token() -> None:
    holds = load("ruleset_holds")
    del holds["bypass_actors"]
    transport = FixtureTransport(rulesets(load("rulesets_list"), {101: holds}))
    status, detail = judged(transport)
    assert status is Status.FAIL
    assert "bypass_actors missing" in detail


def test_a_missing_bypass_actors_is_unknown_without_a_token() -> None:
    holds = load("ruleset_holds")
    del holds["bypass_actors"]
    transport = FixtureTransport(rulesets(load("rulesets_list"), {101: holds}))
    transport.authenticated = False
    status, detail = judged(transport)
    assert status is Status.UNKNOWN
    assert "own terminal on the host" in detail


@pytest.mark.parametrize(
    "actor",
    [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"},
        {"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"},
        {"actor_id": 9, "actor_type": "Integration", "bypass_mode": "pull_request"},
    ],
)
def test_a_non_empty_bypass_actors_fails(actor: dict[str, Any]) -> None:
    status, detail = judged(standard(bypass_actors=[actor]))
    assert status is Status.FAIL
    assert f"bypass_actors is not empty ({actor['actor_type']}:" in detail


def test_a_ruleset_without_code_owner_review_fails() -> None:
    holds = load("ruleset_holds")
    rule(holds, "pull_request")["parameters"]["require_code_owner_review"] = False
    status, detail = judged(standard(rules=holds["rules"]))
    assert status is Status.FAIL
    assert "code-owner review" in detail


def test_a_ruleset_without_a_pull_request_rule_fails() -> None:
    holds = load("ruleset_holds")
    rules = [r for r in holds["rules"] if r["type"] != "pull_request"]
    status, detail = judged(standard(rules=rules))
    assert status is Status.FAIL
    assert "code-owner review" in detail


def test_a_ruleset_without_required_checks_fails() -> None:
    holds = load("ruleset_holds")
    rules = [r for r in holds["rules"] if r["type"] != "required_status_checks"]
    status, detail = judged(standard(rules=rules))
    assert status is Status.FAIL
    assert "no required status checks" in detail


def test_a_required_checks_rule_naming_no_check_fails() -> None:
    holds = load("ruleset_holds")
    rule(holds, "required_status_checks")["parameters"]["required_status_checks"] = []
    status, detail = judged(standard(rules=holds["rules"]))
    assert status is Status.FAIL
    assert "no required status checks" in detail


def test_one_failing_ruleset_among_two_fails() -> None:
    listed = load("rulesets_list")
    second = dict(listed[0], id=103, name="extra")
    extra = load("ruleset_holds")
    extra.update(id=103, name="extra", bypass_actors=[{"actor_id": 5}])
    transport = FixtureTransport(
        rulesets([*listed, second], {101: load("ruleset_holds"), 103: extra})
    )
    status, detail = judged(transport)
    assert status is Status.FAIL
    assert "ruleset 101 'tac-default-branch' holds" in detail
    assert "ruleset 103 'extra' does not hold" in detail


def test_a_ruleset_that_misses_the_default_branch_fails() -> None:
    conditions = {"ref_name": {"include": ["refs/heads/release"], "exclude": []}}
    status, detail = judged(standard(conditions=conditions))
    assert status is Status.FAIL
    assert "covers the default branch 'main'" in detail


def test_a_ruleset_naming_the_default_branch_by_ref_covers_it() -> None:
    conditions = {"ref_name": {"include": ["refs/heads/main"], "exclude": []}}
    assert judged(standard(conditions=conditions))[0] is Status.PASS


def test_a_list_that_cannot_be_read_is_unknown() -> None:
    transport = FixtureTransport(
        {("GET", f"repos/{REPO}/rulesets?per_page=100"): [Reply(403)]}
    )
    status, detail = judged(transport)
    assert status is Status.UNKNOWN
    assert "cannot list rulesets" in detail


def test_the_ruleset_tac_applies_passes_its_own_judge() -> None:
    assert ruleset_problems(desired_ruleset()) == []
