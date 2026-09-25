"""The source guard under Python 3.9 (build condition C4).

macOS ships 3.9 as /usr/bin/python3 and a hook must run before any venv exists,
so the guard stays 3.9 compatible. This runs the candidate, the source
`hooks/run.py`, never the deployed `.agents/hooks/run.py`, which a builder
cannot write and which changes only after a deploy. The interpreter is the 3.9
uv finds: /usr/bin/python3 on a Mac, and in CI the one its workflow installs
with `uv python install 3.9` before the tests. The pin is the 3.9 language level,
not a patch release, because the guard's contract is what every macOS 3.9 runs.
The test never installs one itself, so it needs no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests._guard import (
    SOURCE,
    SOURCE_GUARD,
    STAMPED,
    Fixture,
    emitted,
    event,
    guarded,
    hostile,
    run_guard,
)


def _find() -> str:
    done = subprocess.run(
        ["uv", "python", "find", "--no-project", "3.9"],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() if done.returncode == 0 else ""


@pytest.fixture(scope="module")
def py39() -> str:
    found = _find()
    if not found:
        pytest.fail("uv finds no Python 3.9; run `uv python install 3.9` once")
    version = subprocess.run(
        [found, "-I", "-c", "import sys; print(sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert version == "(3, 9)", version
    return found


@pytest.fixture(params=["source", "stamped"])
def fx(request: pytest.FixtureRequest, tmp_path: Path) -> Fixture:
    fixture = guarded(tmp_path, layout=request.param)
    assert fixture.guard.read_bytes() == SOURCE_GUARD.read_bytes()
    assert fixture.rel == (SOURCE if request.param == "source" else STAMPED)
    return fixture


def test_the_source_guard_itself_loads_under_3_9(py39: str) -> None:
    done = subprocess.run(
        [py39, "-I", str(SOURCE_GUARD), "--help"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert done.returncode == 0, done.stderr
    assert "--client" in done.stdout


def test_allow_and_deny_under_3_9(py39: str, fx: Fixture) -> None:
    tool = event("PreToolUse", tool_name="Edit", tool_input={})
    assert run_guard(fx, "claude", "PreToolUse", tool, python=py39).returncode == 0
    fx.stub(verdict="deny", reason="refused under 3.9")
    for client in ("claude", "codex"):
        done = run_guard(fx, client, "PreToolUse", tool, python=py39)
        assert done.returncode == 2, done.stderr
        assert emitted(done)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_stop_and_session_start_under_3_9(py39: str, fx: Fixture) -> None:
    fx.stub(verdict="remind", reason="the order does not hold")
    assert (
        run_guard(
            fx, "claude", "SubagentStop", event("SubagentStop"), python=py39
        ).returncode
        == 2
    )
    assert run_guard(fx, "codex", "Stop", event("Stop"), python=py39).returncode == 2
    done = run_guard(fx, "codex", "SessionStart", event("SessionStart"), python=py39)
    assert done.returncode == 0
    assert (
        "the order does not hold"
        in emitted(done)["hookSpecificOutput"]["additionalContext"]
    )


def test_tampering_is_refused_under_3_9(py39: str, fx: Fixture) -> None:
    with fx.guard.open("a", encoding="utf-8") as handle:
        handle.write("# tampered\n")
    done = run_guard(fx, "claude", "PreToolUse", event("PreToolUse"), python=py39)
    assert done.returncode == 2
    assert f"{fx.rel} does not match" in done.stderr


def test_a_missing_venv_fails_closed_under_3_9(py39: str, tmp_path: Path) -> None:
    fx = guarded(tmp_path, layout="source", checker="none")
    done = run_guard(fx, "codex", "PreToolUse", event("PreToolUse"), python=py39)
    assert done.returncode == 2
    assert "run tac init" in done.stderr
    stop = run_guard(fx, "codex", "Stop", event("Stop"), python=py39)
    assert stop.returncode == 0


def test_a_hostile_checkout_changes_nothing_under_3_9(
    py39: str, fx: Fixture, tmp_path: Path
) -> None:
    env, marker, work_dir = hostile(fx, tmp_path)
    fx.stub(verdict="deny", reason="the stub answered")
    done = run_guard(
        fx,
        "claude",
        "PreToolUse",
        event("PreToolUse"),
        python=py39,
        env=env,
        cwd=work_dir,
    )
    assert not marker.exists(), marker.read_text("utf-8")
    assert done.returncode == 2
    assert "[stub] the stub answered" in done.stderr
