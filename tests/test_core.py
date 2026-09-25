"""The configuration core: load, merge with the profile, the floor and its waivers."""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from jsonschema import Draft7Validator

from tac import standards
from tac.adapters import claude_session
from tac.cli import cli
from tac.config import (
    CONFIG_DIR,
    CONFIG_SCHEMAS,
    CONTRACTS_DIR,
    config_json_schemas,
    container_evidence,
    floor_check,
    load_config,
    schema_name,
)
from tac.doctor import Status, check_agents_config
from tac.draft07 import DRAFT_07, later_keywords
from tac.standards import FLOOR_FILE, STANDARDS_FILE, Rule
from tac.work import KNOBS_FILE, TEAMS_FILE, Bad
from tests._syncproject import ultracode_off

TODAY = dt.date(2026, 9, 24)
GIT = (
    "git",
    "-c",
    "user.name=fixture",
    "-c",
    "user.email=fixture@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "init.defaultBranch=main",
)


@pytest.fixture
def camp(repo: Path, tmp_path: Path) -> Path:
    """A copy of the shipped configuration, to break one thing at a time."""
    root = tmp_path / "project"
    for rel in (CONFIG_DIR, ".agents/skills"):
        shutil.copytree(repo / rel, root / rel)
    for rel in (KNOBS_FILE, FLOOR_FILE):
        shutil.copy2(repo / rel, root / rel)
    return root


def edit(root: Path, rel: str, old: str, new: str) -> None:
    path = root / rel
    text = path.read_text()
    assert old in text, f"{old!r} not in {rel}"
    path.write_text(text.replace(old, new, 1))


def refused(root: Path, *needles: str) -> str:
    with pytest.raises(Bad) as caught:
        load_config(root, today=TODAY)
    message = str(caught.value)
    for needle in needles:
        assert needle in message, message
    return message


# ---------------------------------------------------------------- load


def test_the_shipped_configuration_loads(repo: Path) -> None:
    config = load_config(repo, today=TODAY)
    assert config.knobs.profile.active == "standard"
    assert config.profile.name == "standard"
    assert set(config.profiles) == {"enterprise", "standard", "yolo"}
    assert set(config.roles) == {
        "chief",
        "director",
        "builder",
        "reviewer",
        "manager",
        "scout",
    }
    assert set(config.gates) == {"tool"}
    assert {t.id for t in config.teams.teams} == {"core", "harness", "docs"}
    assert config.notes == ()


def test_native_delegation_is_off_in_enterprise_and_guarded_elsewhere(
    repo: Path,
) -> None:
    profiles = load_config(repo, today=TODAY).profiles
    assert profiles["enterprise"].native_delegation == "off"
    assert profiles["standard"].native_delegation == "guarded"
    assert profiles["yolo"].native_delegation == "guarded"


def test_profiles_are_complete_files_with_the_same_keys(repo: Path) -> None:
    import tomllib

    keys = {
        name: set(
            tomllib.loads((repo / CONFIG_DIR / f"profiles/{name}.toml").read_text())
        )
        for name in ("enterprise", "standard", "yolo")
    }
    assert keys["enterprise"] == keys["standard"] == keys["yolo"]
    assert "gates" not in keys["standard"]


def test_an_unknown_key_fails_loudly(camp: Path) -> None:
    edit(camp, KNOBS_FILE, 'active = "standard"', 'active = "standard"\nactiv = 1')
    refused(camp, KNOBS_FILE, "unknown key activ")


def test_a_value_of_the_wrong_type_fails(camp: Path) -> None:
    edit(camp, KNOBS_FILE, "max_local_agents = 4", 'max_local_agents = "4"')
    refused(camp, KNOBS_FILE, "max_local_agents")


def test_a_profile_may_not_carry_gates(camp: Path) -> None:
    rel = f"{CONFIG_DIR}/profiles/standard.toml"
    edit(camp, rel, 'effort = "models"', 'effort = "models"\ngates = []')
    refused(camp, rel, "unknown key gates")


def test_a_misspelt_config_file_is_refused(camp: Path) -> None:
    (camp / CONFIG_DIR / "standard.toml").write_text("schema_version = 1\n")
    refused(camp, "standard.toml: not a file tac reads")


