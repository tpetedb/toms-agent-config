"""Build condition C5: `tac doctor` fetches each active branch ruleset by id and
fails on a missing or non-empty bypass_actors, a tag-only ruleset, and a
ruleset without code-owner review that someone must approve, or without the
verify and work checks from the GitHub Actions app."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

import tac.doctor
from tac.doctor import Status, ruleset_status, session_identity_status
from tac.github import (
    OWNER_STEP,
    ApiError,
    GhLogin,
    desired_ruleset,
    gh_auth_status,
    judge_rulesets,
    parse_gh_auth,
    ruleset_problems,
)
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
    status, detail = judged(FixtureTransport(rulesets(load("rulesets_list_empty"), {})))
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


# ---- a ruleset that looks complete but asks for nothing that counts


@pytest.mark.parametrize(
    ("fixture", "message"),
    [
        # A required check, but not ci.yml's: any integration can post "lint".
        ("ruleset_unrelated_check", "the 'verify' check is not required"),
        # The right names, from any source: a forged status satisfies them.
        ("ruleset_any_source_checks", "'verify' check may come from any integration"),
        ("ruleset_other_app_check", "'work' check may come from 424242"),
        # Code-owner review that asks for no approving review asks for nothing.
        ("ruleset_zero_approvals", "asks for no approving review"),
    ],
)
def test_a_ruleset_with_a_bypass_fails(fixture: str, message: str) -> None:
    transport = FixtureTransport(rulesets(load("rulesets_list"), {101: load(fixture)}))
    status, detail = judged(transport)
    assert status is Status.FAIL
    assert "ruleset 101 'tac-default-branch' does not hold" in detail
    assert message in detail


def test_the_work_check_has_to_be_required_as_well() -> None:
    holds = load("ruleset_holds")
    checks = rule(holds, "required_status_checks")["parameters"]
    checks["required_status_checks"] = checks["required_status_checks"][:1]
    status, detail = judged(standard(rules=holds["rules"]))
    assert status is Status.FAIL
    assert "the 'work' check is not required" in detail


def test_a_second_source_for_a_required_check_fails() -> None:
    holds = load("ruleset_holds")
    checks = rule(holds, "required_status_checks")["parameters"]
    checks["required_status_checks"].append({"context": "verify"})
    status, detail = judged(standard(rules=holds["rules"]))
    assert status is Status.FAIL
    assert "'verify' check may come from any integration" in detail


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


# ---- the gh identity an agent session can reach (section 8)


def auth_json(login: str, scopes: str = "repo", state: str = "success") -> str:
    """The shape `gh auth status --json hosts` prints (cli/cli, auth status)."""
    entry = {
        "state": state,
        "active": True,
        "host": "github.com",
        "login": login,
        "tokenSource": "keyring",
        "scopes": scopes,
        "gitProtocol": "https",
    }
    return json.dumps({"hosts": {"github.com": [entry]}})


def fake_gh(folder: Path, stdout: str, code: int = 0) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "out.json").write_text(stdout)
    gh = folder / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{folder}/args"\n'
        f'cat "{folder}/out.json"\n'
        f"exit {code}\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return gh


def test_parse_reads_the_active_working_login() -> None:
    login = parse_gh_auth(auth_json("demo-bot", "repo, admin:org"))
    assert login == GhLogin("demo-bot", ("repo", "admin:org"))


@pytest.mark.parametrize(
    "text",
    ['{"hosts": {}}', auth_json("x", state="error"), "not json", '{"hosts": []}'],
)
def test_parse_sees_no_login(text: str) -> None:
    assert parse_gh_auth(text) is None


def test_gh_auth_status_asks_for_json_and_never_for_the_token(tmp_path: Path) -> None:
    gh = fake_gh(tmp_path / "bin", auth_json("demo-bot"))
    assert gh_auth_status(str(gh), {"PATH": "/usr/bin:/bin"}) == GhLogin(
        "demo-bot", ("repo",)
    )
    args = (tmp_path / "bin/args").read_text().split()
    assert args[:2] == ["auth", "status"] and "--json" in args
    assert "--show-token" not in args and "-t" not in args


def test_gh_auth_status_that_fails_is_an_error(tmp_path: Path) -> None:
    gh = fake_gh(tmp_path / "bin", "", code=1)
    with pytest.raises(ApiError):
        gh_auth_status(str(gh), {"PATH": "/usr/bin:/bin"})


def owner_reply(login: str = "example", kind: str = "User") -> FixtureTransport:
    repo = load("repo")
    repo["owner"] = dict(repo["owner"], login=login, type=kind)
    return FixtureTransport({("GET", f"repos/{REPO}"): [Reply(200, repo)]})


def session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login: GhLogin | None,
    public: FixtureTransport | None = None,
) -> tuple[Status, str]:
    fake_gh(tmp_path / "bin", "{}")
    monkeypatch.setattr(tac.doctor, "gh_auth_status", lambda _gh, _env: login)
    monkeypatch.setattr(tac.doctor, "origin_repository", lambda _root: REPO)
    environ = {"CLAUDECODE": "1", "PATH": str(tmp_path / "bin")}
    return session_identity_status(tmp_path, environ, public or owner_reply())


def test_the_owner_login_in_an_agent_session_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, detail = session(tmp_path, monkeypatch, GhLogin("Example", ("repo",)))
    assert status is Status.FAIL
    assert "an admin identity" in detail and "owns the repository" in detail
    assert "Q14" in detail


def test_an_admin_scope_in_an_agent_session_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = GhLogin("someone", ("repo", "admin:org"))
    status, detail = session(
        tmp_path, monkeypatch, login, owner_reply(kind="Organization")
    )
    assert status is Status.FAIL and "admin:org" in detail


def test_the_machine_account_in_an_agent_session_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, detail = session(tmp_path, monkeypatch, GhLogin("tac-bot", ("repo",)))
    assert status is Status.PASS and "machine account tac-bot" in detail


def test_another_login_in_an_agent_session_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, detail = session(tmp_path, monkeypatch, GhLogin("someone", ("repo",)))
    assert status is Status.UNKNOWN and "only the machine account" in detail


def test_a_logged_out_gh_in_an_agent_session_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, _ = session(tmp_path, monkeypatch, None)
    assert status is Status.PASS


def test_no_gh_in_an_agent_session_passes(tmp_path: Path) -> None:
    environ = {"CLAUDECODE": "1", "PATH": str(tmp_path)}
    assert session_identity_status(tmp_path, environ)[0] is Status.PASS


def test_the_owner_terminal_is_not_judged_by_the_session_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tac.doctor, "agent_session", lambda _env: None)
    monkeypatch.setattr(tac.doctor, "gh_auth_status", pytest.fail)
    status, detail = session_identity_status(tmp_path, {"PATH": str(tmp_path)})
    assert status is Status.PASS and "not an agent session" in detail
