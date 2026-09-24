"""Layer 3b for work orders: scripts/ci_work_from_base.sh judges a pull request's
orders with the base revision's checker, so a candidate that edits the stamped
checker cannot change how its own orders are judged."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests._gitrepo import REPO, commit_all, copy_toolchain, git, make_repo, write

SCRIPT = REPO / "scripts" / "ci_work_from_base.sh"
TEAMS = (".agents/config/teams.toml",)
CHECKER = ".agents/lib/tac/src/tac/work.py"
# Stands for any change to the candidate's checker: if it runs, it says so.
MARKER = "candidate checker ran"


def candidate(tmp_path: Path, base_has_checker: bool) -> Path:
    root = tmp_path / "demo"
    make_repo(root)
    if base_has_checker:
        copy_toolchain(root, TEAMS)
        commit_all(root, "stamp the toolchain")
    git(root, "branch", "base")
    git(root, "checkout", "-q", "-b", "candidate")
    if not base_has_checker:
        copy_toolchain(root, TEAMS)
    checker = root / CHECKER
    checker.write_text(
        checker.read_text("utf-8") + f"\nraise SystemExit({MARKER!r})\n", "utf-8"
    )
    write(root, "src/app.py", "print('candidate')\n")
    commit_all(root, "candidate edits its own checker")
    return root


def run_ci(root: Path) -> tuple[int, str]:
    environ = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    done = subprocess.run(
        ["bash", str(SCRIPT), "base", "candidate"],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def test_the_base_checker_judges_and_the_candidates_never_runs(
    tmp_path: Path,
) -> None:
    code, output = run_ci(candidate(tmp_path, base_has_checker=True))
    assert code == 0, output
    assert MARKER not in output
    assert "::warning::" not in output


def test_a_base_without_a_checker_falls_back_loudly(tmp_path: Path) -> None:
    code, output = run_ci(candidate(tmp_path, base_has_checker=False))
    assert "has no stamped checker" in output
    assert MARKER in output
    assert code != 0