def test_a_missing_active_profile_is_named(camp: Path) -> None:
    (camp / CONFIG_DIR / "profiles/yolo.toml").unlink()
    edit(camp, KNOBS_FILE, 'active = "standard"', 'active = "yolo"')
    refused(camp, "profiles/yolo.toml: missing")


def test_a_pointer_must_name_the_file_it_points_at(camp: Path) -> None:
    edit(camp, KNOBS_FILE, 'policy = "default"', 'policy = "cheap"')
    refused(camp, "[models] policy = 'cheap'", "named 'default'")


def test_a_key_two_files_set_is_refused(camp: Path) -> None:
    # A team called "table" would land on [teams] table from the knob file.
    edit(camp, TEAMS_FILE, "[teams.docs]", "[teams.table]")
    refused(camp, "teams.table", "set in both")


def test_an_elapsed_timeout_is_never_consent(camp: Path) -> None:
    edit(camp, KNOBS_FILE, "timeout_is_consent = false", "timeout_is_consent = true")
    refused(camp, "timeout_is_consent")


def test_the_chief_may_not_merge(camp: Path) -> None:
    edit(camp, KNOBS_FILE, '"open-issue", "comment"', '"merge", "comment"')
    refused(camp, "chief_may")


def test_a_harness_without_a_provider_cannot_be_enforced(camp: Path) -> None:
    edit(
        camp,
        KNOBS_FILE,
        'enforced = ["claude", "codex"]',
        'enforced = ["claude", "codex", "gemini"]',
    )
    edit(camp, KNOBS_FILE, '"pi", "gemini"]', '"pi"]')
    refused(camp, "gemini is enforced")


def test_the_tally_is_kept_by_the_lead_director(camp: Path) -> None:
    edit(camp, KNOBS_FILE, 'tally_by = "fable"', 'tally_by = "astra"')
    refused(camp, "tally_by = 'astra'", "'fable' the lead")


def test_every_director_has_a_seat(camp: Path) -> None:
    edit(
        camp,
        KNOBS_FILE,
        '["fable", "astra", "opus"]',
        '["fable", "astra", "opus", "sol"]',
    )
    refused(camp, "director 'sol' has no [directors.sol]")


def test_every_seated_role_has_a_charter(camp: Path) -> None:
    (camp / CONFIG_DIR / "roles/scout.toml").unlink()
    refused(camp, "roles/scout.toml: missing")


def test_an_effort_the_provider_does_not_accept_fails(camp: Path) -> None:
    rel = f"{CONFIG_DIR}/models.toml"
    edit(
        camp,
        rel,
        'anthropic = { model = "claude-opus-5-5", effort = "medium" }',
        'anthropic = { model = "claude-opus-5-5", effort = "ultra" }',
    )
    refused(camp, "roles.manager.anthropic: effort 'ultra'")


# ---------------------------------------------------------------- ultracode

CHIEF_SEAT = (
    'anthropic = { model = "claude-opus-5-5", effort = "xhigh", ultracode = true }'
)
CODEX_BUILDER = 'openai = { model = "gpt-6-sol", effort = "high" }'
MANAGER_SEAT = 'anthropic = { model = "claude-opus-5-5", effort = "medium" }'


def test_the_chief_starts_with_ultracode_at_xhigh(repo: Path) -> None:
    config = load_config(repo, today=TODAY)
    chief = config.models.roles[config.knobs.governance.chief].seats()["anthropic"]
    assert (chief.model, chief.effort, chief.ultracode) == (
        "claude-opus-5-5",
        "xhigh",
        True,
    )
    # Every other seat keeps its effort and leaves ultracode off.
    others = [
        f"{role}.{pid}"
        for role, spec in config.models.roles.items()
        for pid, found in spec.seats().items()
        if found.ultracode and role != "chief"
    ]
    assert others == []
    assert claude_session(config, "chief") == [
        "claude",
        "--agent",
        "chief",
        "--effort",
        "ultracode",
    ]


def test_ultracode_is_refused_on_openai(camp: Path) -> None:
    edit(
        camp,
        f"{CONFIG_DIR}/models.toml",
        CODEX_BUILDER,
        'openai = { model = "gpt-6-sol", effort = "xhigh", ultracode = true }',
    )
    refused(camp, "roles.builder.openai: ultracode is a Claude Code setting")


