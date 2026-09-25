"""The skill lint, and how `tac sync` links skills."""

from __future__ import annotations

from pathlib import Path

import pytest

from tac.sync import link_skills, skill_opt_in
from tac.work import Bad
from tests._syncproject import synced


def write_skill(root: Path, name: str, metadata: str = "") -> Path:
    folder = root / ".agents/skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    head = f"---\nname: {name}\ndescription: A skill.\n"
    if metadata:
        head += f"metadata:\n{metadata}"
    (folder / "SKILL.md").write_text(f"{head}---\n# {name}\n", encoding="utf-8")
    return folder


# ---------------------------------------------------------------- sync links


def test_an_opt_in_skill_is_never_linked_and_loses_a_stale_link(
    tmp_path: Path,
) -> None:
    root = synced(tmp_path)
    write_skill(root, "lazy", '  opt_in: "true"\n')
    write_skill(root, "plain", '  source: "here"\n')
    stale = root / ".claude/skills/lazy"
    stale.symlink_to("../../.agents/skills/lazy")
    changed = link_skills(root)
    assert "removed .claude/skills/lazy" in changed
    assert "linked .claude/skills/plain" in changed
    assert not stale.exists() and not stale.is_symlink()
    assert (root / ".claude/skills/plain/SKILL.md").is_file()
    assert link_skills(root) == [], "a second run writes nothing"


def test_opt_in_reads_the_frontmatter_only(tmp_path: Path) -> None:
    assert skill_opt_in(write_skill(tmp_path, "a", "  opt_in: true\n"))
    assert skill_opt_in(write_skill(tmp_path, "b", '  opt_in: "true"\n'))
    assert not skill_opt_in(write_skill(tmp_path, "c", '  opt_in: "false"\n'))
    assert not skill_opt_in(write_skill(tmp_path, "d"))
    body = tmp_path / ".agents/skills/e"
    body.mkdir(parents=True)
    (body / "SKILL.md").write_text(
        "---\nname: e\ndescription: x\n---\nmetadata:\n  opt_in: true\n",
        encoding="utf-8",
    )
    assert not skill_opt_in(body), "only the frontmatter counts, not the body"


def test_a_skill_folder_without_skill_md_is_refused(tmp_path: Path) -> None:
    root = synced(tmp_path)
    (root / ".agents/skills/empty").mkdir()
    with pytest.raises(Bad, match=r"no SKILL\.md"):
        link_skills(root)


def test_a_folder_starting_with_an_underscore_is_not_a_skill(tmp_path: Path) -> None:
    root = synced(tmp_path)
    (root / ".agents/skills/_index").mkdir()
    link_skills(root)
    assert not (root / ".claude/skills/_index").exists()
