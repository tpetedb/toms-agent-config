"""The skill lint (`just skills-lint`), and how `tac sync` links skills.

Every folder under `.agents/skills/` whose name does not start with `_` is held
to the portable subset of the Agent Skills specification
(https://agentskills.io/specification, read 2026-09-25): `name` equals its
folder, at most 64 characters of `a-z`, `0-9` and single hyphens; `description`
non-empty and at most 1,024 characters; `compatibility` at most 500; only the
specification's top-level fields; `metadata` a map of strings to strings. The
body stays under 500 lines, as the specification recommends. On top of that,
this repository asks provenance in `metadata` (source, author, licence,
changes), a `--help` that exits 0 from every bundled script, a LICENSE file
where `license` names one, no em dash and no pictograph.

The listing budget. Both clients list every skill's name and description in
every session, inside a budget, and cut descriptions when the sum is over it:

- Claude Code (https://code.claude.com/docs/en/skills, read 2026-09-25): "The
  budget scales at 1% of the model's context window", and each entry's
  description and `when_to_use` together are cut at 1,536 characters. The
  smallest window a TAC seat runs is 200,000 tokens, so 2,000 tokens.
- Codex (https://developers.openai.com/codex/skills, which redirects to
  https://learn.chatgpt.com/docs/build-skills, read 2026-09-25): "at most 2% of
  the model's context window, or 8,000 characters when the context window is
  unknown". The fallback, 8,000 characters, is the smaller Codex figure.

At four characters per token, Claude's 2,000 tokens are 8,000 characters, the
same as Codex's fallback, so the budget is 8,000 characters. The sum over every
skill of its name and description, in characters, must stay under it, so no
client ever cuts one of ours.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
import unicodedata
from pathlib import Path
from typing import Any

import pytest
import yaml

from tac.sync import link_skills, skill_opt_in
from tac.work import Bad
from tests._syncproject import REPO, synced

SKILLS = REPO / ".agents" / "skills"
SPEC_FIELDS = frozenset(
    {"name", "description", "license", "compatibility", "allowed-tools", "metadata"}
)
PROVENANCE = ("source", "author", "licence", "changes")
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
MAX_BODY_LINES = 500
CLAUDE_SMALLEST_WINDOW_TOKENS = 200_000
CLAUDE_LISTING_SHARE = 0.01
CODEX_FALLBACK_CHARS = 8_000
CHARS_PER_TOKEN = 4
LISTING_BUDGET_CHARS = min(
    int(CLAUDE_SMALLEST_WINDOW_TOKENS * CLAUDE_LISTING_SHARE) * CHARS_PER_TOKEN,
    CODEX_FALLBACK_CHARS,
)
CLAUDE_ENTRY_CAP = 1536
EM_DASH = "\u2014"
# The skills section 12 of docs/DESIGN.md ships.
SHIPPED = frozenset(
    {
        "adr",
        "changelog",
        "semver",
        "justfile",
        "mermaid-diagrams",
        "readme-quickstart",
        "verification-before-completion",
        "webapp-testing",
        "work-order",
        "research-first",
        "house-style",
        "hooks-authoring",
        "gitlab-ci",
        "data-platform",
        "shared-memory",
        "ponytail",
        "owner-report",
    }
)
# Skills that belong to one product stay in that product.
PRODUCT_ONLY = (
    "obsidian-notes",
    "camp-progress",
    "council",
    "develop-camp",
    "install-camp",
    "duckdb-sql",
    "python-data",
)
REVIEW_STAGES = frozenset({"review", "signoff"})
REVIEW_ROLES = frozenset({"reviewer", "manager"})


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """The YAML between the opening `---` and the next `---` line, and the body."""
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md does not open with a --- frontmatter line")
    head, found, body = text[4:].partition("\n---\n")
    if not found:
        raise ValueError("the frontmatter is never closed with a --- line")
    data = yaml.safe_load(head)
    if not isinstance(data, dict):
        raise ValueError("the frontmatter is not a mapping")
    return data, body


def pictographs(text: str) -> list[str]:
    """Emoji and other pictographs: the Symbol, Other characters of the emoji
    blocks, and the emoji variation selector."""
    found = []
    for ch in text:
        code = ord(ch)
        if code == 0xFE0F or (
            unicodedata.category(ch) == "So"
            and (0x2600 <= code <= 0x27BF or 0x1F000 <= code <= 0x1FAFF)
        ):
            found.append(f"U+{code:04X}")
    return found


def lint_skill(folder: Path) -> list[str]:
    """Every way one skill folder breaks the lint; empty is clean."""
    where = f"{folder.name}"
    if folder.is_symlink():
        return [f"{where}: a skill folder may not be a link"]
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        return [f"{where}: no SKILL.md"]
    try:
        data, body = split_frontmatter(skill_md.read_text(encoding="utf-8"))
    except (ValueError, yaml.YAMLError) as e:
        return [f"{where}: frontmatter: {e}"]
    problems = []
    name = data.get("name")
    if name != folder.name:
        problems.append(f"{where}: name {name!r} is not the folder name")
    if not isinstance(name, str) or len(name) > MAX_NAME or not NAME.match(name):
        problems.append(f"{where}: name must be 1 to 64 of a-z, 0-9, single hyphens")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append(f"{where}: description is empty")
    elif len(description) > MAX_DESCRIPTION:
        problems.append(
            f"{where}: description is {len(description)} characters, "
            f"over {MAX_DESCRIPTION}"
        )
    compatibility = data.get("compatibility")
    if compatibility is not None and (
        not isinstance(compatibility, str) or len(compatibility) > MAX_COMPATIBILITY
    ):
        problems.append(f"{where}: compatibility over {MAX_COMPATIBILITY} characters")
    extra = sorted(set(data) - SPEC_FIELDS)
    if extra:
        problems.append(f"{where}: fields outside the specification: {extra}")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        problems.append(f"{where}: no metadata; provenance goes there")
    else:
        bad = sorted(
            str(k)
            for k, v in metadata.items()
            if not isinstance(k, str) or not isinstance(v, str)
        )
        if bad:
            problems.append(f"{where}: metadata values must be strings: {bad}")
        for key in PROVENANCE:
            if not str(metadata.get(key, "")).strip():
                problems.append(f"{where}: metadata.{key} is missing")
    lines = body.count("\n") + 1
    if lines >= MAX_BODY_LINES:
        problems.append(f"{where}: body is {lines} lines, {MAX_BODY_LINES} or more")
    licence = data.get("license")
    if (
        isinstance(licence, str)
        and "LICENSE" in licence
        and not (folder / "LICENSE").is_file()
    ):
        problems.append(f"{where}: license names LICENSE, which is not there")
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(folder.parent).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if EM_DASH in text:
            problems.append(f"{rel}: an em dash")
        found = pictographs(text)
        if found:
            problems.append(f"{rel}: pictographs {sorted(set(found))}")
    return problems


def skill_folders() -> list[Path]:
    return sorted(p for p in SKILLS.iterdir() if not p.name.startswith("_"))


def frontmatter_of(name: str) -> dict[str, Any]:
    return split_frontmatter((SKILLS / name / "SKILL.md").read_text())[0]


def opt_in_names() -> set[str]:
    return {
        p.name
        for p in skill_folders()
        if p.is_dir()
        and frontmatter_of(p.name).get("metadata", {}).get("opt_in") in (True, "true")
    }


def bundled_scripts() -> list[Path]:
    return sorted(
        p
        for p in SKILLS.glob("*/scripts/*")
        if p.is_file() and p.suffix in (".py", ".sh")
    )


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


# ---------------------------------------------------------------- the lint


def test_the_shipped_skills_are_exactly_section_12s() -> None:
    assert {p.name for p in skill_folders()} == SHIPPED


@pytest.mark.parametrize("name", sorted(SHIPPED))
def test_every_skill_passes_the_portable_subset(name: str) -> None:
    assert lint_skill(SKILLS / name) == []


def test_the_descriptions_fit_the_smaller_listing_budget() -> None:
    assert LISTING_BUDGET_CHARS == 8_000, "see the module docstring"
    total = 0
    for folder in skill_folders():
        data = frontmatter_of(folder.name)
        entry = str(data["description"]) + str(data.get("when_to_use", ""))
        assert len(entry) <= CLAUDE_ENTRY_CAP, f"{folder.name}: Claude would cut it"
        total += len(folder.name) + len(str(data["description"]))
    tokens = total / CHARS_PER_TOKEN
    assert total < LISTING_BUDGET_CHARS, (
        f"{total} characters ({tokens:.0f} tokens) of names and descriptions, "
        f"over the {LISTING_BUDGET_CHARS} character listing budget; shorten some"
    )


@pytest.mark.parametrize("script", bundled_scripts(), ids=lambda p: p.name)
def test_every_bundled_script_answers_help(script: Path) -> None:
    runner = [sys.executable] if script.suffix == ".py" else ["bash"]
    done = subprocess.run(
        [*runner, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip(), "--help printed nothing"


def test_the_bundled_scripts_are_the_expected_ones() -> None:
    names = {p.relative_to(SKILLS).as_posix() for p in bundled_scripts()}
    assert {
        "owner-report/scripts/repo_tree.py",
        "owner-report/scripts/render_mermaid.sh",
        "webapp-testing/scripts/with_server.py",
    } <= names


def test_a_vendored_skill_carries_its_notice_in_third_party() -> None:
    third = (REPO / "THIRD_PARTY.md").read_text(encoding="utf-8")
    for folder in skill_folders():
        licence = frontmatter_of(folder.name)["metadata"]["licence"]
        if "the owner's own work" in licence:
            continue
        assert (folder / "LICENSE").is_file(), f"{folder.name}: no LICENSE"
        assert f"`.agents/skills/{folder.name}/`" in third, folder.name


def test_ponytail_is_the_one_opt_in_skill_and_is_for_builders() -> None:
    assert opt_in_names() == {"ponytail"}
    metadata = frontmatter_of("ponytail")["metadata"]
    assert metadata["roles"] == "builder"
    assert "e3ba2aa6f1e6f0bc4d69eb09c9f0d0a93af56156" in metadata["source"]


def test_no_opt_in_skill_is_given_to_a_team_or_a_review() -> None:
    opt_in = opt_in_names()
    teams = tomllib.loads((REPO / ".agents/config/teams.toml").read_text())
    for team, table in teams.get("teams", {}).items():
        given = opt_in & set(table.get("skills", []))
        assert not given, f"teams.{team}.skills names opt-in {sorted(given)}"
    for path in sorted((REPO / ".agents/config/pipelines").glob("*.toml")):
        pipeline = tomllib.loads(path.read_text())
        for stage in pipeline.get("stages", []):
            reviewing = (
                stage.get("id") in REVIEW_STAGES
                or stage.get("role") in REVIEW_ROLES
                or stage.get("provider") == "other"
            )
            given = opt_in & set(stage.get("skills", []))
            assert not (reviewing and given), (
                f"{path.name} stage {stage.get('id')} gives a reviewer {sorted(given)}"
            )


def test_product_only_and_caveman_skills_are_absent() -> None:
    present = {p.name for p in SKILLS.iterdir()}
    assert not present & set(PRODUCT_ONLY)
    assert not [n for n in present if n.startswith("caveman")]
    for path in SKILLS.rglob("*"):
        if path.is_file() and path.suffix in (".md", ".py", ".sh", ".toml"):
            assert "caveman" not in path.read_text(encoding="utf-8").lower(), path


# ---------------------------------------------------------------- the lint's teeth


def skill_at(tmp_path: Path, name: str, head: str, body: str = "# x\n") -> Path:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "SKILL.md").write_text(f"---\n{head}---\n{body}", encoding="utf-8")
    return folder


GOOD = (
    "description: Does one thing.\n"
    "metadata:\n  source: here\n  author: me\n  licence: MIT\n  changes: none\n"
)


def test_a_clean_skill_passes(tmp_path: Path) -> None:
    assert lint_skill(skill_at(tmp_path, "good", "name: good\n" + GOOD)) == []


@pytest.mark.parametrize(
    ("folder", "head", "body", "expected"),
    [
        ("a", "name: b\n" + GOOD, "x\n", "not the folder name"),
        ("Bad-Name", "name: Bad-Name\n" + GOOD, "x\n", "a-z, 0-9"),
        ("a--b", "name: a--b\n" + GOOD, "x\n", "a-z, 0-9"),
        (
            "long",
            "name: long\n" + GOOD.replace("Does one thing.", "x" * 1025),
            "x\n",
            "over 1024",
        ),
        ("extra", "name: extra\nargument-hint: x\n" + GOOD, "x\n", "outside"),
        ("bare", "name: bare\ndescription: y\n", "x\n", "no metadata"),
        (
            "partial",
            "name: partial\ndescription: y\nmetadata:\n  source: s\n",
            "x\n",
            "metadata.author",
        ),
        (
            "typed",
            "name: typed\n" + GOOD + "  opt_in: true\n",
            "x\n",
            "must be strings",
        ),
        ("big", "name: big\n" + GOOD, "x\n" * 500, "body is"),
        ("dash", "name: dash\n" + GOOD, "a \u2014 b\n", "an em dash"),
        ("pict", "name: pict\n" + GOOD, "ok \u2705\n", "pictographs"),
        ("lic", "name: lic\nlicense: see LICENSE\n" + GOOD, "x\n", "LICENSE"),
    ],
)
def test_the_lint_refuses(
    tmp_path: Path, folder: str, head: str, body: str, expected: str
) -> None:
    problems = lint_skill(skill_at(tmp_path, folder, head, body))
    assert any(expected in p for p in problems), problems


def test_the_lint_refuses_a_linked_folder_and_a_missing_skill_md(
    tmp_path: Path,
) -> None:
    real = skill_at(tmp_path, "real", "name: real\n" + GOOD)
    (tmp_path / "alias").symlink_to(real)
    assert "may not be a link" in lint_skill(tmp_path / "alias")[0]
    (tmp_path / "empty").mkdir()
    assert lint_skill(tmp_path / "empty") == ["empty: no SKILL.md"]