def test_ultracode_needs_effort_xhigh(camp: Path) -> None:
    rel = f"{CONFIG_DIR}/models.toml"
    edit(camp, rel, CHIEF_SEAT, CHIEF_SEAT.replace('"xhigh"', '"high"'))
    refused(camp, 'roles.chief.anthropic: ultracode = true needs effort = "xhigh"')


def test_ultracode_is_refused_on_a_subagent_only_role(camp: Path) -> None:
    edit(
        camp,
        f"{CONFIG_DIR}/roles/manager.toml",
        'runs = "headless"',
        'runs = "subagent"',
    )
    # A subagent-only role loads with an effort alone.
    load_config(camp, today=TODAY)
    edit(
        camp,
        f"{CONFIG_DIR}/models.toml",
        MANAGER_SEAT,
        CHIEF_SEAT,
    )
    refused(camp, "roles.manager.anthropic sets ultracode, but roles/manager.toml")


def test_without_ultracode_the_chief_starts_at_its_effort(camp: Path) -> None:
    edit(
        camp,
        f"{CONFIG_DIR}/models.toml",
        CHIEF_SEAT,
        'anthropic = { model = "claude-opus-5-5", effort = "high" }',
    )
    config = load_config(camp, today=TODAY)
    assert claude_session(config, "chief") == [
        "claude",
        "--agent",
        "chief",
        "--effort",
        "high",
    ]
    # Only the host session is started by hand; the others have no command here.
    assert claude_session(config, "builder") is None
    assert claude_session(config, "director") is None


def test_explain_a_role_prints_its_seat_and_launch(repo: Path) -> None:
    runner = CliRunner()
    told = runner.invoke(cli, ["explain", "roles.chief", "--root", str(repo)])
    assert told.exit_code == 0, told.output
    head = told.output.split("\n\n")[0].splitlines()
    assert head[0].startswith(
        "roles.chief on anthropic, through claude  # .agents/config/models.toml:"
    )
    assert head[1:5] == [
        "  model:     claude-opus-5-5",
        "  effort:    xhigh",
        "  ultracode: true",
        "  launch:    claude --agent chief --effort ultracode  (just chief)",
    ]
    assert "more tokens and takes longer" in told.output
    assert 'roles.chief.runs = "host-session"' in told.output


def test_launch_command_prints_what_just_chief_runs(camp: Path, repo: Path) -> None:
    runner = CliRunner()
    plain = runner.invoke(cli, ["config", "launch-command", "--root", str(repo)])
    assert plain.exit_code == 0, plain.output
    assert plain.output == "claude --agent chief --effort ultracode\n"
    as_json = runner.invoke(
        cli, ["config", "launch-command", "chief", "--json", "--root", str(repo)]
    )
    assert json.loads(as_json.output) == [
        "claude",
        "--agent",
        "chief",
        "--effort",
        "ultracode",
    ]
    builder = runner.invoke(
        cli, ["config", "launch-command", "builder", "--root", str(repo)]
    )
    assert builder.exit_code == 1
    assert 'runs = "host-session"' in builder.output
    # An edit to models.toml is the whole change: the command follows it.
    edit(
        camp,
        f"{CONFIG_DIR}/models.toml",
        CHIEF_SEAT,
        'anthropic = { model = "claude-opus-5-5", effort = "max" }',
    )
    edited = runner.invoke(cli, ["config", "launch-command", "--root", str(camp)])
    assert edited.output == "claude --agent chief --effort max\n"


def test_just_chief_runs_the_command_tac_reads_from_models_toml(repo: Path) -> None:
    justfile = (repo / "justfile").read_text()
    recipe = re.search(r"^chief \*args:\n((?:    .*\n)+)", justfile, re.M)
    assert recipe is not None
    assert recipe.group(1) == (
        '    {{ shell(tac + " config launch-command") }} {{ args }}\n'
    )


def test_a_team_cannot_outgrow_the_host_budget(camp: Path) -> None:
    edit(camp, KNOBS_FILE, "max_local_agents = 4", "max_local_agents = 1")
    refused(camp, "max_workers = 2 is above [teams] max_local_agents = 1")


def test_a_team_skill_must_exist(camp: Path) -> None:
    edit(camp, TEAMS_FILE, 'skills = ["work-order"]', 'skills = ["no-such-skill"]')
    refused(camp, "skill 'no-such-skill'")


