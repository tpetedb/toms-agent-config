"""`tac sync` renders what the goldens say, and `tac check` catches every drift.

The goldens under tests/fixtures/sync/golden/ are the renders of this
repository's own inputs, one flat file per output named `<path with / as
__>.golden`, so no harness mistakes a golden for its own configuration. After a
deliberate change to a template, the adapters or the inputs, rewrite them with
`TAC_UPDATE_GOLDEN=1 uv run --frozen pytest -q tests/test_sync.py` and review
the diff like any other change.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from tac import sync as sync_mod
from tac.cli import cli
from tac.contracts import MODEL_FACING_DIR, strict_subset_problems
from tac.doctor import Status, check_generated_lock
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
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"type": "object", "properties": {"a": {}}}))
    assert check_tree(root) == []
    handoff = root / MODEL_FACING_DIR / "review.schema.json"
    handoff.parent.mkdir(parents=True)
    handoff.write_text(json.dumps({"type": "object", "properties": {"a": {}}}))
    problems = check_tree(root)
    assert (
        f"{MODEL_FACING_DIR}/review.schema.json#/: property a must be required"
        in problems
    )
    handoff.write_text(json.dumps(STRICT))
    assert check_tree(root) == []
