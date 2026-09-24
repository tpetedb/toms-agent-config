"""The runner skeleton: its store, its key, its socket, and what it will sign."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
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
    Runner,
    RunnerError,
    agent_command,
    ancestor_commands,
    bind,
    controller_store,
    create_key,
    ensure_store,
    install_command,
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


def make_runner(repo: Path, state: Path, clients: Path | None = None) -> Runner:
    provision(repo, state)
    search = [str(PYTHON.parent)] + ([str(clients)] if clients else [])
    return open_runner(
        repo,
        env(state),
        programs=(PYTHON.name,),
        search_path=os.pathsep.join(search),
    )


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


def test_a_gate_is_observed_signed_and_kept(repo: Path, state: Path) -> None:
    runner = make_runner(repo, state)
    argv = [PYTHON.name, "-c", "raise SystemExit(3)"]
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
    kept = runner.store / "receipts" / "r1" / f"{receipt.receipt_id}.json"
    assert parse(kept.read_text("utf-8")) == signed


GATE = {"op": "gate", "run_id": "r1", "stage": "s1", "argv": ["x"]}
PROBE = {"op": "probe", "harness": "claude", "probe": "version", "run_id": "r"}
REFUSED = [
    # No op signs a payload it is handed.
    ({"op": "sign", "receipt": {"exit": 0}}, "refused request"),
    # A request names what to observe and can never carry the outcome.
    ({**GATE, "exit": 0}, "refused request"),
    ({**GATE, "argv": ["sh"]}, "not a gate"),
    ({**GATE, "run_id": "../x"}, "run_id"),
    ({**PROBE, "harness": "pi"}, "pi"),
    ({**PROBE, "probe": "trust"}, "no probe"),
]


@pytest.mark.parametrize(("payload", "message"), REFUSED)
def test_the_runner_signs_only_what_it_observes(
    repo: Path, state: Path, payload: dict, message: str
) -> None:
    runner = make_runner(repo, state)
    with serving(runner) as sock, pytest.raises(RunnerError, match=message):
        request(sock, payload)
    assert not any((runner.store / "receipts").rglob("*.json"))


def test_a_dirty_tree_is_not_observed(repo: Path, state: Path) -> None:
    runner = make_runner(repo, state)
    write(repo, "src/uncommitted.py", "pass\n")
    gate = {"op": "gate", "run_id": "r1", "stage": "s1", "argv": [PYTHON.name]}
    with serving(runner) as sock, pytest.raises(RunnerError, match="uncommitted"):
        request(sock, gate)


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


# ---- tac receipt client


def client(repo: Path, sock: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        cli,
        ["receipt", "client", "--repo", str(repo), "--socket", str(sock), *args],
    )
    return result.exit_code, result.output


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
    assert receipt.observed["client_version"] == "9.9.9 (Claude Code)"
    for name in ("requested_model", "actual_model", "billing_route", "exit"):
        assert name in receipt.observed


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
