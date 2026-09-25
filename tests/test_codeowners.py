"""`tac doctor` fails naming each path CI runs from that CODEOWNERS leaves open.

On pull_request the workflow and the gate scripts are the candidate's, so the
owner's required review is the only layer an agent cannot bypass, and it reaches
only what CODEOWNERS names (docs/DESIGN.md, section 8).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tac.codeowners import (
    NEW_ATTRIBUTES,
    NEW_GATE,
    NEW_WORKFLOW,
    ci_gaps,
    owners_of,
    parse,
)
from tac.doctor import CHECKS, Status, check_codeowners_ci
from tests._gitrepo import REPO, write

WORKFLOW = """on: pull_request
jobs:
  verify:
    steps:
      - env:
          GATES: private_scan.sh
        run: bash "$RUNNER_TEMP/gates/private_scan.sh"
"""
# The CI paths of the fixture below, each of which a gap must name.
PATHS = (
    ".github/workflows/ci.yml",
    ".github/CODEOWNERS",
    "scripts/ci_work.sh",
    "scripts/private_scan.sh",
    "justfile",
    ".gitattributes",
    NEW_ATTRIBUTES,
    NEW_WORKFLOW,
    NEW_GATE,
)
COVERED = """/.github/  @owner
/scripts/  @owner
/justfile  @owner
.gitattributes  @owner
"""


def project(tmp_path: Path, codeowners: str | None) -> Path:
    write(tmp_path, ".github/workflows/ci.yml", WORKFLOW)
    write(tmp_path, "scripts/ci_work.sh", "exit 0\n")
    write(tmp_path, "scripts/private_scan.sh", "exit 0\n")
    # Named by no workflow and not a ci_ gate: CI never runs it.
    write(tmp_path, "scripts/stamp_lib.py", "")
    write(tmp_path, "justfile", "verify:\n    true\n")
    if codeowners is not None:
        write(tmp_path, ".github/CODEOWNERS", codeowners)
    return tmp_path


def test_this_repositorys_codeowners_covers_what_ci_runs() -> None:
    status, detail = check_codeowners_ci(REPO)
    assert status is Status.PASS, detail
    assert "codeowners-ci" in [c.name for c in CHECKS]


def test_folder_rules_on_github_scripts_and_the_justfile_pass(tmp_path: Path) -> None:
    status, detail = check_codeowners_ci(project(tmp_path, COVERED))
    assert status is Status.PASS, detail
    assert "9 paths" in detail


def test_a_missing_codeowners_fails(tmp_path: Path) -> None:
    status, detail = check_codeowners_ci(project(tmp_path, None))
    assert status is Status.FAIL
    assert "no CODEOWNERS" in detail


def test_codeowners_that_guard_other_paths_fail_naming_every_ci_path(
    tmp_path: Path,
) -> None:
    status, detail = check_codeowners_ci(project(tmp_path, "/src/ @owner\n"))
    assert status is Status.FAIL
    for path in PATHS:
        assert f"{path}: no rule matches" in detail, path
    assert "scripts/stamp_lib.py" not in detail


@pytest.mark.parametrize(
    ("codeowners", "gap"),
    [
        # Today's workflow only: a new workflow a pull request adds is open.
        (
            COVERED.replace("/.github/  @owner", "/.github/workflows/ci.yml @owner\n")
            + "/.github/CODEOWNERS @owner\n",
            f"{NEW_WORKFLOW}: no rule matches",
        ),
        # A later rule with no owner takes one gate back out.
        (COVERED + "/scripts/ci_work.sh\n", "scripts/ci_work.sh: the last matching"),
        (COVERED.replace("/justfile  @owner", ""), "justfile: no rule matches"),
        # A script is guarded because a workflow names it, not by its folder.
        (
            COVERED + "/scripts/private_scan.sh\n",
            "scripts/private_scan.sh: the last matching rule names no owner",
        ),
        (COVERED + "/scripts/ @tac-bot\n", f"{NEW_GATE}: owned only by the agent"),
        # Attributes can mark a text file binary, so every one is owned: the
        # root rule alone leaves a folder's own file open.
        (
            COVERED.replace(".gitattributes  @owner", ""),
            ".gitattributes: no rule matches",
        ),
        (
            COVERED.replace(".gitattributes  @owner", "/.gitattributes  @owner"),
            f"{NEW_ATTRIBUTES}: no rule matches",
        ),
        # CODEOWNERS itself: whoever edits it decides every other rule.
        (COVERED + "/.github/CODEOWNERS\n", ".github/CODEOWNERS: the last matching"),
    ],
)
def test_each_gap_is_named(tmp_path: Path, codeowners: str, gap: str) -> None:
    status, detail = check_codeowners_ci(project(tmp_path, codeowners))
    assert status is Status.FAIL
    assert gap in detail, detail


def test_every_gap_and_only_those_is_listed(tmp_path: Path) -> None:
    root = project(tmp_path, COVERED + "/scripts/ci_work.sh\n/justfile\n")
    assert ci_gaps(root, "tac-bot") == [
        "scripts/ci_work.sh: the last matching rule names no owner",
        "justfile: the last matching rule names no owner",
    ]


# ---- patterns, read as GitHub documents them
# https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners#example-of-a-codeowners-file


@pytest.mark.parametrize(
    ("pattern", "path", "owned"),
    [
        ("*", "a/b/c.txt", True),
        ("*.js", "src/deep/app.js", True),
        ("*.js", "src/app.jsx", False),
        ("/build/logs/", "build/logs/x/y.log", True),
        ("/build/logs/", "other/build/logs/y.log", False),
        ("docs/*", "docs/getting-started.md", True),
        ("docs/*", "docs/build-app/troubleshooting.md", False),
        ("apps/", "deep/apps/x.py", True),
        ("/docs/", "docs/a/b.md", True),
        ("**/logs", "a/b/logs/x.log", True),
        ("/apps/github", "apps/github/x", True),
        ("/apps/github", "x/apps/github/y", False),
        ("/scripts/ci_*.sh", "scripts/ci_work.sh", True),
        ("/scripts/ci_*.sh", "scripts/private_scan.sh", False),
        ("/justfile", "justfile", True),
        # GitHub skips a negation or a character range.
        ("!/justfile", "justfile", False),
        ("/[j]ustfile", "justfile", False),
    ],
)
def test_patterns_match_as_github_reads_them(
    pattern: str, path: str, owned: bool
) -> None:
    assert (owners_of(parse(f"{pattern} @owner\n"), path) is not None) is owned


def test_the_last_matching_rule_wins_and_comments_are_skipped() -> None:
    rules = parse(
        "# a comment\n*  @everyone  # trailing words are a comment\n/src/ @owner\n"
    )
    assert owners_of(rules, "README.md") == ("@everyone",)
    assert owners_of(rules, "src/a.py") == ("@owner",)
