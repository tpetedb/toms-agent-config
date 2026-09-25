"""`tac sync` renders what the goldens say, and `tac check` catches every drift.

The goldens under tests/fixtures/sync/golden/ are the renders of this
repository's own inputs, one flat file per output named `<path with / as
__>.golden`, so no harness mistakes a golden for its own configuration. After a
deliberate change to a template, the adapters or the inputs, rewrite them with
`TAC_UPDATE_GOLDEN=1 uv run --frozen pytest -q tests/test_sync.py` and review
the diff like any other change.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from tac import adapters
from tac import sync as sync_mod
from tac.cli import cli
from tac.config import load_config
from tac.contracts import MODEL_FACING_DIR, strict_subset_problems
from tac.doctor import Status, check_generated_lock
from tac.draft07 import DRAFT_07
from tac.sync import (
    LOCK_FILE,
    check_staged,
    check_tree,
    link_skills,
    read_template,
    render,
    sync,
)
from tac.work import Bad
from tests._gitrepo import GIT
from tests._syncproject import REPO, copy_project, replace_in, synced

GOLDEN = REPO / "tests" / "fixtures" / "sync" / "golden"


def golden_name(rel: str) -> str:
    return rel.replace("/", "__") + ".golden"


def goldens() -> dict[str, str]:
    return {
        p.name.removesuffix(".golden").replace("__", "/"): p.read_text(encoding="utf-8")
        for p in sorted(GOLDEN.glob("*.golden"))
    }


# ---------------------------------------------------------------- renders


def test_renders_match_golden_files(tmp_path: Path) -> None:
    root = synced(tmp_path)
    rendered = render(root).outputs
    if os.environ.get("TAC_UPDATE_GOLDEN") == "1":
        GOLDEN.mkdir(parents=True, exist_ok=True)
        for stale in GOLDEN.glob("*.golden"):
            stale.unlink()
        for rel, text in rendered.items():
            (GOLDEN / golden_name(rel)).write_text(text, encoding="utf-8")
    assert set(rendered) == set(goldens()), "the set of outputs changed"
    for rel, text in goldens().items():
        assert (root / rel).read_text(encoding="utf-8") == text, rel


def test_the_repository_is_in_sync() -> None:
    assert check_tree(REPO) == []


def test_every_output_is_on_the_allowlist_and_named_by_its_template(
    tmp_path: Path,
) -> None:
    root = synced(tmp_path)
    result = render(root)
    for rel, source in result.sources.items():
        template = read_template(root, source)
        assert rel == template.output.format(role=Path(rel).stem)


def test_the_lock_names_inputs_templates_policy_floor_and_outputs(
    tmp_path: Path,
) -> None:
    root = synced(tmp_path)
    lock = sync_mod.read_lock(root)
    assert lock is not None
    assert ".agents/config.toml" in lock["inputs"]
    assert ".agents/context/brief.md" in lock["inputs"]
    assert set(lock["policy"]) == {".agents/config/policy.toml"}
    assert set(lock["floor"]) == {".agents/standards.floor.toml"}
    assert "templates/adapters/claude/CLAUDE.md.j2" in lock["templates"]
    assert set(lock["outputs"]) == set(render(root).outputs)


# ---------------------------------------------------------------- container profiles


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def test_sync_refuses_yolo_on_the_host_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = synced(tmp_path)
    replace_in(root / ".agents/config.toml", 'active = "standard"', 'active = "yolo"')
    before = _files(root)
    # The host, whatever the machine running the test is: no marker is found.
    monkeypatch.setattr("tac.config.container_evidence", lambda *_: None)
    with pytest.raises(Bad, match=re.escape("profiles/yolo.toml: isolation")):
        sync(root)
    assert _files(root) == before
    assert any("profiles/yolo.toml: isolation" in p for p in check_tree(root))
    done = CliRunner().invoke(cli, ["sync", "--root", str(root)])
    assert done.exit_code != 0 and "/.dockerenv" in done.output
    assert _files(root) == before


def test_yolo_renders_bypass_with_the_sandbox_off_inside_a_container(
    tmp_path: Path,
) -> None:
    # The container is the isolation there, which is the shape yolo asks for.
    root = copy_project(tmp_path)
    replace_in(root / ".agents/config.toml", 'active = "standard"', 'active = "yolo"')
    config = load_config(root, container="/.dockerenv (Docker)")
    settings = json.loads(render(root, config).outputs[".claude/settings.json"])
    assert settings["permissions"]["defaultMode"] == "bypassPermissions"
    assert "disableBypassPermissionsMode" not in settings["permissions"]
    assert settings["sandbox"]["enabled"] is False


# ---------------------------------------------------------------- drift


def test_a_hand_edit_is_caught(tmp_path: Path) -> None:
    root = synced(tmp_path)
    settings = root / ".claude/settings.json"
    data = json.loads(settings.read_text())
    data["sandbox"]["enabled"] = False
    settings.write_text(json.dumps(data, indent=2) + "\n")
    problems = check_tree(root)
    assert any(
        p.startswith(".claude/settings.json: edited by hand") for p in problems
    ), problems


def test_a_template_edit_without_sync_is_caught(tmp_path: Path) -> None:
    root = synced(tmp_path)
    template = root / "templates/adapters/claude/CLAUDE.md.j2"
    template.write_text(template.read_text() + "\nOne more line.\n")
    problems = check_tree(root)
    assert (
        f"{LOCK_FILE}: stale, templates templates/adapters/claude/CLAUDE.md.j2 "
        "changed since the last sync"
    ) in problems
    assert "CLAUDE.md: out of date with its inputs; run tac sync" in problems
    sync(root)
    assert check_tree(root) == []


def test_an_input_edit_without_sync_is_caught(tmp_path: Path) -> None:
    root = synced(tmp_path)
    replace_in(
        root / ".agents/config/models.toml",
        'openai = { model = "gpt-6-sol", effort = "high" }             # the Codex',
        'openai = { model = "gpt-6-sol", effort = "medium" }           # the Codex',
    )
    problems = check_tree(root)
    assert any("stale, inputs .agents/config/models.toml" in p for p in problems)
    assert ".codex/agents/builder.toml: out of date with its inputs; run tac sync" in (
        problems
    )


def test_a_policy_or_floor_edit_makes_the_lock_stale(tmp_path: Path) -> None:
    root = synced(tmp_path)
    policy = root / ".agents/config/policy.toml"
    policy.write_text(policy.read_text() + "\n")
    floor = root / ".agents/standards.floor.toml"
    floor.write_text(floor.read_text() + "\n")
    problems = check_tree(root)
    assert (
        f"{LOCK_FILE}: stale, policy .agents/config/policy.toml changed since "
        "the last sync"
    ) in problems
    assert any("stale, floor .agents/standards.floor.toml" in p for p in problems)


def test_a_missing_lock_and_a_missing_output_are_caught(tmp_path: Path) -> None:
    root = synced(tmp_path)
    (root / "CLAUDE.md").unlink()
    (root / LOCK_FILE).unlink()
    problems = check_tree(root)
    assert f"{LOCK_FILE}: missing; run tac sync" in problems
    assert "CLAUDE.md: missing; run tac sync" in problems


def test_an_unexpected_output_is_caught(tmp_path: Path) -> None:
    root = synced(tmp_path)
    (root / ".codex/agents/stray.toml").write_text('name = "stray"\n')
    assert ".codex/agents/stray.toml: unexpected; no template renders it" in (
        check_tree(root)
    )


def test_an_unexpected_file_anywhere_under_codex_is_caught(tmp_path: Path) -> None:
    # Codex runs the hooks it finds under .codex/ in a trusted project, so a
    # planted file and an emptied hooks.json are both red.
    root = synced(tmp_path)
    (root / ".codex/hooks.json").write_text("{}\n")
    (root / ".codex/rules").mkdir()
    (root / ".codex/rules/extra.rules").write_text("allow\n")
    problems = check_tree(root)
    assert any(p.startswith(".codex/hooks.json: edited by hand") for p in problems)
    assert ".codex/rules/extra.rules: unexpected; no template renders it" in problems


def test_a_hand_edit_with_its_lock_line_rewritten_is_named(tmp_path: Path) -> None:
    root = synced(tmp_path)
    claude = root / "CLAUDE.md"
    edited = claude.read_text() + "\nA line nobody rendered.\n"
    claude.write_text(edited)
    lock = root / LOCK_FILE
    recorded = sync_mod.read_lock(root)
    assert recorded is not None
    old = recorded["outputs"]["CLAUDE.md"]
    new = sync_mod._digest(edited.encode("utf-8"))  # pyright: ignore[reportPrivateUsage]
    lock.write_text(lock.read_text().replace(old, new))
    problems = check_tree(root)
    assert (
        f"{LOCK_FILE}: the outputs line for CLAUDE.md is not the hash a render "
        "gives; the lock was edited by hand"
    ) in problems
    assert any(
        p.startswith("CLAUDE.md: edited by hand, with its lock line rewritten")
        for p in problems
    ), problems
    assert not any("out of date" in p for p in problems)


def test_a_forged_lock_line_alone_is_named(tmp_path: Path) -> None:
    root = synced(tmp_path)
    lock = root / LOCK_FILE
    recorded = sync_mod.read_lock(root)
    assert recorded is not None
    old = recorded["outputs"]["AGENTS.md"]
    lock.write_text(lock.read_text().replace(old, "0" * 64))
    assert check_tree(root) == [
        f"{LOCK_FILE}: the outputs line for AGENTS.md is not the hash a render "
        "gives; the lock was edited by hand"
    ]


def test_a_hand_edited_lock_is_caught(tmp_path: Path) -> None:
    root = synced(tmp_path)
    lock = root / LOCK_FILE
    lock.write_text(
        lock.read_text().replace("schema_version = 1", "schema_version = 1\n")
    )
    assert check_tree(root) == [f"{LOCK_FILE}: not what tac sync writes; run tac sync"]


def test_a_role_that_loses_its_seat_loses_its_file(tmp_path: Path) -> None:
    root = synced(tmp_path)
    # The chief sits on Anthropic only, so Codex has no chief to render.
    assert not (root / ".codex/agents/chief.toml").exists()
    enforced = 'enforced = ["claude", "codex"]'
    replace_in(root / ".agents/config.toml", enforced, 'enforced = ["claude"]')
    replace_in(
        root / ".agents/config.toml",
        'instructions_only = ["copilot"',
        'instructions_only = ["codex", "copilot"',
    )
    changed = sync(root)
    assert "removed .codex/config.toml" in changed
    assert "removed .codex/agents/builder.toml" in changed
    assert check_tree(root) == []


# ---------------------------------------------------------------- templates


def test_a_template_naming_a_path_outside_the_allowlist_fails(tmp_path: Path) -> None:
    root = synced(tmp_path)
    (root / "templates/adapters/claude/evil.md.j2").write_text(
        "{# output: .git/hooks/pre-commit #}\nboom\n"
    )
    with pytest.raises(Bad, match="not on the allowlist"):
        render(root)
    assert any("not on the allowlist" in p for p in check_tree(root))


def test_a_template_must_name_its_output_on_its_first_line(tmp_path: Path) -> None:
    root = synced(tmp_path)
    (root / "templates/adapters/common/extra.md.j2").write_text("no header\n")
    with pytest.raises(Bad, match="first line must be"):
        render(root)


def test_a_structured_output_needs_a_data_template(tmp_path: Path) -> None:
    root = synced(tmp_path)
    source = root / "templates/adapters/codex/config.toml.data.j2"
    source.rename(root / "templates/adapters/codex/config.toml.j2")
    with pytest.raises(Bad, match="data template"):
        render(root)


def test_templates_render_sandboxed(tmp_path: Path) -> None:
    root = synced(tmp_path)
    replace_in(
        root / "templates/adapters/claude/CLAUDE.md.j2",
        "@AGENTS.md",
        "{{ note.__class__.__mro__ }}",
    )
    with pytest.raises(Bad, match="SecurityError"):
        render(root)


def test_templates_render_with_strict_undefined(tmp_path: Path) -> None:
    root = synced(tmp_path)
    replace_in(
        root / "templates/adapters/common/AGENTS.md.j2",
        "{{ common.brief }}",
        "{{ common.no_such_value }}",
    )
    with pytest.raises(Bad, match="UndefinedError"):
        render(root)


def test_a_value_cannot_break_the_toml_it_lands_in(tmp_path: Path) -> None:
    root = synced(tmp_path)
    replace_in(
        root / ".agents/config/roles/builder.toml",
        'description = "Builds one work order',
        'description = "Quote \\" and \\"\\"\\" and a\\nnew line. Builds one '
        "work order",
    )
    sync(root)
    data = tomllib.loads((root / ".codex/agents/builder.toml").read_text())
    assert data["description"].startswith('Quote " and """ and a\nnew line.')


