"""The runner skeleton: its store, its key, its socket, and what it will sign."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from tac.cli import cli
from tac.receipts import (
    RUNNER_PUB,
    Binding,
    ReceiptError,
    load_public_key,
    parse,
    policy_hash,
    verify,
)
from tac.runner import (
    SANDBOX_EXEC,
    Runner,
    RunnerError,
    agent_command,
    ancestor_commands,
    bind,
    controller_store,
    create_key,
    default_sandbox,
    ensure_store,
    inside_sandbox,
    install_command,
    key_path,
    load_key,
    open_runner,
    refuse_agent_parent,
    request,
    require_own_venv,
    runner_venv,
    socket_path,
    state_home,
    store_slug,
)
from tests._gitrepo import REPOSITORY, commit_all, git, make_repo, short_dir, write

PYTHON = Path(sys.executable)
# A gate or probe observes the candidate tree only inside the macOS Seatbelt
# sandbox; elsewhere the runner refuses, which has its own test below.
seatbelt = pytest.mark.skipif(
    default_sandbox() is None, reason="needs macOS sandbox-exec"
)
# Keeps an agent session's own markers out of the commands under test.
NO_AGENT = {"CLAUDECODE": None, "CODEX_SANDBOX": None}


@pytest.fixture(autouse=True)
def no_agent_ancestors(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests may themselves run under an agent client; the ancestry
    tripwire has its own tests below with the chain passed in."""
    monkeypatch.setattr("tac.runner.ancestor_commands", lambda _pid: [])


@pytest.fixture
def state() -> Iterator[Path]:
    with short_dir() as path:
        yield path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    make_repo(root)
    return root


def env(state: Path) -> dict[str, str]:
    return {"TAC_STATE_HOME": str(state)}


def provision(repo: Path, state: Path) -> Path:
    store = ensure_store(controller_store(repo, env(state)))
    create_key(store)
    return store


