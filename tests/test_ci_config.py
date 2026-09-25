"""Layer 3b for the floor: scripts/ci_config_from_base.sh judges a pull request's
configuration against the base's floor with the base revision's checker, so a
candidate that loosens the floor and rewrites its own checker is still refused."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

from tac.github import REQUIRED_CHECKS
from tests._gitrepo import REPO, commit_all, copy_toolchain, git

SCRIPT = REPO / "scripts" / "ci_config_from_base.sh"
FLOOR = ".agents/standards.floor.toml"
# What the config checker reads, beside the toolchain it runs from.
CONFIG = (".agents/config.toml", ".agents/config", ".agents/skills", FLOOR)
CHECKER = ".agents/lib/tac/src/tac/config.py"
STANDARDS = ".agents/lib/tac/src/tac/standards.py"
LOOSE = ('lint = "required"', 'lint = "optional"')
TIGHT = ("commit_max_subject = 72", "commit_max_subject = 64")


def edit(root: Path, rel: str, old: str, new: str) -> None:
    path = root / rel
    text = path.read_text("utf-8")
    assert old in text, f"{old!r} not in {rel}"
    path.write_text(text.replace(old, new, 1), "utf-8")


def repo(tmp_path: Path, extra: tuple[str, ...] = CONFIG) -> Path:
    root = tmp_path / "demo"
    root.mkdir()
    git(root, "init", "-q")
    copy_toolchain(root, extra)
    return root


def bring(root: Path, *rels: str) -> None:
    """Files and folders of this checkout the base lacked, into the candidate."""
    for rel in rels:
        source, target = REPO / rel, root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)


def candidate(root: Path) -> None:
    commit_all(root, "base")
    git(root, "branch", "base")
    git(root, "checkout", "-q", "-b", "candidate")


def blind_the_checker(root: Path) -> None:
    """The candidate's own checker stops seeing a loosened floor."""
    edit(
        root,
        STANDARDS,
        '    """Every way `head` asks less than `base`: a dropped key or a looser '
        'value."""\n',
        '    """Every way `head` asks less than `base`: a dropped key or a looser '
        'value."""\n    return []\n',
    )


def run_ci(root: Path) -> tuple[int, str]:
    environ = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    done = subprocess.run(
        ["bash", str(SCRIPT), "base"],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def test_a_loosened_floor_is_refused_though_the_candidate_blinds_its_checker(
    tmp_path: Path,
) -> None:
    root = repo(tmp_path)
    candidate(root)
    edit(root, FLOOR, *LOOSE)
    blind_the_checker(root)
    commit_all(root, "loosen the floor and the checker that would see it")
    code, output = run_ci(root)
    assert code != 0, output
    assert "floor key lint was loosened" in output, output


def test_a_tightened_floor_passes(tmp_path: Path) -> None:
    root = repo(tmp_path)
    candidate(root)
    edit(root, FLOOR, *TIGHT)
    commit_all(root, "tighten the floor")
    code, output = run_ci(root)
    assert code == 0, output
    assert "config holds" in output
    assert "::warning::" not in output


def test_a_base_with_a_floor_and_no_checker_is_refused(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / CHECKER).unlink()
    candidate(root)
    bring(root, CHECKER)
    commit_all(root, "bring the checker")
    code, output = run_ci(root)
    assert code == 1, output
    assert "but no checker to judge it" in output


def test_a_base_without_a_floor_or_checker_falls_back_loudly(
    tmp_path: Path,
) -> None:
    root = repo(tmp_path, extra=())
    (root / CHECKER).unlink()
    candidate(root)
    bring(root, CHECKER, *CONFIG)
    commit_all(root, "bring the floor and the checker")
    code, output = run_ci(root)
    assert "has no floor and no config checker" in output
    assert code == 0, output


def test_ci_runs_the_floor_check_from_the_base_in_a_required_job() -> None:
    ci = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    job = ci["jobs"]["gates"]
    # A step in a job the ruleset does not require would never block a merge.
    assert "gates" in REQUIRED_CHECKS
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    step = next(s for s in job["steps"] if "ci_config_from_base.sh" in s.get("run", ""))
    # A branch name reaches the shell as a variable, never as script text.
    assert "${{" not in step["run"]
    assert step["env"] == {"BASE_SHA": "${{ github.event.pull_request.base.sha }}"}
    # The base revision's copy of the script, taken by an earlier step.
    assert step["run"] == 'bash "$RUNNER_TEMP/gates/ci_config_from_base.sh" "$BASE_SHA"'