# ---------------------------------------------------------------- --staged


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        [*GIT, "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return done.stdout


def test_check_staged_judges_the_index(tmp_path: Path) -> None:
    root = synced(tmp_path)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    assert check_staged(root) == []
    claude = root / "CLAUDE.md"
    claude.write_text(claude.read_text() + "hand edit\n")
    # Unstaged, the edit is in the working tree only.
    assert check_staged(root) == []
    assert any(p.startswith("CLAUDE.md: edited by hand") for p in check_tree(root))
    _git(root, "add", "CLAUDE.md")
    assert any(p.startswith("CLAUDE.md: edited by hand") for p in check_staged(root))


def test_the_cli_exits_one_on_drift_and_zero_when_clean(tmp_path: Path) -> None:
    root = synced(tmp_path)
    runner = CliRunner()
    clean = runner.invoke(cli, ["check", "--root", str(root)])
    assert clean.exit_code == 0, clean.output
    (root / "AGENTS.md").write_text("replaced\n")
    dirty = runner.invoke(cli, ["check", "--root", str(root)])
    assert dirty.exit_code == 1
    assert "AGENTS.md: edited by hand" in dirty.output
    again = runner.invoke(cli, ["sync", "--root", str(root)])
    assert again.exit_code == 0 and "wrote AGENTS.md" in again.output


def test_doctor_reports_drift(tmp_path: Path) -> None:
    root = synced(tmp_path)
    assert check_generated_lock(root)[0] is Status.PASS
    (root / "CLAUDE.md").write_text("replaced\n")
    status, detail = check_generated_lock(root)
    assert status is Status.FAIL and "CLAUDE.md" in detail


# ---------------------------------------------------------------- budget


def test_the_agents_md_chain_stays_under_its_budget(tmp_path: Path) -> None:
    root = synced(tmp_path)
    brief = root / ".agents/context/brief.md"
    brief.write_text(brief.read_text() + ("x" * 80 + "\n") * 400)
    sync(root)
    problems = check_tree(root)
    assert any(p.startswith("AGENTS.md:") and "byte budget" in p for p in problems)


# ---------------------------------------------------------------- skill links


def test_skills_are_linked_relative_and_never_to_a_linked_folder(
    tmp_path: Path,
) -> None:
    root = synced(tmp_path)
    link = root / ".claude/skills/work-order"
    assert link.is_symlink()
    assert os.readlink(link) == "../../.agents/skills/work-order"
    assert (link / "SKILL.md").is_file()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / ".agents/skills/sneaky").symlink_to(elsewhere)
    with pytest.raises(Bad, match="may not be a link"):
        link_skills(root)


