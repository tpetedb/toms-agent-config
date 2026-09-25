"""What the guard loads and runs, whatever the session prepared (build
condition C2).

A rendered hook command starts an absolute interpreter with -I on an absolute
guard, under a fixed PATH. These tests give it the worst checkout and
environment an agent could prepare, a shim first on PATH, planted modules in the
checkout, the working folder and PYTHONPATH, and a project `pyproject.toml` and
`uv.toml` pointing uv elsewhere, and assert that nothing planted runs and the
checker still answers from the deployed venv. They also start the guard in ways
-I alone cannot rule out, to reach the refusals that stand behind it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tac.sync import sync
from tests._guard import (
    Fixture,
    base_python,
    emitted,
    event,
    guarded,
    hostile,
    patch,
    real_checkout,
    rendered,
    run_guard,
    tool_event,
)
from tests._syncproject import copy_project


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return guarded(tmp_path)


@pytest.fixture
def real(tmp_path: Path) -> Fixture:
    return real_checkout(tmp_path)


def test_path_pythonpath_and_planted_modules_change_nothing_the_guard_loads(
    fx: Fixture, tmp_path: Path
) -> None:
    env, marker, work_dir = hostile(fx, tmp_path)
    fx.stub(mode="echo")
    done = run_guard(
        fx, "claude", "SessionStart", event("SessionStart"), env=env, cwd=work_dir
    )
    assert done.returncode == 0, done.stderr
    assert not marker.exists(), marker.read_text("utf-8")
    seen = json.loads(emitted(done)["hookSpecificOutput"]["additionalContext"])
    # The checker got the fixed PATH and none of the variables that steer Python,
    # uv or git, nor the token.
    assert seen["env"]["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    for name in env:
        if name not in ("PATH", "HOME"):
            assert name not in seen["env"], name
    assert seen["cwd"] == os.path.realpath(fx.root)
    for entry in seen["path"]:
        assert entry, "the working folder is on the checker's sys.path"
        for place in (fx.root, work_dir, tmp_path / "planted"):
            inside = os.path.realpath(entry).startswith(os.path.realpath(place))
            assert not inside or ".agents/.venv" in entry, entry


def test_a_real_checker_still_answers_from_the_venv_in_a_hostile_checkout(
    tmp_path: Path,
) -> None:
    fx = guarded(tmp_path, checker="candidate")
    copy_project(fx.root)
    sync(fx.root, links=False)
    env, marker, work_dir = hostile(fx, tmp_path)
    target = fx.root / "AGENTS.md"
    done = run_guard(
        fx,
        "claude",
        "PreToolUse",
        tool_event("PreToolUse", file_path=str(target)),
        env=env,
        cwd=work_dir,
    )
    assert not marker.exists(), marker.read_text("utf-8")
    assert done.returncode == 2, done.stderr
    assert "[generated-paths] AGENTS.md is generated" in done.stderr


@pytest.mark.skipif(
    not os.access("/usr/bin/python3", os.X_OK), reason="no system interpreter"
)
@pytest.mark.parametrize("client", ["claude", "codex"])
def test_the_rendered_command_runs_the_stamped_guard_in_a_hostile_session(
    real: Fixture, tmp_path: Path, client: str
) -> None:
    """The exact command tac sync renders, run by a shell as the client runs it,
    from an environment and a checkout an agent prepared: it reaches the real
    checker, refuses an edit of a generated file, and loads nothing planted."""
    done = subprocess.run(
        ["/usr/bin/git", "init", "-q", str(real.root)], capture_output=True, check=False
    )
    assert done.returncode == 0, done.stderr
    env, marker, work_dir = hostile(real, tmp_path)
    target = real.root / ".claude" / "settings.json"
    if client == "claude":
        env["CLAUDE_PROJECT_DIR"] = str(real.root)
        cwd = work_dir
        call = tool_event("PreToolUse", file_path=str(target))
    else:
        # Codex names no project folder, so the command asks git from the
        # session's folder, anywhere inside the checkout.
        cwd = real.root / "src"
        cwd.mkdir(exist_ok=True)
        call = tool_event(
            "PreToolUse",
            tool="apply_patch",
            command=patch("Update File: ../.claude/settings.json"),
        )
    call["cwd"] = str(cwd)
    command = rendered(real.root, client, "PreToolUse")
    ran = subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps(call),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
        timeout=120,
    )
    assert not marker.exists(), marker.read_text("utf-8")
    assert ran.returncode == 2, ran.stderr
    assert "[generated-paths] .claude/settings.json is generated" in ran.stderr
    stop = subprocess.run(
        ["/bin/sh", "-c", rendered(real.root, client, "Stop")],
        input=json.dumps(event("Stop", cwd=str(cwd))),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
        timeout=120,
    )
    assert not marker.exists(), marker.read_text("utf-8")
    assert stop.returncode == 0, stop.stderr
    assert "tac guard" not in stop.stdout + stop.stderr


# ---------------------------------------------------------------- behind -I

# -I keeps the checkout and the working folder off sys.path, so these refusals
# never fire from a rendered command. A start through runpy reaches them: it
# stands for any way a module or a path entry from the checkout could be in
# place before the guard's own code runs.
RUNPY = """
import runpy, sys
guard, root, how = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, root)
if how == "module":
    import planted_here
    sys.path.remove(root)
sys.argv = [guard, "--client", "claude", "--event", "PreToolUse"]
runpy.run_path(guard, run_name="__main__")
"""


def run_through_runpy(fx: Fixture, how: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [base_python(), "-I", "-c", RUNPY, str(fx.guard), str(fx.root), how],
        input=json.dumps(tool_event("PreToolUse")),
        capture_output=True,
        text=True,
        cwd=fx.root,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
        timeout=60,
    )


def test_a_checkout_folder_on_sys_path_is_refused(fx: Fixture) -> None:
    done = run_through_runpy(fx, "path")
    assert done.returncode == 2, done.stderr
    assert f"sys.path holds {fx.root}" in done.stderr
    assert emitted(done)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_module_imported_from_the_checkout_is_refused(fx: Fixture) -> None:
    (fx.root / "planted_here.py").write_text("PLANTED = True\n", "utf-8")
    done = run_through_runpy(fx, "module")
    assert done.returncode == 2, done.stderr
    assert "planted_here was imported from" in done.stderr
    assert emitted(done)["hookSpecificOutput"]["permissionDecision"] == "deny"
