"""Build condition C1: the guard's failure modes are tested, not assumed.

A hook that fails is not a hook that refuses (design section 5, 'When the guard
itself fails'). Each case runs the guard the way a client runs it, the stamped
`run.py` as a subprocess with the candidate checker in its venv, against a real
runner serving a short socket with its own key: a missing token, an altered
token, one token spent by two concurrent spawns, the runner lost in the middle
of a check, and a hook timeout. Where refusal must survive a hook failure, the
enterprise profile turns native delegation off, and the rendered settings deny
the spawn tools outright.

Nothing waits on a clock but the timeout case, which is bounded by the guard's
own deadline and waits on the guard's process exit; the concurrent and lost
cases meet on a Barrier and an Event.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import socket
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tac import runner as runner_module
from tac.config import load_config
from tac.runner import (
    Runner,
    RunnerServer,
    bind,
    controller_store,
    create_key,
    ensure_store,
    open_runner,
)
from tac.sync import sync
from tests._gitrepo import ORIGIN, git, short_dir
from tests._guard import (
    Fixture,
    emitted,
    event,
    guarded,
    real_checkout,
    run_guard,
)
from tests._syncproject import copy_project, replace_in, ultracode_off

SESSION = "7b0c9f7e-2d5e-4a57-9a39-1f7f4c3c9d11"
PROMPT = "Draft the order spec from the request."
DIGEST = hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def no_agent_ancestors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tac.runner.ancestor_commands", lambda _pid: [])


@dataclass
class Served:
    fx: Fixture
    home: Path
    runner: Runner
    server: RunnerServer

    def env(self) -> dict[str, str]:
        # The guard hands the checker HOME and not TAC_STATE_HOME, so the
        # checker finds the controller store under HOME, as on the host.
        return {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}

    def dispatch(self, token: str) -> None:
        folder = self.fx.root / ".git" / "agents" / "dispatch"
        folder.mkdir(parents=True, exist_ok=True)
        record = {
            "run_id": "r1",
            "stage": "specify",
            "role": "chief",
            "token": token,
            "kind": "agent",
            "session_id": SESSION,
        }
        (folder / f"{SESSION}.json").write_text(json.dumps(record), "utf-8")

    def spawn(self) -> subprocess.CompletedProcess[str]:
        payload = event(
            "PreToolUse",
            session_id=SESSION,
            cwd=str(self.fx.root),
            tool_name="Agent",
            agent_type="chief",
            tool_input={"prompt": PROMPT, "subagent_type": "scout"},
        )
        return run_guard(self.fx, "claude", "PreToolUse", payload, env=self.env())

    def live_tokens(self) -> list[Path]:
        return sorted((self.runner.store / "tokens").glob("*.json"))


@pytest.fixture(scope="module")
def checkout(tmp_path_factory: pytest.TempPathFactory) -> Fixture:
    """One synced checkout with the candidate checker, built once: a venv and a
    render per test would make the module slow for nothing."""
    fx = real_checkout(tmp_path_factory.mktemp("guard-failure"))
    git(fx.root, "init", "-q")
    git(fx.root, "remote", "add", "origin", ORIGIN)
    return fx


@pytest.fixture
def served(checkout: Fixture) -> Iterator[Served]:
    with short_dir() as home:
        # The state folder the checker derives from HOME, given to the runner
        # the way the owner's shell would: through XDG_STATE_HOME.
        environ = {"XDG_STATE_HOME": str(home / ".local" / "state")}
        store = ensure_store(controller_store(checkout.root, environ))
        create_key(store)
        found = open_runner(checkout.root, environ, repository="example/demo")
        server = bind(found)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield Served(checkout, home, found, server)
        finally:
            with contextlib.suppress(OSError):
                server.shutdown()
                server.server_close()
            dispatch = checkout.root / ".git" / "agents" / "dispatch"
            for leftover in dispatch.glob("*.json") if dispatch.is_dir() else []:
                leftover.unlink()


def denied(done: subprocess.CompletedProcess[str], why: str) -> None:
    assert done.returncode == 2, done.stdout + done.stderr
    decision = emitted(done)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert why in decision["permissionDecisionReason"], decision


def test_a_valid_token_lets_one_spawn_through(served: Served) -> None:
    served.dispatch(
        served.runner.issue_token("r1", "specify", DIGEST, "agent", SESSION)
    )
    done = served.spawn()
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_missing_token_is_a_deny(served: Served) -> None:
    # The runner dispatched this session (its marker), but no record holds a token.
    marker = served.runner.store / "dispatched"
    marker.mkdir(exist_ok=True)
    (marker / SESSION).write_text("r1 specify\n", encoding="utf-8")
    try:
        denied(served.spawn(), "no dispatch record")
    finally:
        (marker / SESSION).unlink()


def test_an_altered_token_is_a_deny_and_burns_nothing_else(served: Served) -> None:
    token = served.runner.issue_token("r1", "specify", DIGEST, "agent", SESSION)
    altered = ("0" if token[0] != "0" else "1") + token[1:]
    served.dispatch(altered)
    before = served.live_tokens()
    denied(served.spawn(), "unknown, or already used")
    assert served.live_tokens() == before
    # The real token still works once.
    served.dispatch(token)
    assert served.spawn().returncode == 0


OTHER = "0f3c2a8e-7d41-4b0c-8c55-2e9d6b1a4f70"


def test_a_session_presenting_another_session_s_token_is_a_deny(
    served: Served,
) -> None:
    # The runner dispatched OTHER for specify and this session for review; this
    # session copied OTHER's run, stage and token into its own record.
    stolen = served.runner.issue_token("r1", "specify", DIGEST, "agent", OTHER)
    marker = served.runner.store / "dispatched"
    marker.mkdir(exist_ok=True)
    (marker / SESSION).write_text("r1 review\n", encoding="utf-8")
    served.dispatch(stolen)
    before = served.live_tokens()
    try:
        denied(served.spawn(), "another run or stage")
        # Refused before the runner was asked: OTHER's token is still live.
        assert served.live_tokens() == before
    finally:
        (marker / SESSION).unlink()
    # With no marker to compare, the runner holds the session binding itself,
    # and the copied token is burned rather than spent.
    denied(served.spawn(), "session_id")
    assert len(served.live_tokens()) == len(before) - 1


def test_a_token_reused_by_two_concurrent_spawns_lets_exactly_one_through(
    served: Served,
) -> None:
    served.dispatch(
        served.runner.issue_token("r1", "specify", DIGEST, "agent", SESSION)
    )
    start = threading.Barrier(2)
    results: list[subprocess.CompletedProcess[str]] = []
    lock = threading.Lock()

    def spawn() -> None:
        start.wait()
        done = served.spawn()
        with lock:
            results.append(done)

    threads = [threading.Thread(target=spawn) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    codes = sorted(r.returncode for r in results)
    assert codes == [0, 2], [r.stdout + r.stderr for r in results]
    [refused] = [r for r in results if r.returncode == 2]
    denied(refused, "already used")


def test_the_runner_lost_mid_check_is_a_deny(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    served.dispatch(
        served.runner.issue_token("r1", "specify", DIGEST, "agent", SESSION)
    )
    reached = threading.Event()
    release = threading.Event()

    def lost(self: Any) -> None:
        # The request arrived; the runner goes away before it answers.
        self.rfile.readline()
        reached.set()
        release.wait()
        self.connection.shutdown(socket.SHUT_RDWR)

    monkeypatch.setattr(runner_module._Handler, "handle", lost)  # pyright: ignore[reportPrivateUsage]
    results: list[subprocess.CompletedProcess[str]] = []
    guard = threading.Thread(target=lambda: results.append(served.spawn()))
    guard.start()
    assert reached.wait(timeout=60)
    served.server.shutdown()
    served.server.server_close()
    release.set()
    guard.join()
    [done] = results
    denied(done, "without a reply")


def test_a_hook_timeout_answers_deny_before_the_client_gives_up(
    tmp_path: Path,
) -> None:
    fx = guarded(tmp_path)
    fx.stub(mode="hang")
    payload = event(
        "PreToolUse",
        tool_name="Agent",
        agent_type="chief",
        tool_input={"prompt": PROMPT},
    )
    # The guard's own deadline (1 s) is under the client's hook timeout; the
    # test waits on the guard's exit, never on a sleep.
    done = run_guard(fx, "claude", "PreToolUse", payload, deadline=1)
    denied(done, "no answer within 1s")


def test_the_enterprise_profile_takes_the_spawn_tools_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A container profile renders only inside a container, so the test says so.
    monkeypatch.setattr("tac.config.container_evidence", lambda *_: "/.dockerenv")
    root = copy_project(tmp_path / "project")
    ultracode_off(root)
    replace_in(
        root / ".agents/config.toml", 'active = "standard"', 'active = "enterprise"'
    )
    sync(root, links=False)
    config = load_config(root)
    assert config.profile.name == "enterprise"
    assert config.profile.native_delegation == "off"
    settings = json.loads((root / ".claude/settings.json").read_text("utf-8"))
    assert {"Agent", "Task", "Workflow"} <= set(settings["permissions"]["deny"])