def test_a_skill_falls_back_to_a_copy_without_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = copy_project(tmp_path)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("symlinks are off here")

    monkeypatch.setattr(Path, "symlink_to", refuse)
    changed = sync(root)
    assert "copied .claude/skills/work-order" in changed
    copy = root / ".claude/skills/work-order"
    assert not copy.is_symlink() and (copy / "SKILL.md").is_file()


def test_a_removed_skill_loses_its_link(tmp_path: Path) -> None:
    root = synced(tmp_path)
    (root / ".claude/skills/gone").symlink_to("../../.agents/skills/gone")
    assert "removed .claude/skills/gone" in link_skills(root)


# ---------------------------------------------------------------- contracts (C4)

STRICT = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "risk": {"type": ["string", "null"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "risk", "items"],
    "additionalProperties": False,
}


def test_a_strict_schema_passes_the_subset_lint() -> None:
    assert strict_subset_problems(STRICT) == []


def test_the_subset_lint_names_each_departure() -> None:
    loose = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"allOf": [{"type": "string"}]},
            "c": {"type": "object", "properties": {"d": {"type": "string"}}},
        },
        "required": ["a", "b"],
    }
    problems = strict_subset_problems(loose)
    assert "/: property c must be required" in problems
    assert "/: additionalProperties must be false" in problems
    assert "/properties/b: allOf is outside the strict subset" in problems
    assert "/properties/c: property d must be required" in problems
    assert strict_subset_problems({"type": "array"}) == [
        "/: the root must be an object schema"
    ]