def test_a_host_profile_may_not_bypass_permissions(camp: Path) -> None:
    rel = f"{CONFIG_DIR}/profiles/standard.toml"
    edit(camp, rel, 'defaultMode = "acceptEdits"', 'defaultMode = "bypassPermissions"')
    edit(
        camp,
        rel,
        'sandbox_mode = "workspace-write"',
        'sandbox_mode = "danger-full-access"',
    )
    refused(camp, "refused on the host", "bypassPermissions", "danger-full-access")


# ---------------------------------------------------------------- container profiles

# What a test passes as the evidence of a container: the marker files are
# root-owned, so no test creates them, and every test injects the answer.
IN_A_CONTAINER = "/.dockerenv (Docker)"


@pytest.mark.parametrize(
    ("active", "isolation"),
    [("yolo", "disposable-container"), ("enterprise", "container")],
)
def test_a_container_profile_is_refused_as_active_outside_a_container(
    camp: Path, active: str, isolation: str
) -> None:
    ultracode_off(camp)
    edit(camp, KNOBS_FILE, 'active = "standard"', f'active = "{active}"')
    with pytest.raises(Bad) as caught:
        load_config(camp, today=TODAY, container=None)
    message = str(caught.value)
    for needle in (
        f"{CONFIG_DIR}/profiles/{active}.toml",
        f'isolation = "{isolation}"',
        "/.dockerenv",
        "/run/.containerenv",
        "never evidence",
    ):
        assert needle in message, message


@pytest.mark.parametrize("active", ["yolo", "enterprise"])
def test_a_container_profile_loads_inside_a_container(camp: Path, active: str) -> None:
    ultracode_off(camp)
    edit(camp, KNOBS_FILE, 'active = "standard"', f'active = "{active}"')
    config = load_config(camp, today=TODAY, container=IN_A_CONTAINER)
    assert config.profile.name == active
    assert config.container == IN_A_CONTAINER


def test_every_profile_file_stays_valid_while_the_host_one_is_active(
    camp: Path,
) -> None:
    # The refusal is about the active profile only: yolo.toml and enterprise.toml
    # are still read and checked as files on the host.
    config = load_config(camp, today=TODAY, container=None)
    assert config.profile.name == "standard"
    assert {"yolo", "enterprise"} <= set(config.profiles)
    rel = f"{CONFIG_DIR}/profiles/yolo.toml"
    edit(camp, rel, 'network = "allowlist"', 'network = "open"')
    with pytest.raises(Bad, match=re.escape("profiles/yolo.toml")):
        load_config(camp, today=TODAY, container=None)


def test_an_environment_variable_is_never_container_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Any process can set a variable, so setting one must change nothing; the
    # answer is compared with itself, so the test holds inside a container too.
    before = container_evidence()
    for name, value in (
        ("TAC_CONTAINER", "1"),
        ("container", "podman"),
        ("CI", "true"),
        ("REMOTE_CONTAINERS", "true"),
    ):
        monkeypatch.setenv(name, value)
    assert container_evidence() == before


def test_only_a_root_owned_regular_file_is_container_evidence(tmp_path: Path) -> None:
    mine = tmp_path / "dockerenv"
    mine.write_text("")
    linked = tmp_path / "linked"
    linked.symlink_to("/etc/hosts")
    # A file the running user made, a link to a root-owned file and a root-owned
    # folder are not evidence; a root-owned regular file stands in for a marker.
    assert container_evidence(((str(mine), "Docker"),)) is None
    assert container_evidence(((str(linked), "Docker"),)) is None
    assert container_evidence((("/usr", "Docker"),)) is None
    assert container_evidence((("/no/such/marker", "Docker"),)) is None
    assert container_evidence((("/etc/hosts", "Docker"),)) == "/etc/hosts (Docker)"


