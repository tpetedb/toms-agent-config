"""The bypass attempts of design section 9 that no other test makes, offline.

"What dev/ proves about the layers, including each bypass attempt" lists them;
each test here is named after its sentence. Where a case already has a test it
is mapped in dev/coverage/map.toml and not repeated: a forged approval
(test_approvals.py), a spawn with a missing, an altered or a concurrently
reused token through the stamped guard (test_guard_failure.py), a result that
fails its contract (test_handoff.py, test_runner.py). The GitHub-bound attempts
and the classifier are live probes (tac.proof.LIVE_PROBES).

Every guard run here is the stamped `run.py` as a client starts it, with the
candidate checker in its venv, against a real runner on a short socket with its
own key; nothing waits on a clock.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tac.receipts import SignedReceipt
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
from tests._gitrepo import GIT, ORIGIN, git, short_dir
from tests._guard import Fixture, emitted, event, real_checkout, run_guard
from tests._syncproject import copy_project, replace_in
from tests.test_receipt_ci import base_repo, commit_receipt, receipt, run_ci
from tests.test_workflow_guard import BODY, register

SESSION = "4f0d2c1b-8a37-4e59-b1c6-0d9e8f7a6b52"
PROMPT = "Draft the order spec from the request."


@pytest.fixture(autouse=True)
def no_agent_ancestors(monkeypatch: pytest.MonkeyPatch) -> None:
    # The in-process runner must not take the test's own ancestors for an agent.
    monkeypatch.setattr("tac.runner.ancestor_commands", lambda _pid: [])


# ---------------------------------------------------------------- a served checkout


@dataclass
class Served:
    fx: Fixture
    home: Path
    runner: Runner

    def dispatch(self, token: str, kind: str) -> None:
        folder = self.fx.root / ".git" / "agents" / "dispatch"
        folder.mkdir(parents=True, exist_ok=True)
        record = {
            "run_id": "r1",
            "stage": "specify",
            "role": "chief",
            "token": token,
            "kind": kind,
            "session_id": SESSION,
        }
        (folder / f"{SESSION}.json").write_text(json.dumps(record), "utf-8")

    def call(
        self, tool: str, role: str, **tool_input: Any
    ) -> subprocess.CompletedProcess[str]:
        payload = event(
            "PreToolUse",
            session_id=SESSION,
            cwd=str(self.fx.root),
            tool_name=tool,
            agent_type=role,
            tool_input=tool_input,
        )
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        return run_guard(self.fx, "claude", "PreToolUse", payload, env=env)


@pytest.fixture(scope="module")
def checkout(tmp_path_factory: pytest.TempPathFactory) -> Fixture:
    """A synced checkout whose order pipeline registers one Workflow script on
    the chief's specify stage, with the candidate checker in its venv."""
    fx = real_checkout(tmp_path_factory.mktemp("bypass"))
    register(fx.root)
    sync(fx.root, links=False)
    git(fx.root, "init", "-q")
    git(fx.root, "remote", "add", "origin", ORIGIN)
    return fx


@pytest.fixture
def served(checkout: Fixture) -> Iterator[Served]:
    with short_dir() as home:
        environ = {"XDG_STATE_HOME": str(home / ".local" / "state")}
        store = ensure_store(controller_store(checkout.root, environ))
        create_key(store)
        found = open_runner(checkout.root, environ, repository="example/demo")
        server: RunnerServer = bind(found)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield Served(checkout, home, found)
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
    assert "[handoff-guard]" in decision["permissionDecisionReason"]
    assert why in decision["permissionDecisionReason"], decision


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- spawns and Workflow


def test_a_native_spawn_whose_token_was_already_spent_is_denied(served: Served) -> None:
    """A native subagent spawn without a valid dispatch token is denied: a token
    spent once, presented again later from the same record."""
    served.dispatch(
        served.runner.issue_token("r1", "specify", sha(PROMPT), "agent", SESSION),
        "agent",
    )
    first = served.call("Agent", "chief", prompt=PROMPT, subagent_type="scout")
    assert first.returncode == 0, first.stdout + first.stderr
    denied(
        served.call("Agent", "chief", prompt=PROMPT, subagent_type="scout"),
        "already used",
    )