def fake_client(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def fake_just(folder: Path) -> Path:
    """A `just` that prints the command line it was given and exits 3.

    It also records it next to itself, which only an unsandboxed run can do:
    the tests of a refusal assert the record is absent.
    """
    record = folder / "just-argv"
    body = f'printf "%s\\n" "$@" | tee "{record}" 2>/dev/null\nexit 3'
    fake_client(folder, "just", body)
    return record


def make_runner(repo: Path, state: Path, clients: Path | None = None) -> Runner:
    provision(repo, state)
    clients = clients or repo.parent / "bin"
    fake_just(clients)
    search = [str(PYTHON.parent), str(clients), "/usr/bin", "/bin"]
    return open_runner(repo, env(state), search_path=os.pathsep.join(search))


@contextlib.contextmanager
def serving(runner: Runner) -> Iterator[Path]:
    server = bind(runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield socket_path(runner.store)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


# ---- the controller store


def test_the_store_lives_under_the_user_state_dir(repo: Path, state: Path) -> None:
    store = controller_store(repo, env(state))
    assert store.parent == state.resolve()
    assert store.name == store_slug(repo)
    assert store.name.startswith("demo-")


def test_state_home_follows_xdg_then_the_default() -> None:
    assert state_home({"XDG_STATE_HOME": "/srv/state"}) == Path("/srv/state/tac")
    assert state_home({}) == Path.home() / ".local" / "state" / "tac"
    assert state_home({"TAC_STATE_HOME": "/srv/tac"}) == Path("/srv/tac")


def test_every_worktree_shares_one_store(repo: Path, state: Path) -> None:
    other = repo.parent / "demo-wt"
    git(repo, "worktree", "add", "-q", str(other), "-b", "side")
    assert controller_store(other, env(state)) == controller_store(repo, env(state))


def test_a_store_inside_the_repository_is_refused(repo: Path) -> None:
    with pytest.raises(RunnerError, match="outside the repository"):
        controller_store(repo, {"TAC_STATE_HOME": str(repo / "state")})


def test_a_store_inside_the_git_directory_is_refused(repo: Path) -> None:
    with pytest.raises(RunnerError, match="outside the repository"):
        controller_store(repo, {"TAC_STATE_HOME": str(repo / ".git" / "tac")})


def test_the_store_and_the_key_are_private(repo: Path, state: Path) -> None:
    store = provision(repo, state)
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert stat.S_IMODE((store / "signing-key.pem").stat().st_mode) == 0o600


def test_the_key_is_created_once(repo: Path, state: Path) -> None:
    store = provision(repo, state)
    first = load_key(store).public_key().public_bytes_raw()
    again = create_key(store).public_key().public_bytes_raw()
    assert first == again


def test_a_key_readable_by_others_is_refused(repo: Path, state: Path) -> None:
    store = provision(repo, state)
    (store / "signing-key.pem").chmod(0o644)
    with pytest.raises(RunnerError, match="0600"):
        load_key(store)


def test_a_runner_without_a_key_is_refused(repo: Path, state: Path) -> None:
    with pytest.raises(RunnerError, match="no signing key"):
        open_runner(repo, env(state))


def test_a_runner_without_an_origin_is_refused(tmp_path: Path, state: Path) -> None:
    root = tmp_path / "loose"
    make_repo(root)
    git(root, "remote", "remove", "origin")
    provision(root, state)
    with pytest.raises(RunnerError, match="origin"):
        open_runner(root, env(state))


# ---- a host process, never a child of an agent


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_an_agent_parent_is_refused(marker: str) -> None:
    with pytest.raises(RunnerError, match="never a child of an agent session"):
        refuse_agent_parent({marker: "1"})
    refuse_agent_parent({})


@pytest.mark.parametrize(
    "command",
    [
        "/opt/homebrew/bin/claude --resume",
        "claude",
        "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js",
        "/usr/local/bin/codex exec",
        "node /usr/local/lib/node_modules/@openai/codex/bin/codex.js",
    ],
)
def test_an_agent_client_among_the_parents_is_refused(command: str) -> None:
    chain = ["uv run tac runner serve", "just runner", "/bin/zsh -c x", command]
    with pytest.raises(RunnerError, match="among its parents"):
        refuse_agent_parent({}, ancestors=chain)


def test_an_owner_terminal_chain_is_accepted() -> None:
    chain = ["uv run tac runner serve", "just runner", "-zsh", "tmux", "login -pf"]
    assert agent_command(chain) is None
    refuse_agent_parent({}, ancestors=chain)


@pytest.mark.skipif(inside_sandbox(), reason="ps is setuid and cannot start in one")
def test_the_parent_chain_is_read_from_ps() -> None:
    chain = ancestor_commands(os.getpid())
    assert chain
    assert "pytest" in chain[0] or "python" in chain[0]


def test_init_refuses_inside_an_agent_session(repo: Path, state: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["runner", "init", "--repo", str(repo)],
        env={**env(state), "CLAUDECODE": "1"},
    )
    assert result.exit_code == 1
    assert "never a child of an agent session" in result.output


def test_init_writes_a_runner_pub_that_loads(repo: Path, state: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["runner", "init", "--repo", str(repo), "--write-pub"],
        env={**env(state), **NO_AGENT},
    )
    assert result.exit_code == 0, result.output
    public = load_public_key((repo / RUNNER_PUB).read_text("utf-8"))
    store = controller_store(repo, env(state))
    assert public.public_bytes_raw() == load_key(store).public_key().public_bytes_raw()


# ---- the socket


def test_ping_and_pubkey_answer(repo: Path, state: Path) -> None:
    runner = make_runner(repo, state)
    with serving(runner) as sock:
        assert request(sock, {"op": "ping"})["pong"] is True
        pem = request(sock, {"op": "pubkey"})["pem"]
    assert isinstance(pem, str)
    expected = runner.private.public_key().public_bytes_raw()
    assert load_public_key(pem).public_bytes_raw() == expected


def test_the_socket_is_private_and_removed_on_close(repo: Path, state: Path) -> None:
    runner = make_runner(repo, state)
    with serving(runner) as sock:
        assert stat.S_IMODE(sock.stat().st_mode) & 0o077 == 0
    assert not sock.exists()


def test_a_second_runner_on_the_same_store_is_refused(repo: Path, state: Path) -> None:
    runner = make_runner(repo, state)
    with serving(runner), pytest.raises(RunnerError, match="another runner"):
        bind(runner)


@seatbelt
def test_a_gate_is_observed_signed_and_kept(repo: Path, state: Path) -> None:
    runner = make_runner(repo, state)
    argv = ["just", "work-check", "order-a"]
    with serving(runner) as sock:
        gate = {"op": "gate", "run_id": "r1", "stage": "s1", "argv": argv}
        reply = request(sock, gate)
    signed = parse(json.dumps(reply["receipt"]))
    revision = git(repo, "rev-parse", "HEAD")
    expected = Binding(
        repository=REPOSITORY,
        revision=revision,
        run_id="r1",
        stage="s1",
        policy_hash=policy_hash(repo, revision),
    )
    receipt = verify(signed, runner.private.public_key(), expected)
    assert receipt.observed["exit"] == 3
    assert receipt.observed["argv"] == argv
    # The justfile is named, never searched for; the fake just prints its argv.
    ran = [
        "--justfile",
        str(repo.resolve() / "justfile"),
        "--working-directory",
        str(repo.resolve()),
        "work-check",
        "order-a",
    ]
    printed = "".join(f"{part}\n" for part in ran).encode("utf-8")
    assert (
        receipt.observed["stdout_sha256"]
        == "sha256:" + hashlib.sha256(printed).hexdigest()
    )
    kept = runner.store / "receipts" / "r1" / f"{receipt.receipt_id}.json"
    assert parse(kept.read_text("utf-8")) == signed


GATE = {"op": "gate", "run_id": "r1", "stage": "s1", "argv": ["just", "verify"]}
PROBE = {"op": "probe", "harness": "claude", "probe": "version", "run_id": "r"}
REFUSED = [
    # No op signs a payload it is handed.
    ({"op": "sign", "receipt": {"exit": 0}}, "refused request"),
    # A request names what to observe and can never carry the outcome.
    ({**GATE, "exit": 0}, "refused request"),
    ({**GATE, "argv": ["sh"]}, "not a gate"),
    ({**GATE, "argv": ["just"]}, "not a gate"),
    # Only `just <recipe> [ids]`: no just flag can choose what runs.
    ({**GATE, "argv": ["just", "--command", "sh", "-c", "id"]}, "not a gate recipe"),
    ({**GATE, "argv": ["just", "-f", "/tmp/justfile", "verify"]}, "not a gate recipe"),
    ({**GATE, "argv": ["just", "--justfile", "x", "verify"]}, "not a gate recipe"),
    ({**GATE, "argv": ["just", "--shell", "sh", "verify"]}, "not a gate recipe"),
    ({**GATE, "argv": ["just", "--set", "tac", "sh", "verify"]}, "not a gate recipe"),
    ({**GATE, "argv": ["just", "tac=sh", "verify"]}, "not a gate recipe"),
    ({**GATE, "argv": ["just", "stamp-lib"]}, "not a gate recipe"),
    ({**GATE, "argv": ["just", "verify", "extra"]}, "takes 0 argument"),
    ({**GATE, "argv": ["just", "work-check"]}, "takes 1 argument"),
    ({**GATE, "argv": ["just", "work-check", "x; id"]}, "not an id"),
    ({**GATE, "argv": ["just", "work-check", "--set"]}, "not an id"),
    ({**GATE, "argv": ["just", "work-check", "a=b"]}, "not an id"),
    ({**GATE, "run_id": "../x"}, "run_id"),
    ({**PROBE, "harness": "pi"}, "pi"),
    pytest.param({**PROBE, "probe": "keychain"}, "no probe", marks=seatbelt),
]


@pytest.mark.parametrize(("payload", "message"), REFUSED)
def test_the_runner_signs_only_what_it_observes(
    repo: Path, state: Path, payload: dict, message: str
) -> None:
    runner = make_runner(repo, state)
    with serving(runner) as sock, pytest.raises(RunnerError, match=message):
        request(sock, payload)
    assert not any((runner.store / "receipts").rglob("*.json"))
    assert not (repo.parent / "bin" / "just-argv").exists()


@seatbelt
def test_a_dirty_tree_is_not_observed(repo: Path, state: Path) -> None:
    runner = make_runner(repo, state)
    write(repo, "src/uncommitted.py", "pass\n")
    with serving(runner) as sock, pytest.raises(RunnerError, match="uncommitted"):
        request(sock, GATE)


@seatbelt
def test_a_probe_reads_the_committed_table_not_the_working_tree(
    repo: Path, state: Path
) -> None:
    clients = repo.parent / "bin"
    fake_client(clients, "claude", "echo '9.9.9 (Claude Code)'")
    runner = make_runner(repo, state, clients)
    write(repo, ".agents/config/probes.toml", "not = [toml\n")
    git(repo, "update-index", "--assume-unchanged", ".agents/config/probes.toml")
    probe = {"op": "probe", "harness": "claude", "probe": "version", "run_id": "p"}
    with serving(runner) as sock:
        reply = request(sock, probe)
    assert parse(json.dumps(reply["receipt"])).receipt.observed["matched"] is True


def trust_runner(repo: Path, state: Path, trusted: bool) -> Runner:
    """A runner whose Claude user config does or does not trust the checkout."""
    clients = repo.parent / "bin"
    fake_client(clients, "claude", "echo '9.9.9 (Claude Code)'")
    config = repo.parent / "claude-config"
    config.mkdir()
    entry = {"hasTrustDialogAccepted": trusted}
    projects = {str(repo.resolve()): entry}
    (config / ".claude.json").write_text(json.dumps({"projects": projects}))
    provision(repo, state)
    return open_runner(
        repo,
        {**env(state), "CLAUDE_CONFIG_DIR": str(config)},
        search_path=os.pathsep.join([str(clients), "/usr/bin", "/bin"]),
    )


@seatbelt
@pytest.mark.parametrize("trusted", [True, False])
def test_a_trust_probe_signs_what_the_client_config_says(
    repo: Path, state: Path, trusted: bool
) -> None:
    runner = trust_runner(repo, state, trusted)
    probe = {"op": "probe", "harness": "claude", "probe": "trust", "run_id": "t"}
    with serving(runner) as sock:
        reply = request(sock, probe)
    receipt = parse(json.dumps(reply["receipt"])).receipt
    assert receipt.binding.stage == "probe.trust"
    assert receipt.observed["expected"] == {"exit": 0, "trusted": True}
    observed = receipt.observed["observed"]
    assert isinstance(observed, dict) and observed["trusted"] is trusted
    assert receipt.observed["client_version"] == "9.9.9 (Claude Code)"
    assert receipt.observed["matched"] is trusted
    # A shareable receipt names no absolute path.
    assert str(repo.parent) not in json.dumps(reply["receipt"])


@seatbelt
def test_a_trust_probe_never_reads_the_callers_environment(
    repo: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = trust_runner(repo, state, True)
    elsewhere = repo.parent / "elsewhere"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))
    probe = {"op": "probe", "harness": "claude", "probe": "trust", "run_id": "t"}
    with serving(runner) as sock:
        reply = request(sock, probe)
    assert parse(json.dumps(reply["receipt"])).receipt.observed["matched"] is True


# ---- tac receipt client


def client(repo: Path, sock: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        cli,
        ["receipt", "client", "--repo", str(repo), "--socket", str(sock), *args],
    )
    return result.exit_code, result.output


@seatbelt
def test_receipt_client_writes_a_signed_probe_into_the_order(
    repo: Path, state: Path
) -> None:
    clients = repo.parent / "bin"
    fake_client(clients, "claude", "echo '9.9.9 (Claude Code)'")
    runner = make_runner(repo, state, clients)
    with serving(runner) as sock:
        code, output = client(
            repo, sock, "--harness", "claude", "--probe", "version", "--order", "demo"
        )
    assert code == 0, output
    [copy] = (repo / "work/orders/demo/receipts").glob("*.json")
    receipt = parse(copy.read_text("utf-8")).receipt
    assert copy.stem == receipt.receipt_id
    assert receipt.kind == "probe"
    assert receipt.binding.stage == "probe.version"
    assert receipt.binding.order_id == "demo"
    assert receipt.observed["client_version"] == "9.9.9 (Claude Code)"
    for name in ("requested_model", "actual_model", "billing_route", "exit"):
        assert name in receipt.observed


@seatbelt
def test_receipt_client_exits_non_zero_when_observed_differs(
    repo: Path, state: Path
) -> None:
    clients = repo.parent / "bin"
    fake_client(clients, "codex", "echo 'broken' >&2; exit 4")
    runner = make_runner(repo, state, clients)
    with serving(runner) as sock:
        code, output = client(repo, sock, "--harness", "codex", "--probe", "version")
    assert code == 1
    body = json.loads(output)
    assert body["receipt"]["observed"]["exit"] == 4
    assert body["receipt"]["observed"]["missing"] == ["client_version"]


def test_receipt_client_without_a_runner_fails(repo: Path, state: Path) -> None:
    code, output = client(
        repo, state / "absent.sock", "--harness", "claude", "--probe", "version"
    )
    assert code == 1
    assert "no runner is listening" in output


def test_receipt_client_refuses_a_bad_order_id(repo: Path, state: Path) -> None:
    code, output = client(
        repo, state / "x.sock", "--harness", "claude", "--probe", "v", "--order", "../x"
    )
    assert code == 1
    assert "not an order id" in output


@seatbelt
def test_a_new_commit_makes_an_old_probe_receipt_stale(repo: Path, state: Path) -> None:
    clients = repo.parent / "bin"
    fake_client(clients, "claude", "echo 1.0")
    runner = make_runner(repo, state, clients)
    probe = {"op": "probe", "harness": "claude", "probe": "version", "run_id": "p"}
    with serving(runner) as sock:
        signed = parse(json.dumps(request(sock, probe)["receipt"]))
    old = signed.receipt.binding
    write(repo, "src/next.py", "pass\n")
    revision = commit_all(repo, "next")
    now = old.model_copy(update={"revision": revision})
    with pytest.raises(ReceiptError, match="revision"):
        verify(signed, runner.private.public_key(), now)


# ---- the gate child: candidate code never reaches the store


def with_recipe(repo: Path, body: str) -> None:
    """Commit a justfile whose work-check recipe is candidate code."""
    write(repo, "justfile", f"work-check id:\n    {body}\n")
    commit_all(repo, "candidate justfile")


def real_just_runner(repo: Path, state: Path, environ: dict[str, str]) -> Runner:
    provision(repo, state)
    found = shutil.which("just")
    assert found is not None
    search = os.pathsep.join([str(Path(found).parent), "/usr/bin", "/bin"])
    return open_runner(repo, {**env(state), **environ}, search_path=search)


def gate_exit(runner: Runner, order: str = "order-a") -> int:
    argv = ["just", "work-check", order]
    with serving(runner) as sock:
        reply = request(sock, {"op": "gate", "run_id": "g", "stage": "s", "argv": argv})
    exit_code = parse(json.dumps(reply["receipt"])).receipt.observed["exit"]
    assert isinstance(exit_code, int)
    return exit_code


def key_state(store: Path) -> tuple[str, int]:
    path = key_path(store)
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode


needs_just = pytest.mark.skipif(shutil.which("just") is None, reason="needs just")


@seatbelt
@needs_just
def test_a_gate_criterion_cannot_read_the_signing_key(
    repo: Path, state: Path, tmp_path: Path
) -> None:
    store = controller_store(repo, env(state))
    leak = tmp_path / "leak"
    key = key_path(store)
    with_recipe(repo, f"cat '{key}' > '{leak}'")
    runner = real_just_runner(repo, state, {})
    before = key_state(runner.store)
    assert gate_exit(runner) != 0
    assert not leak.exists() or b"PRIVATE KEY" not in leak.read_bytes()
    assert key_state(runner.store) == before


@seatbelt
@needs_just
def test_a_gate_cannot_move_the_store_out_of_the_way(
    repo: Path, state: Path, tmp_path: Path
) -> None:
    store = controller_store(repo, env(state))
    moved = state.parent / (state.name + "-moved")
    leak = tmp_path / "leak"
    inner = moved / store.relative_to(state) / "signing-key.pem"
    with_recipe(repo, f"mv '{state}' '{moved}' && cat '{inner}' > '{leak}'")
    runner = real_just_runner(repo, state, {})
    assert gate_exit(runner) != 0
    assert not moved.exists()
    assert not leak.exists() or b"PRIVATE KEY" not in leak.read_bytes()


@seatbelt
@needs_just
def test_a_gate_cannot_write_into_the_store(repo: Path, state: Path) -> None:
    store = controller_store(repo, env(state))
    forged = store / "receipts" / "forged.json"
    key = key_path(store)
    with_recipe(
        repo, f"echo x >> '{key}'; echo '{{}}' > '{forged}'; test -e '{forged}'"
    )
    runner = real_just_runner(repo, state, {})
    before = key_state(runner.store)
    assert gate_exit(runner) != 0
    assert not forged.exists()
    assert key_state(runner.store) == before


@seatbelt
@needs_just
def test_a_gate_cannot_reach_the_runner_socket(repo: Path, state: Path) -> None:
    sock = socket_path(controller_store(repo, env(state)))
    code = f"import socket; socket.socket(socket.AF_UNIX).connect({str(sock)!r})"
    with_recipe(repo, f"'{PYTHON}' -c \"{code}\"")
    runner = real_just_runner(repo, state, {})
    assert gate_exit(runner) != 0


@contextlib.contextmanager
def listening(family: socket.AddressFamily, address: str | tuple[str, int]):
    """A server socket this test owns, never one of the owner's own services."""
    server = socket.socket(family)
    try:
        server.bind(address)
        server.listen(4)
        server.setblocking(False)
        yield server
    finally:
        server.close()


def connected_to(server: socket.socket) -> bool:
    """Whether a client got through: a completed connect waits in the backlog."""
    try:
        conn, _ = server.accept()
    except BlockingIOError:
        return False
    conn.close()
    return True


@seatbelt
@needs_just
def test_a_gate_cannot_reach_a_unix_socket_outside_its_scratch(
    repo: Path, state: Path
) -> None:
    # Stands in for the owner's tmux or Docker socket: a process outside the
    # sandbox that would run a command for whoever connects.
    with short_dir() as outside, listening(socket.AF_UNIX, str(outside / "s")) as srv:
        path = str(outside / "s")
        code = f"import socket; socket.socket(socket.AF_UNIX).connect({path!r})"
        with_recipe(repo, f"'{PYTHON}' -c \"{code}\"")
        runner = real_just_runner(repo, state, {})
        assert gate_exit(runner) != 0
        assert not connected_to(srv)


@seatbelt
@needs_just
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_a_gate_cannot_reach_a_loopback_port(
    repo: Path, state: Path, host: str
) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with listening(family, (host, 0)) as srv:
        port = srv.getsockname()[1]
        code = f"import socket; socket.create_connection(({host!r}, {port}), 5)"
        with_recipe(repo, f"'{PYTHON}' -c \"{code}\"")
        runner = real_just_runner(repo, state, {})
        assert gate_exit(runner) != 0
        assert not connected_to(srv)


@seatbelt
@needs_just
def test_a_gate_may_serve_and_reach_a_unix_socket_in_its_scratch(
    repo: Path, state: Path
) -> None:
    # A test a gate runs may bind a socket under its TMPDIR and connect to it.
    code = (
        "import os, socket; p = os.path.join(os.environ['TMPDIR'], 's'); "
        "a = socket.socket(socket.AF_UNIX); a.bind(p); a.listen(1); "
        "socket.socket(socket.AF_UNIX).connect(p)"
    )
    with_recipe(repo, f"'{PYTHON}' -c \"{code}\"")
    runner = real_just_runner(repo, state, {})
    assert gate_exit(runner) == 0


@seatbelt
@needs_just
def test_the_gate_environment_carries_no_tokens(
    repo: Path, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # In the runner's own environment as well as the one it was opened with.
    monkeypatch.setenv("GH_TOKEN", "t1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "t3")
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()
    # Written inside the checkout, the one place outside its scratch a gate
    # writes; ignored, so the tree stays clean and the gate gets its receipt.
    write(repo, ".gitignore", "seen-env\n")
    with_recipe(repo, "env > seen-env")
    tokens = {
        "GH_TOKEN": "t1",
        "GITHUB_TOKEN": "t2",
        "ANTHROPIC_API_KEY": "t3",
        "OPENAI_API_KEY": "t4",
        "TAC_BOT_TOKEN": "t5",
        "GIT_DIR": "/elsewhere",
        "HOME": str(owner_home),
        "TMPDIR": str(tmp_path),
        "LANG": "C.UTF-8",
    }
    runner = real_just_runner(repo, state, tokens)
    assert gate_exit(runner) == 0
    text = (repo / "seen-env").read_text()
    seen = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert name not in seen
    assert "TAC_BOT_TOKEN" not in seen
    assert "TAC_STATE_HOME" not in seen
    assert "GIT_DIR" not in seen
    assert {"HOME", "TMPDIR", "UV_CACHE_DIR", "LANG", "PATH"} <= seen.keys()
    for value in ("t1", "t2", "t3", "t4", "t5"):
        assert f"={value}" not in text
    # The owner's HOME and TMPDIR are not the child's: it gets a scratch folder,
    # removed once the gate is over.
    scratch = Path(seen["HOME"]).parent
    assert scratch.name.startswith("tac-gate-")
    assert Path(seen["TMPDIR"]).parent == scratch
    assert Path(seen["UV_CACHE_DIR"]).is_relative_to(scratch)
    assert str(owner_home) not in text
    assert not scratch.exists()


def outside_targets(repo: Path, owner_home: Path) -> dict[str, Path]:
    """Places a gate child must never write: each lets code run later as the
    owner outside any sandbox, or changes the judge."""
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return {
        "owner claude config": owner_home / ".claude" / "x",
        "owner launch agents": owner_home / "Library" / "LaunchAgents" / "x",
        "owner uv cache": owner_home / ".cache" / "uv" / "x",
        "judge venv": repo / ".agents" / ".venv" / "x",
        "stamped toolchain": repo / ".agents" / "lib" / "x",
        "harness config": repo / ".claude" / "x",
        "shared git hooks": common / "hooks" / "x",
        "git config": common / "config",
    }


def prepare_targets(repo: Path, targets: dict[str, Path]) -> dict[Path, bytes | None]:
    """Create each target's folder, and commit an ignore for the judge venv."""
    write(repo, ".gitignore", ".agents/.venv/\n.claude/\n")
    commit_all(repo, "ignore the venv")
    before: dict[Path, bytes | None] = {}
    for path in targets.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        before[path] = path.read_bytes() if path.exists() else None
    return before


@seatbelt
@needs_just
@pytest.mark.parametrize("worktree", [False, True], ids=["main-clone", "worktree"])
@pytest.mark.parametrize(
    "target",
    [
        "owner claude config",
        "owner launch agents",
        "owner uv cache",
        "judge venv",
        "stamped toolchain",
        "harness config",
        "shared git hooks",
        "git config",
    ],
)
def test_a_gate_writes_nothing_outside_the_checkout_and_its_scratch(
    repo: Path, state: Path, tmp_path: Path, target: str, worktree: bool
) -> None:
    owner_home = tmp_path / "owner-home"
    root = repo
    if worktree:
        root = tmp_path / "linked"
        git(repo, "worktree", "add", "-q", "-b", "linked", str(root))
    targets = outside_targets(root, owner_home)
    before = prepare_targets(root, targets)
    path = targets[target]
    # By absolute path, by the child's own view of git, and through a link
    # planted in the checkout.
    link = root / "planted"
    write(root, ".gitignore", ".agents/.venv/\n.claude/\nplanted\n")
    with_recipe(
        root,
        f"ln -s '{path}' planted; echo x >> planted; echo x >> '{path}'; "
        'echo x >> "$(git rev-parse --path-format=absolute --git-common-dir)'
        '/hooks/x"; test "$(cat planted)" = x',
    )
    runner = real_just_runner(root, state, {"HOME": str(owner_home)})
    assert gate_exit(runner) != 0
    link.unlink(missing_ok=True)
    for each, content in before.items():
        now = each.read_bytes() if each.exists() else None
        assert now == content, f"the gate wrote {each}"


@seatbelt
@needs_just
def test_a_gate_that_changes_the_checkout_gets_no_receipt(
    repo: Path, state: Path
) -> None:
    with_recipe(repo, "echo x > stray.txt")
    runner = real_just_runner(repo, state, {})
    argv = ["just", "work-check", "order-a"]
    gate = {"op": "gate", "run_id": "g", "stage": "s", "argv": argv}
    with serving(runner) as sock, pytest.raises(RunnerError, match="changed"):
        request(sock, gate)
    assert not any((runner.store / "receipts").rglob("*.json"))


@contextlib.contextmanager
def kept_scratch(parent: Path) -> Iterator[Path]:
    """The runner's scratch folder, kept afterwards so a test can look inside."""
    folder = Path(tempfile.mkdtemp(prefix="tac-gate-", dir=parent)).resolve()
    for name in ("home", "tmp", "cache"):
        (folder / name).mkdir()
    yield folder


@seatbelt
def test_the_runners_git_status_runs_sandboxed(
    repo: Path, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A clean filter in the repository's config is candidate code: git status
    # may start it, so the runner starts git status inside the sandbox too. The
    # filter marks that it ran and tries to copy the key into the child's own
    # TMPDIR, which the sandbox lets it write.
    store = controller_store(repo, env(state))
    kept = tmp_path / "kept"
    kept.mkdir()
    monkeypatch.setattr(Runner, "scratch", lambda _self: kept_scratch(kept))
    write(repo, ".gitattributes", "*.txt filter=grab\n")
    write(repo, "a.txt", "a\n")
    commit_all(repo, "attributes")
    grab = (
        f'/usr/bin/touch "$TMPDIR/ran"; '
        f"/bin/cat '{key_path(store)}' > \"$TMPDIR/leak\"; /bin/cat"
    )
    git(repo, "config", "filter.grab.clean", grab)
    later = (repo / "a.txt").stat().st_mtime + 60
    os.utime(repo / "a.txt", (later, later))
    runner = make_runner(repo, state)
    with contextlib.suppress(RunnerError):
        runner.clean_revision()
    assert list(kept.glob("tac-gate-*/tmp/ran")), "the filter never ran"
    for leak in kept.glob("tac-gate-*/tmp/leak"):
        assert b"PRIVATE KEY" not in leak.read_bytes()


@seatbelt
def test_the_runners_git_status_cannot_write_the_tree(repo: Path, state: Path) -> None:
    write(repo, ".gitattributes", "*.txt filter=plant\n")
    write(repo, "a.txt", "a\n")
    commit_all(repo, "attributes")
    planted = repo / "planted.txt"
    git(repo, "config", "filter.plant.clean", f"echo x > '{planted}'; /bin/cat")
    later = (repo / "a.txt").stat().st_mtime + 60
    os.utime(repo / "a.txt", (later, later))
    runner = make_runner(repo, state)
    with contextlib.suppress(RunnerError):
        runner.clean_revision()
    assert not planted.exists()


def test_without_a_sandbox_the_runner_refuses_to_gate(repo: Path, state: Path) -> None:
    runner = dataclasses.replace(make_runner(repo, state), sandbox=None)
    with serving(runner) as sock, pytest.raises(RunnerError, match="refuses to gate"):
        request(sock, GATE)
    assert not any((runner.store / "receipts").rglob("*.json"))
    assert not (repo.parent / "bin" / "just-argv").exists()


def test_off_macos_there_is_no_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tac.runner.sys.platform", "linux")
    assert default_sandbox() is None


def test_inside_a_sandbox_there_is_none_to_give(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Seatbelt profile cannot nest: a runner already sandboxed must refuse.
    monkeypatch.setattr("tac.runner.inside_sandbox", lambda: True)
    assert default_sandbox() is None


@seatbelt
def test_a_sandboxed_process_knows_it_is_sandboxed() -> None:
    code = "from tac.runner import inside_sandbox; print(inside_sandbox())"
    profile = "(version 1)(allow default)"
    done = subprocess.run(
        [SANDBOX_EXEC, "-p", profile, str(PYTHON), "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == "True"
    assert inside_sandbox() is False


def test_a_linked_signing_key_is_refused(repo: Path, state: Path) -> None:
    store = provision(repo, state)
    real = state / "elsewhere.pem"
    key_path(store).rename(real)
    key_path(store).symlink_to(real)
    with pytest.raises(RunnerError, match="not a link"):
        load_key(store)


# ---- the runner's own venv


def test_install_builds_the_store_venv_non_editable(repo: Path, state: Path) -> None:
    write(repo, ".agents/pyproject.toml", "[project]\nname = 'x'\n")
    store = ensure_store(controller_store(repo, env(state)))
    bin_dir = fake_client(state / "bin", "uv", "exit 0").parent
    argv, run_env = install_command(
        repo, store, {"PATH": str(bin_dir), "VIRTUAL_ENV": "/elsewhere"}
    )
    assert argv[1:] == [
        "sync", "--frozen", "--no-editable", "--project", str(repo / ".agents"),
    ]  # fmt: skip
    assert run_env["UV_PROJECT_ENVIRONMENT"] == str(runner_venv(store))
    assert "VIRTUAL_ENV" not in run_env
    assert not runner_venv(store).is_relative_to(repo)


def test_install_without_the_agents_project_is_refused(repo: Path, state: Path) -> None:
    store = ensure_store(controller_store(repo, env(state)))
    bin_dir = fake_client(state / "bin", "uv", "exit 0").parent
    with pytest.raises(RunnerError, match=r"pyproject\.toml"):
        install_command(repo, store, {"PATH": str(bin_dir)})


def test_runner_install_runs_uv_into_the_store(repo: Path, state: Path) -> None:
    write(repo, ".agents/pyproject.toml", "[project]\nname = 'x'\n")
    record = state / "uv-called"
    bin_dir = fake_client(
        state / "bin", "uv", f'echo "$UV_PROJECT_ENVIRONMENT $*" > {record}'
    ).parent
    result = CliRunner().invoke(
        cli,
        ["runner", "install", "--repo", str(repo)],
        env={**env(state), **NO_AGENT, "PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert result.exit_code == 0, result.output
    venv = runner_venv(controller_store(repo, env(state)))
    assert record.read_text().startswith(f"{venv} sync --frozen --no-editable")


def test_install_refuses_inside_an_agent_session(repo: Path, state: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["runner", "install", "--repo", str(repo)],
        env={**env(state), "CODEX_SANDBOX": "seatbelt"},
    )
    assert result.exit_code == 1
    assert "never a child of an agent session" in result.output


def test_serving_needs_tac_from_the_store_venv(repo: Path, state: Path) -> None:
    store = provision(repo, state)
    site = runner_venv(store) / "lib" / "python3.12" / "site-packages" / "tac"
    require_own_venv(store, site / "__init__.py")
    with pytest.raises(RunnerError, match="not from its own venv"):
        require_own_venv(store, repo / "src" / "tac" / "__init__.py")
    with pytest.raises(RunnerError, match="not from its own venv"):
        require_own_venv(store, repo / ".agents" / ".venv" / "site-packages" / "x.py")


def test_serve_refuses_from_a_checkout_environment(repo: Path, state: Path) -> None:
    provision(repo, state)
    result = CliRunner().invoke(
        cli,
        ["runner", "serve", "--repo", str(repo)],
        env={**env(state), **NO_AGENT},
    )
    assert result.exit_code == 1
    assert "not from its own venv" in result.output
    assert not socket_path(controller_store(repo, env(state))).exists()