def test_config_check_refuses_yolo_on_the_host(
    camp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ultracode_off(camp)
    edit(camp, KNOBS_FILE, 'active = "standard"', 'active = "yolo"')
    monkeypatch.setattr("tac.config.container_evidence", lambda *_: None)
    done = CliRunner().invoke(cli, ["config", "check", "--root", str(camp)])
    assert done.exit_code == 1
    assert "profiles/yolo.toml" in done.output and "/.dockerenv" in done.output


# ---------------------------------------------------------------- mcp


def test_the_shipped_profiles_agree_with_mcp_toml(repo: Path) -> None:
    config = load_config(repo, today=TODAY)
    assert config.mcp.strict and not config.mcp.servers
    assert config.profiles["standard"].mcp_servers == "from-conf"
    assert config.profiles["enterprise"].mcp_servers == "none"


NO_SERVERS = "servers = {}"
ONE_SERVER = 'servers = { docs = { command = "/usr/bin/true" } }'


def test_a_profile_with_no_servers_refuses_a_listed_server(camp: Path) -> None:
    # Checked for every profile, since any one of them may become active.
    edit(camp, f"{CONFIG_DIR}/mcp.toml", NO_SERVERS, ONE_SERVER)
    refused(
        camp,
        f'{CONFIG_DIR}/profiles/enterprise.toml: mcp_servers = "none"',
        f"{CONFIG_DIR}/mcp.toml lists docs",
        "never load",
    )


def test_from_conf_needs_a_strict_mcp_toml(camp: Path) -> None:
    edit(camp, f"{CONFIG_DIR}/mcp.toml", "strict = true", "strict = false")
    message = refused(
        camp,
        f'{CONFIG_DIR}/profiles/standard.toml: mcp_servers = "from-conf"',
        f"{CONFIG_DIR}/mcp.toml says strict = false",
        "user-level servers",
    )
    assert "profiles/yolo.toml" in message
    assert "profiles/enterprise.toml" not in message


def test_no_prompts_needs_a_disposable_container(camp: Path) -> None:
    rel = f"{CONFIG_DIR}/profiles/standard.toml"
    edit(camp, rel, 'approvals = "external-effects"', 'approvals = "none"')
    refused(camp, 'approvals = "none" needs isolation = "disposable-container"')


def test_a_gate_runs_only_as_a_just_recipe(camp: Path) -> None:
    rel = f"{CONFIG_DIR}/gates/tool.toml"
    edit(camp, rel, 'argv = ["just", "gate-wheel"]', 'argv = ["bash", "-c", "true"]')
    refused(camp, "argv[0] must be just")


def test_a_deferred_check_is_off_and_names_its_question(repo: Path) -> None:
    import tomllib

    pack = tomllib.loads((repo / CONFIG_DIR / "gates/data-pipeline.toml").read_text())
    soda = pack["checks"]["soda-scan"]
    assert soda["enabled"] is False
    assert soda["waits_on"] == "Q13"


def test_the_active_packs_recipes_exist(repo: Path) -> None:
    justfile = (repo / "justfile").read_text()
    recipes = set(re.findall(r"^([a-z][a-z0-9-]*)(?:\s[^:]*)?:", justfile, re.M))
    config = load_config(repo, today=TODAY)
    for pack in config.gates.values():
        for check in pack.checks.values():
            if check.enabled:
                assert check.argv[1] in recipes, check.argv


# ---------------------------------------------------------------- merge


def test_the_active_profile_is_merged_under_profile(camp: Path) -> None:
    # Enterprise turns native delegation off, which ultracode's Workflow calls
    # cannot run under, so the project takes ultracode off first.
    ultracode_off(camp)
    edit(camp, KNOBS_FILE, 'active = "standard"', 'active = "enterprise"')
    config = load_config(camp, today=TODAY, container=IN_A_CONTAINER)
    [entry] = config.explain("profile.native_delegation")
    assert entry.value == "off"
    assert entry.source == f"{CONFIG_DIR}/profiles/enterprise.toml"
    assert entry.applies == "while profile.active = enterprise"
    assert "dispatch only through tac run" in entry.comment
    [active] = config.explain("profile.active")
    assert active.source == KNOBS_FILE


def test_explain_names_the_file_line_and_comment(repo: Path) -> None:
    config = load_config(repo, today=TODAY)
    [entry] = config.explain("profile.active")
    line = (repo / KNOBS_FILE).read_text().splitlines()[entry.line - 1]
    assert line.startswith('active = "standard"')
    assert "switching profile is this one line" in entry.comment


def test_explain_a_table_lists_every_key_under_it(repo: Path) -> None:
    config = load_config(repo, today=TODAY)
    paths = [e.path for e in config.explain("usage")]
    assert paths[:2] == ["usage.claude.slow_at", "usage.claude.stop_at"]
    assert "usage.max_age_s" in paths


def test_every_split_file_is_mounted(repo: Path) -> None:
    entries = load_config(repo, today=TODAY).entries
    for key in (
        "models.roles.builder.anthropic.model",
        "roles.chief.identity",
        "teams.core.owns",
        "work.base",
        "standards.commits.max_subject",
        "floor.tests",
        "gates.tool.checks.wheel.argv",
        "hooks.guard.python",
        "policy.paths.deny_write",
        "mcp.strict",
        "network.egress.allow",
        "runtime.env.codex_inherit",
        "github.merge.method",
        "probes.version.kind",
    ):
        assert key in entries, key


def test_the_cli_shows_and_explains(repo: Path) -> None:
    runner = CliRunner()
    shown = runner.invoke(cli, ["config", "show", "--root", str(repo)])
    assert shown.exit_code == 0, shown.output
    assert 'profile.active = "standard"    # .agents/config.toml:' in shown.output
    told = runner.invoke(cli, ["explain", "profile.active", "--root", str(repo)])
    assert told.exit_code == 0, told.output
    assert "from:    .agents/config.toml:" in told.output
    assert "switching profile is this one line" in told.output
    missing = runner.invoke(cli, ["explain", "profile.activ", "--root", str(repo)])
    assert missing.exit_code == 1
    assert "did you mean profile.active" in missing.output


def test_the_doctor_loads_the_whole_configuration(camp: Path, repo: Path) -> None:
    assert check_agents_config(repo)[0] is Status.PASS
    edit(camp, KNOBS_FILE, "quorum = 3", "quorum = 4")
    status, detail = check_agents_config(camp)
    assert status is Status.FAIL
    assert "does not load" in detail


# ---------------------------------------------------------------- the floor


def test_the_floor_wins_where_the_project_is_looser(camp: Path) -> None:
    edit(camp, STANDARDS_FILE, 'tests = "required"', 'tests = "optional"')
    config = load_config(camp, today=TODAY)
    assert config.standards["quality"]["tests"] == "required"
    [entry] = config.explain("standards.quality.tests")
    assert entry.value == "required"
    assert entry.source == FLOOR_FILE
    assert 'the floor wins over the project\'s "optional"' in entry.applies
    assert any("standards.quality.tests" in note for note in config.notes)


def test_the_project_may_tighten_the_floor(camp: Path) -> None:
    edit(camp, STANDARDS_FILE, "max_subject = 72", "max_subject = 60")
    config = load_config(camp, today=TODAY)
    [entry] = config.explain("standards.commits.max_subject")
    assert (entry.value, entry.source) == (60, STANDARDS_FILE)


def test_a_number_takes_the_stricter_bound(camp: Path) -> None:
    edit(camp, STANDARDS_FILE, "max_subject = 72", "max_subject = 100")
    assert load_config(camp, today=TODAY).standards["commits"]["max_subject"] == 72


def test_a_set_is_the_union(camp: Path) -> None:
    edit(
        camp,
        STANDARDS_FILE,
        'trailers_required = ["Co-Authored-By"]',
        'trailers_required = ["Signed-off-by"]',
    )
    trailers = load_config(camp, today=TODAY).standards["commits"]["trailers_required"]
    assert trailers == ["Co-Authored-By", "Signed-off-by"]


def test_a_boolean_gate_holds_if_either_side_holds_it(camp: Path) -> None:
    edit(camp, STANDARDS_FILE, "em_dashes = false", "em_dashes = true")
    config = load_config(camp, today=TODAY)
    assert config.standards["code"]["markdown"]["em_dashes"] is False


def test_an_enum_value_outside_the_order_fails(camp: Path) -> None:
    edit(camp, STANDARDS_FILE, 'tests = "required"', 'tests = "sometimes"')
    refused(camp, "tests = 'sometimes'", "optional < required")


def test_the_floor_refuses_an_unknown_key(camp: Path) -> None:
    edit(camp, FLOOR_FILE, 'lint = "required"', 'lint = "required"\nspeed = "fast"')
    refused(camp, "[floor] unknown key speed")


def test_rules_compare_each_kind() -> None:
    at_least = Rule("coverage", ("quality", "coverage"), "min")
    assert at_least.stricter(80, 90) == 90
    assert at_least.looser(70, 80)
    assert not at_least.looser(90, 80)
    gate = standards.RULES["em_dashes"]
    assert gate.looser(True, False)
    assert not gate.looser(False, True)
    trailers = standards.RULES["trailers_required"]
    assert trailers.looser(["a"], ["a", "b"])
    assert not trailers.looser(["b", "a", "c"], ["a", "b"])


def floor(**values: object) -> standards.Floor:
    return standards.Floor(values, ())


def test_the_floor_may_only_be_tightened() -> None:
    base = floor(tests="required", commit_max_subject=72, trailers_required=["a"])
    assert standards.loosened(base, base) == []
    tighter = floor(
        tests="required",
        commit_max_subject=60,
        trailers_required=["a", "b"],
        lint="required",
    )
    assert standards.loosened(base, tighter) == []
    looser = floor(tests="optional", commit_max_subject=90, trailers_required=[])
    problems = standards.loosened(base, looser)
    assert len(problems) == 3
    assert all("loosened" in p for p in problems)
    dropped = standards.loosened(base, floor(tests="required", trailers_required=["a"]))
    assert dropped == ["floor key commit_max_subject was dropped (it was 72)"]


def git(root: Path, *args: str) -> None:
    subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True)