def test_the_subset_lint_limits_depth() -> None:
    schema: dict = {"type": "string"}
    for _ in range(11):
        schema = {
            "type": "object",
            "properties": {"x": schema},
            "required": ["x"],
            "additionalProperties": False,
        }
    assert any("deeper than 10" in p for p in strict_subset_problems(schema))


def test_only_model_facing_contracts_are_held_to_the_subset(tmp_path: Path) -> None:
    root = synced(tmp_path)
    # A config schema keeps optional keys and defaults; the lint never reads it.
    config = root / "contracts/config/example.schema.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"type": "object", "properties": {"a": {}}}))
    assert check_tree(root) == []
    handoff = root / MODEL_FACING_DIR / "review.schema.json"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(json.dumps({"type": "object", "properties": {"a": {}}}))
    problems = check_tree(root)
    assert (
        f"{MODEL_FACING_DIR}/review.schema.json#/: property a must be required"
        in problems
    )
    handoff.write_text(json.dumps({"$schema": DRAFT_07, **STRICT}))
    assert check_tree(root) == []


# ---------------------------------------------------------------- hook wiring


def _guard_constant(name: str) -> object:
    """A constant of the stdlib guard, read without importing it."""
    tree = ast.parse((REPO / "hooks" / "run.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "frozenset"
            ):
                return frozenset(ast.literal_eval(value.args[0]))
            return ast.literal_eval(value)
    raise AssertionError(f"hooks/run.py has no {name}")


def test_the_adapters_and_the_guard_agree_on_events_and_environment() -> None:
    # The guard cannot import tac, so it keeps its own copy of both tables.
    assert _guard_constant("EVENTS") == adapters.GUARD_EVENTS
    assert _guard_constant("PASS_ENV") == adapters.HOOK_ENV
    # A hook that cannot find the guard fails the way the guard fails.
    assert _guard_constant("DENY_CLASS") == adapters.DENY_CLASS


def test_codex_hooks_use_the_tool_names_codex_reports(tmp_path: Path) -> None:
    root = synced(tmp_path)
    hooks = json.loads((root / ".codex/hooks.json").read_text())["hooks"]
    assert set(hooks) == {"PreToolUse", "Stop"}
    (pre,) = hooks["PreToolUse"]
    # apply_patch for every file edit, spawn_agent for a native subagent:
    # https://developers.openai.com/codex/hooks
    assert pre["matcher"].split("|") == ["apply_patch", "spawn_agent"]
    for groups in hooks.values():
        for group in groups:
            (one,) = group["hooks"]
            assert (
                "/usr/bin/git rev-parse --show-toplevel)/.agents/hooks/run.py"
                in (one["command"])
            )
            assert "--client codex" in one["command"]


def test_a_record_check_renders_no_hook(tmp_path: Path) -> None:
    root = synced(tmp_path)
    claude = json.loads((root / ".claude/settings.json").read_text())["hooks"]
    codex = json.loads((root / ".codex/hooks.json").read_text())["hooks"]
    assert "SubagentStart" not in claude
    assert "SubagentStart" not in codex


def test_a_check_on_an_event_the_guard_does_not_answer_is_refused(
    tmp_path: Path,
) -> None:
    root = copy_project(tmp_path)
    replace_in(
        root / ".agents/config/hooks.toml",
        'event = "SubagentStart"                   # PreToolUse | PostToolUse | '
        "SessionStart | SubagentStart | Stop\n"
        'kind = "record"',
        'event = "SubagentStart"\nkind = "reminder"',
    )
    with pytest.raises(Bad, match=r"spawn-record is wired on claude for SubagentStart"):
        render(root)


def test_matchers_merge_into_one_group_and_a_regex_keeps_its_own() -> None:
    assert adapters._merge(["Edit|Write", "Read", "Write"]) == ["Edit|Write|Read"]
    assert adapters._merge(["Edit", "mcp__.*"]) == ["Edit", "mcp__.*"]
    assert adapters._merge(["Edit", "*"]) == ["*"]


def test_a_guard_script_that_leaves_plain_words_is_refused(tmp_path: Path) -> None:
    root = copy_project(tmp_path)
    replace_in(
        root / ".agents/config/hooks.toml",
        'script = ".agents/hooks/run.py"',
        'script = ".agents/hooks/run.py$(id)"',
    )
    with pytest.raises(Bad, match=r"guard\.script"):
        render(root)


def test_the_lock_hashes_the_git_hooks_with_the_guard(tmp_path: Path) -> None:
    root = synced(tmp_path)
    lock = sync_mod.read_lock(root)
    assert lock is not None
    assert "hooks/git/prek.toml" in lock["hooks"]
    prek = root / "hooks/git/prek.toml"
    prek.write_text(prek.read_text() + "\n# an edit\n")
    assert (
        f"{LOCK_FILE}: stale, hooks hooks/git/prek.toml changed since the last sync"
        in check_tree(root)
    )


# ---------------------------------------------------------------- git hooks


def test_the_git_hooks_must_run_what_hooks_toml_names_in_order(
    tmp_path: Path,
) -> None:
    root = synced(tmp_path)
    replace_in(
        root / ".agents/config/hooks.toml",
        'pre_commit = ["ruff-format", "ruff-check",',
        'pre_commit = ["ruff-check", "ruff-format",',
    )
    sync(root, links=False)
    problems = check_tree(root)
    assert any(
        p.startswith("hooks/git/prek.toml: pre-commit runs ['ruff-format'")
        and "names ['ruff-check', 'ruff-format'" in p
        for p in problems
    ), problems


def test_a_git_hook_that_fetches_or_is_not_a_system_command_is_refused(
    tmp_path: Path,
) -> None:
    root = synced(tmp_path)
    prek = root / "hooks/git/prek.toml"
    text = prek.read_text()
    text = text.replace('repo = "local"', 'repo = "https://example.com/hooks"', 1)
    prek.write_text(text)
    sync(root, links=False)
    assert "hooks/git/prek.toml: every repo is local; nothing is fetched" in (
        check_tree(root)
    )
    prek.write_text(
        text.replace('repo = "https://example.com/hooks"', 'repo = "local"').replace(
            'language = "system"', 'language = "python"', 1
        )
    )
    sync(root, links=False)
    assert "hooks/git/prek.toml: ruff-format is not language = system" in (
        check_tree(root)
    )


def test_missing_git_hooks_are_named(tmp_path: Path) -> None:
    root = synced(tmp_path)
    (root / "hooks/git/prek.toml").unlink()
    sync(root, links=False)
    assert any(p.startswith("hooks/git/prek.toml: missing") for p in check_tree(root))