def test_a_workflow_call_with_a_reused_token_is_denied(served: Served) -> None:
    served.dispatch(
        served.runner.issue_token("r1", "specify", sha(BODY), "workflow", SESSION),
        "workflow",
    )
    first = served.call("Workflow", "chief", script=BODY)
    assert first.returncode == 0, first.stdout + first.stderr
    denied(served.call("Workflow", "chief", script=BODY), "already used")


def test_a_workflow_call_without_a_token_is_denied(served: Served) -> None:
    done = served.call("Workflow", "chief", script=BODY)
    assert done.returncode == 2, done.stdout + done.stderr
    assert "[handoff-guard]" in done.stderr


def test_a_workflow_call_with_an_unregistered_script_is_denied(served: Served) -> None:
    other = BODY.replace("Draft the order spec", "Do anything at all")
    served.dispatch(
        served.runner.issue_token("r1", "specify", sha(other), "workflow", SESSION),
        "workflow",
    )
    before = sorted((served.runner.store / "tokens").glob("*.json"))
    done = served.call("Workflow", "chief", script=other)
    assert done.returncode == 2, done.stdout + done.stderr
    assert "[handoff-guard]" in done.stderr
    # Refused on the registry, before the runner was asked for the token.
    assert sorted((served.runner.store / "tokens").glob("*.json")) == before


def test_a_workflow_call_from_a_builder_is_denied(served: Served) -> None:
    served.dispatch(
        served.runner.issue_token("r1", "specify", sha(BODY), "workflow", SESSION),
        "workflow",
    )
    denied(served.call("Workflow", "builder", script=BODY), "delegates = false")


# ---------------------------------------------------------------- a policy tamper


@pytest.fixture
def committed(tmp_path: Path) -> Path:
    root = copy_project(tmp_path / "project")
    sync(root, links=False)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def check_staged(root: Path) -> subprocess.CompletedProcess[str]:
    """`tac check --staged`, the pre-commit hook's command, as prek runs it."""
    subprocess.run([*GIT, "-C", str(root), "add", "-A"], check=True)
    return subprocess.run(
        [sys.executable, "-m", "tac", "check", "--staged", "--root", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_policy_tamper_written_through_a_shell_is_refused_at_commit(
    committed: Path,
) -> None:
    """A worker's shell writes past the guard; the commit is refused whether the
    tamper drops the policy's own protection or only loosens what it denies."""
    policy = committed / ".agents/config/policy.toml"
    original = policy.read_text("utf-8")
    replace_in(
        policy, 'deny_write = [".agents/**", ".codex/**"]', 'deny_write = [".codex/**"]'
    )
    done = check_staged(committed)
    assert done.returncode == 1
    assert "deny_write must keep .agents/**" in done.stdout + done.stderr
    policy.write_text(original, encoding="utf-8")
    replace_in(policy, '"*.env", ', "")
    done = check_staged(committed)
    assert done.returncode == 1, done.stdout + done.stderr
    assert ".agents/config/policy.toml" in done.stdout + done.stderr


# ---------------------------------------------------------------- an altered receipt


def test_an_altered_receipt_is_recomputed_and_rejected_by_the_ci_verifier_script(
    tmp_path: Path,
) -> None:
    """The CI script with the base revision's own verifier and key: a committed
    receipt whose observed result was edited after signing is refused."""
    key = Ed25519PrivateKey.generate()
    root = base_repo(tmp_path, key, verifier=True)
    signed = receipt(root, key)
    body = signed.receipt.model_copy(
        update={"observed": {**signed.receipt.observed, "exit": 1}}
    )
    commit_receipt(root, SignedReceipt(receipt=body, signature=signed.signature))
    code, output = run_ci(root)
    assert code == 1, output
    assert "signature does not match" in output