def test_floor_check_judges_the_candidate_against_the_base(camp: Path) -> None:
    git(camp, "init", "-q")
    git(camp, "add", "-A")
    git(camp, "commit", "-q", "-m", "base")
    assert floor_check(camp, "HEAD") == ([], [])
    edit(camp, FLOOR_FILE, 'dates = "iso-8601"', 'dates = "any"')
    problems, _ = floor_check(camp, "HEAD")
    assert any("floor key dates was loosened" in p for p in problems)
    edit(camp, FLOOR_FILE, 'dates = "any"', 'dates = "iso-8601"')
    edit(camp, FLOOR_FILE, "commit_max_subject = 72", "commit_max_subject = 64")
    assert floor_check(camp, "HEAD") == ([], [])


def test_floor_check_on_a_base_without_a_floor_notes_it(camp: Path) -> None:
    floor_text = (camp / FLOOR_FILE).read_text()
    (camp / FLOOR_FILE).unlink()
    git(camp, "init", "-q")
    git(camp, "add", "-A")
    git(camp, "commit", "-q", "-m", "before the floor")
    (camp / FLOOR_FILE).write_text(floor_text)
    problems, notes = floor_check(camp, "HEAD")
    assert problems == []
    assert "has no .agents/standards.floor.toml" in notes[0]


