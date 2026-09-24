"""`tac github apply`, driven only by recorded responses: it is never run live."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tac.github_cli
from tac.cli import cli
from tac.github import (
    GITHUB_ACTIONS_APP_ID,
    REQUIRED_CHECKS,
    RULESET_NAME,
    apply_ruleset,
    desired_ruleset,
)
from tests._github_fixtures import REPO, FixtureTransport, Reply, load, rulesets

PERMISSION = f"repos/{REPO}/collaborators/tac-bot/permission"


def transport(
    listed: list[Any],
    after: dict[str, Any] | None = None,
    role: str = "collaborator_write",
    permission: Reply | None = None,
) -> FixtureTransport:
    """The identity, the list before the write, and the ruleset read back after."""
    applied = after if after is not None else load("ruleset_holds")
    replies = rulesets(load("rulesets_list"), {101: applied})
    replies[("GET", f"repos/{REPO}/rulesets?per_page=100")] = [
        Reply(200, listed),
        Reply(200, load("rulesets_list")),
    ]
    replies[("GET", PERMISSION)] = [permission or Reply(200, load(role))]
    replies[("POST", f"repos/{REPO}/rulesets")] = [Reply(201, applied)]
    replies[("PUT", f"repos/{REPO}/rulesets/101")] = [Reply(200, applied)]
    return FixtureTransport(replies)


def test_the_ruleset_turns_bypass_off_for_everyone() -> None:
    body = desired_ruleset()
    assert body["bypass_actors"] == []
    assert body["target"] == "branch" and body["enforcement"] == "active"
    assert body["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    kinds = [r["type"] for r in body["rules"]]
    assert kinds == [
        "deletion",
        "non_fast_forward",
        "required_linear_history",
        "pull_request",
        "required_status_checks",
    ]


def test_the_ruleset_requires_code_owners_and_squash_only() -> None:
    [pr] = [r for r in desired_ruleset()["rules"] if r["type"] == "pull_request"]
    assert pr["parameters"]["require_code_owner_review"] is True
    assert pr["parameters"]["dismiss_stale_reviews_on_push"] is True
    assert pr["parameters"]["allowed_merge_methods"] == ["squash"]


def test_the_required_checks_come_from_github_actions_only() -> None:
    [rsc] = [
        r for r in desired_ruleset()["rules"] if r["type"] == "required_status_checks"
    ]
    checks = rsc["parameters"]["required_status_checks"]
    assert [c["context"] for c in checks] == list(REQUIRED_CHECKS)
    assert {c["integration_id"] for c in checks} == {GITHUB_ACTIONS_APP_ID}
    assert rsc["parameters"]["strict_required_status_checks_policy"] is True


def test_the_required_checks_are_the_ci_jobs(repo: Path) -> None:
    workflow = yaml.safe_load((repo / ".github/workflows/ci.yml").read_text())
    assert set(REQUIRED_CHECKS) == set(workflow["jobs"])


def test_apply_creates_the_ruleset_when_none_exists_and_judges_it() -> None:
    fixture = transport(listed=[])
    outcome = apply_ruleset(fixture, REPO, "tac-bot", desired_ruleset())
    assert outcome.ok
    assert fixture.writes() == [("POST", f"repos/{REPO}/rulesets", desired_ruleset())]
    assert ("GET", f"repos/{REPO}/rulesets/101", None) in fixture.calls
    assert "created ruleset 101 'tac-default-branch'" in outcome.lines


def test_apply_updates_its_own_ruleset_in_place() -> None:
    fixture = transport(listed=load("rulesets_list"))
    outcome = apply_ruleset(fixture, REPO, "tac-bot", desired_ruleset())
    assert outcome.ok and outcome.ruleset_id == 101
    assert fixture.writes() == [
        ("PUT", f"repos/{REPO}/rulesets/101", desired_ruleset())
    ]


def test_apply_leaves_a_tag_ruleset_of_the_same_name_alone() -> None:
    tag = dict(load("rulesets_list")[1], name=RULESET_NAME)
    fixture = transport(listed=[tag])
    apply_ruleset(fixture, REPO, "tac-bot", desired_ruleset())
    assert [w[0] for w in fixture.writes()] == ["POST"]


def test_apply_refuses_an_admin_bot_and_writes_nothing() -> None:
    fixture = transport(listed=[], role="collaborator_admin")
    outcome = apply_ruleset(fixture, REPO, "tac-bot", desired_ruleset())
    assert not outcome.ok
    assert "never admin" in outcome.lines[0]
    assert fixture.writes() == []


def test_apply_names_q14_when_the_bot_is_missing() -> None:
    fixture = transport(listed=[], permission=Reply(404))
    outcome = apply_ruleset(fixture, REPO, "tac-bot", desired_ruleset())
    assert not outcome.ok
    assert "Q14" in outcome.lines[0]
    assert fixture.writes() == []


def test_apply_fails_when_the_ruleset_read_back_does_not_hold() -> None:
    applied = load("ruleset_holds")
    applied["bypass_actors"] = [{"actor_id": 5, "actor_type": "RepositoryRole"}]
    fixture = transport(listed=[], after=applied)
    outcome = apply_ruleset(fixture, REPO, "tac-bot", desired_ruleset())
    assert not outcome.ok
    assert any("bypass_actors is not empty" in line for line in outcome.lines)


def test_apply_reports_a_refused_write() -> None:
    fixture = transport(listed=[])
    fixture.replies[("POST", f"repos/{REPO}/rulesets")] = [Reply(422)]
    outcome = apply_ruleset(fixture, REPO, "tac-bot", desired_ruleset())
    assert not outcome.ok
    assert "was not applied" in outcome.lines[-1]


# ---- the command line


def test_dry_run_prints_the_ruleset_and_calls_nothing() -> None:
    result = CliRunner().invoke(cli, ["github", "apply", "--dry-run"])
    assert result.exit_code == 0
    assert json.loads(result.output) == desired_ruleset()


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_apply_is_refused_inside_an_agent_session(
    marker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(marker, "1")
    monkeypatch.setattr(tac.github_cli, "gh_transport", pytest.fail)
    result = CliRunner().invoke(cli, ["github", "apply", "--repo", REPO])
    assert result.exit_code != 0
    assert "refused inside an agent session" in result.output


def owner_terminal(monkeypatch: pytest.MonkeyPatch, fixture: FixtureTransport) -> None:
    monkeypatch.setattr(tac.github_cli, "agent_session", lambda _env: None)
    monkeypatch.setattr(tac.github_cli, "gh_transport", lambda: fixture)


def test_apply_from_the_owner_terminal_exits_zero_when_it_holds(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_terminal(monkeypatch, transport(listed=[]))
    args = ["github", "apply", "--repo", REPO, "--root", str(repo)]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert "holds" in result.output


def test_apply_exits_non_zero_on_an_admin_bot(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_terminal(monkeypatch, transport(listed=[], role="collaborator_admin"))
    args = ["github", "apply", "--repo", REPO, "--root", str(repo)]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 1
    assert "no ruleset was written" in result.output


def test_apply_refuses_without_codeowners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_terminal(monkeypatch, transport(listed=[]))
    args = ["github", "apply", "--repo", REPO, "--root", str(tmp_path)]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0
    assert "CODEOWNERS is missing" in result.output


def test_apply_refuses_a_malformed_repository(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_terminal(monkeypatch, transport(listed=[]))
    args = ["github", "apply", "--repo", "../x", "--root", str(repo)]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0
    assert "not owner/name" in result.output