def test_the_cli_check_refuses_a_loosened_floor(camp: Path) -> None:
    git(camp, "init", "-q")
    git(camp, "add", "-A")
    git(camp, "commit", "-q", "-m", "base")
    edit(camp, FLOOR_FILE, 'lint = "required"', 'lint = "optional"')
    result = CliRunner().invoke(
        cli, ["config", "check", "--base", "HEAD", "--root", str(camp)]
    )
    assert result.exit_code == 1
    assert "floor key lint was loosened" in result.output


# ---------------------------------------------------------------- waivers

WAIVER = """[[waivers]]
gate = "types"
reason = "dev/ledger prototype notebooks are untyped until the dev proof order"
expires = {expires}
approved_by = "{by}"

"""


def waive(root: Path, expires: str = "2026-10-15", by: str = "owner") -> None:
    edit(root, FLOOR_FILE, "waivers = []\n", WAIVER.format(expires=expires, by=by))


def test_a_waiver_holds_until_its_day(camp: Path) -> None:
    waive(camp)
    config = load_config(camp, today=TODAY)
    assert [w.gate for w in config.floor.waivers] == ["types"]
    [entry] = config.explain("waivers[0].gate")
    assert entry.applies == "until 2026-10-15"
    assert load_config(camp, today=dt.date(2026, 10, 15)).floor.waivers


def test_a_quoted_date_is_read_as_a_date(camp: Path) -> None:
    waive(camp, expires='"2026-10-15"')
    [waiver] = load_config(camp, today=TODAY).floor.waivers
    assert waiver.expires == dt.date(2026, 10, 15)


def test_an_expired_waiver_fails(camp: Path) -> None:
    waive(camp)
    with pytest.raises(Bad, match="the waiver for types expired on 2026-10-15"):
        load_config(camp, today=dt.date(2026, 10, 16))


def test_a_waiver_the_owner_did_not_approve_fails(camp: Path) -> None:
    waive(camp, by="chief")
    refused(camp, "approved_by")


def test_a_waiver_for_an_unknown_gate_fails(camp: Path) -> None:
    waive(camp)
    edit(camp, FLOOR_FILE, 'gate = "types"', 'gate = "vibes"')
    refused(camp, "waiver for unknown gate 'vibes'")


def test_the_shipped_floor_has_no_waiver_that_could_expire(repo: Path) -> None:
    config = load_config(repo, today=TODAY)
    assert all(w.expires >= TODAY for w in config.floor.waivers)


# ---------------------------------------------------------------- JSON Schema draft-07


def shipped_tomls(root: Path) -> list[str]:
    config = (p.relative_to(root).as_posix() for p in (root / CONFIG_DIR).rglob("*"))
    return sorted([KNOBS_FILE, FLOOR_FILE, *(p for p in config if p.endswith(".toml"))])


def as_json(rel: Path) -> Any:
    # TOML dates reach JSON as ISO 8601 text, the way taplo and CI read them.
    data = tomllib.loads(rel.read_text(encoding="utf-8"))
    return json.loads(json.dumps(data, default=lambda d: d.isoformat()))


def test_the_committed_config_schemas_match_the_models(repo: Path) -> None:
    folder = repo / CONTRACTS_DIR
    expected = {
        f"{name}.schema.json": json.dumps(body, indent=2, sort_keys=True) + "\n"
        for name, body in config_json_schemas().items()
    }
    committed = {p.name: p.read_text("utf-8") for p in folder.glob("*.schema.json")}
    assert committed == expected, "run: uv run tac config schema --write"


@pytest.mark.parametrize("name", sorted(CONFIG_SCHEMAS))
def test_each_config_schema_is_draft_07_and_nothing_later(name: str) -> None:
    schema = config_json_schemas()[name]
    Draft7Validator.check_schema(schema)
    assert schema["$schema"] == DRAFT_07
    assert schema["additionalProperties"] is False
    assert later_keywords(schema) == []


def test_every_shipped_file_passes_its_json_schema(repo: Path) -> None:
    schemas = config_json_schemas()
    for rel in shipped_tomls(repo):
        name = schema_name(rel)
        assert name is not None, f"{rel} has no schema"
        errors = [
            f"{rel}: {'/'.join(map(str, e.absolute_path))}: {e.message}"
            for e in Draft7Validator(schemas[name]).iter_errors(as_json(repo / rel))
        ]
        assert errors == []


def test_every_config_schema_is_used_by_a_shipped_file(repo: Path) -> None:
    used = {schema_name(rel) for rel in shipped_tomls(repo)}
    assert used == set(CONFIG_SCHEMAS)


def test_the_json_schema_refuses_an_unknown_key_and_a_wrong_enum(repo: Path) -> None:
    knobs = as_json(repo / KNOBS_FILE)
    assert isinstance(knobs, dict)
    validator = Draft7Validator(config_json_schemas()["config"])
    assert list(validator.iter_errors({**knobs, "surprise": 1}))
    profile = {**knobs["profile"], "active": 3}
    assert list(validator.iter_errors({**knobs, "profile": profile}))


def test_later_keywords_reads_keywords_not_property_names() -> None:
    schema = {
        "$defs": {},
        "properties": {"prefixItems": {"type": "array", "prefixItems": []}},
        "enum": [{"$defs": 1}],
    }
    assert later_keywords(schema) == [
        "/: $defs",
        "/properties/prefixItems: prefixItems",
    ]


def test_config_schema_write_replaces_stale_files(tmp_path: Path) -> None:
    stale = tmp_path / CONTRACTS_DIR / "gone.schema.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["config", "schema", "--write", "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert not stale.exists()
    written = sorted(p.stem for p in stale.parent.glob("*.schema.json"))
    assert written == sorted(f"{n}.schema" for n in CONFIG_SCHEMAS)
    again = runner.invoke(cli, ["config", "schema", "--write", "--root", str(tmp_path)])
    assert again.exit_code == 0 and again.output == ""
    one = runner.invoke(cli, ["config", "schema", "role"])
    assert json.loads(one.output) == config_json_schemas()["role"]
    assert runner.invoke(cli, ["config", "schema"]).exit_code != 0
