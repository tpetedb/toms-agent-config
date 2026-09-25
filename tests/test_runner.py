"""The trusted runner of M3: its keychain, tokens, leases, effects, inbox,
usage reading and the `tac run` state machine (design sections 4, 5 and 10;
build conditions C1 and C6).

The keychain is always a fake `security` under the test's folder: the owner's
real login keychain is never read or written. Gates are stubbed where the
runner's Seatbelt sandbox cannot nest; git runs against a local bare remote,
`gh`, `claude` and `codex` are fakes, and nothing reaches the network.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tac import approvals, human, inbox, leases, run, usage
from tac.cli import cli
from tac.config import load_config
from tac.effects import EffectError, EffectRequest, Effects, action
from tac.keychain import (
    BOT_TOKEN,
    KEYCHAIN_ENTRIES,
    SIGNING_KEY,
    Entry,
    Keychain,
    KeychainError,
    system_keychain,
)
from tac.receipts import Binding, policy_hash, resolve, verify
from tac.runner import (
    Gate,
    Runner,
    RunnerError,
    TokenConsume,
    answer,
    controller_store,
    create_key,
    default_sandbox,
    ensure_store,
    key_path,
    load_key,
    open_runner,
    private_hex,
)
from tests import _fakeclients as fakes
from tests._gitrepo import git, make_repo, short_dir, write

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "runner"
TOKEN = "ghp_" + "a1" * 18
NO_AGENT = {"CLAUDECODE": None, "CODEX_SANDBOX": None}


@pytest.fixture(autouse=True)
def no_agent_ancestors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tac.runner.ancestor_commands", lambda _pid: [])


@pytest.fixture
def state() -> Iterator[Path]:
    with short_dir() as path:
        yield path


def env(state: Path) -> dict[str, str]:
    return {"TAC_STATE_HOME": str(state)}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    make_repo(root)
    return root


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    return fakes.install(tmp_path / "bin", "security", "gh", "claude", "codex")


def keychain(bin_dir: Path, slug: str = "demo-0123456789") -> Keychain:
    return Keychain(slug, str(bin_dir / "security"))


# ---------------------------------------------------------------- the keychain (C6)


def test_the_runner_names_exactly_two_keychain_entries() -> None:
    assert len(KEYCHAIN_ENTRIES) == 2
    assert set(KEYCHAIN_ENTRIES) == {SIGNING_KEY, BOT_TOKEN}
    assert all("owner" not in e.service for e in KEYCHAIN_ENTRIES)


@pytest.mark.parametrize("method", ["read", "exists", "write"])
def test_a_third_keychain_name_is_refused_before_any_process_starts(
    tmp_path: Path, method: str
) -> None:
    trap = fakes.never(tmp_path / "trap", "security")
    chain = Keychain("demo-0123456789", str(trap))
    owner = Entry("gh-owner-token", "owner", "the owner's own token")
    with pytest.raises(KeychainError, match="two keychain entries"):
        if method == "write":
            chain.write(owner, TOKEN)
        else:
            getattr(chain, method)(owner)
    assert not (trap.parent / "called").exists()


def test_security_is_named_by_absolute_path() -> None:
    with pytest.raises(KeychainError, match="absolute path"):
        Keychain("demo-0123456789", "security")


def test_the_keychain_reads_writes_and_keeps_the_secret_off_the_command_line(
    bin_dir: Path,
) -> None:
    chain = keychain(bin_dir)
    assert not chain.exists(BOT_TOKEN)
    with pytest.raises(KeychainError, match="no keychain item"):
        chain.read(BOT_TOKEN)
    chain.write(BOT_TOKEN, TOKEN)
    assert chain.exists(BOT_TOKEN)
    assert chain.read(BOT_TOKEN) == TOKEN
    assert chain.service(BOT_TOKEN) == "tac-bot-token.demo-0123456789"
    assert all(TOKEN not in " ".join(c["argv"]) for c in fakes.calls(bin_dir))


def test_a_secret_that_would_need_quoting_is_refused(bin_dir: Path) -> None:
    with pytest.raises(KeychainError, match="characters"):
        keychain(bin_dir).write(BOT_TOKEN, "has a space and ; semicolon")


def test_a_test_run_never_reaches_the_real_keychain() -> None:
    assert system_keychain("demo-0123456789") is None


def test_the_runner_loads_its_key_from_the_keychain_first(
    repo: Path, state: Path, bin_dir: Path
) -> None:
    store = ensure_store(controller_store(repo, env(state)))
    private = create_key(store)
    chain = keychain(bin_dir, store.name)
    chain.write(SIGNING_KEY, private_hex(private))
    key_path(store).unlink()
    warned: list[str] = []
    runner = open_runner(repo, env(state), keychain=chain, warn=warned.append)
    assert private_hex(runner.private) == private_hex(private)
    assert warned == []


def test_a_key_file_still_loads_with_a_warning_naming_the_step(
    repo: Path, state: Path, bin_dir: Path
) -> None:
    store = ensure_store(controller_store(repo, env(state)))
    private = create_key(store)
    warned: list[str] = []
    found = load_key(store, keychain(bin_dir, store.name), warned.append)
    assert private_hex(found) == private_hex(private)
    [message] = warned
    assert "just runner-keychain" in message


def keychain_import(
    repo: Path, state: Path, bin_dir: Path, extra: dict[str, str | None] | None = None
) -> Any:
    return CliRunner().invoke(
        cli,
        [
            "runner",
            "keychain",
            "import",
            "--repo",
            str(repo),
            "--security",
            str(bin_dir / "security"),
            "--token-stdin",
        ],
        input=TOKEN + "\n",
        env={**env(state), **NO_AGENT, **(extra or {})},
    )


def test_keychain_import_moves_the_key_and_stores_the_token(
    repo: Path, state: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tac.runner_cli.inside_sandbox", lambda: False)
    store = ensure_store(controller_store(repo, env(state)))
    private = create_key(store)
    done = keychain_import(repo, state, bin_dir)
    assert done.exit_code == 0, done.output
    assert not key_path(store).exists()
    chain = keychain(bin_dir, store.name)
    assert chain.read(SIGNING_KEY) == private_hex(private)
    assert chain.read(BOT_TOKEN) == TOKEN
    assert TOKEN not in done.output
    # The runner now signs with the key from the keychain, and warns no more.
    warned: list[str] = []
    runner = open_runner(repo, env(state), keychain=chain, warn=warned.append)
    assert private_hex(runner.private) == private_hex(private) and warned == []


def test_keychain_import_refuses_an_agent_session_and_a_sandbox(
    repo: Path, state: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ensure_store(controller_store(repo, env(state)))
    create_key(store)
    monkeypatch.setattr("tac.runner_cli.inside_sandbox", lambda: False)
    done = keychain_import(repo, state, bin_dir, {"CLAUDECODE": "1"})
    assert done.exit_code == 1 and "agent session" in done.output
    monkeypatch.setattr("tac.runner_cli.inside_sandbox", lambda: True)
    done = keychain_import(repo, state, bin_dir)
    assert done.exit_code == 1 and "sandbox" in done.output
    assert key_path(store).exists()
    assert fakes.calls(bin_dir) == []


@pytest.mark.parametrize(
    ("given", "answered"),
    [
        ("protocol=https\nhost=github.com\n\n", True),
        ("protocol=https\nhost=example.com\n\n", False),
        ("protocol=http\nhost=github.com\n\n", False),
    ],
)
def test_the_credential_helper_answers_github_only(
    bin_dir: Path, given: str, answered: bool
) -> None:
    keychain(bin_dir).write(BOT_TOKEN, TOKEN)
    done = CliRunner().invoke(
        cli,
        [
            "runner",
            "credential",
            "--slug",
            "demo-0123456789",
            "--security",
            str(bin_dir / "security"),
            "get",
        ],
        input=given,
    )
    assert done.exit_code == 0
    assert (f"password={TOKEN}" in done.output) is answered


# ---------------------------------------------------------------- dispatch tokens (C1)


@pytest.fixture
def runner(repo: Path, state: Path) -> Runner:
    store = ensure_store(controller_store(repo, env(state)))
    create_key(store)
    return open_runner(repo, env(state))


def consume(runner: Runner, token: str, **over: str) -> dict[str, Any]:
    body = {
        "op": "token.consume",
        "token": token,
        "run_id": "r1",
        "stage": "build",
        "sha256": hashlib.sha256(b"prompt").hexdigest(),
        "kind": "agent",
        "session_id": "s-1",
        **over,
    }
    return answer(runner, json.dumps(body).encode())


def test_a_token_is_bound_and_spent_once(runner: Runner) -> None:
    digest = hashlib.sha256(b"prompt").hexdigest()
    token = runner.issue_token("r1", "build", digest, "agent")
    assert consume(runner, token)["ok"] is True
    again = consume(runner, token)
    assert again["ok"] is False and "already used" in again["error"]
    # The store keeps the token's hash, never the token.
    stored = " ".join(p.read_text() for p in (runner.store / "tokens").rglob("*.json"))
    assert token not in stored


@pytest.mark.parametrize(
    "over",
    [
        {"run_id": "r2"},
        {"stage": "review"},
        {"sha256": hashlib.sha256(b"other").hexdigest()},
        {"kind": "workflow"},
    ],
)
def test_a_wrong_binding_is_refused_and_burns_the_token(
    runner: Runner, over: dict[str, str]
) -> None:
    token = runner.issue_token(
        "r1", "build", hashlib.sha256(b"prompt").hexdigest(), "agent"
    )
    refused = consume(runner, token, **over)
    assert refused["ok"] is False and "burned" in refused["error"]
    assert consume(runner, token)["ok"] is False


def test_a_token_is_never_issued_over_the_socket(runner: Runner) -> None:
    body = {"op": "token.issue", "run_id": "r1", "stage": "s", "sha256": "0" * 64}
    reply = answer(runner, json.dumps(body).encode())
    assert reply["ok"] is False and "run loop" in str(reply["error"])


def test_a_malformed_consume_request_is_refused(runner: Runner) -> None:
    with pytest.raises(ValueError):
        TokenConsume.model_validate({"op": "token.consume", "token": "short"})


# ---------------------------------------------------------------- leases


def seat(state: Path, budget: int, pid: int | None = None) -> leases.Lease:
    return leases.acquire(
        env(state),
        budget,
        repository="example/demo",
        run_id="r",
        stage="s",
        role="builder",
        pid=pid,
    )


def test_leases_count_across_the_host_and_reclaim_the_dead(state: Path) -> None:
    first = seat(state, 2)
    second = seat(state, 2)
    with pytest.raises(RunnerError, match="max_local_agents is 2"):
        seat(state, 2)
    leases.release(second)
    third = seat(state, 2)
    # A lease whose process is gone is reclaimed and its file removed.
    dead = seat(state, 3, pid=2**22 + 12345)
    fourth = seat(state, 3)
    assert not dead.path.exists()
    assert first.path.parent == state / "leases"
    for lease in (first, third, fourth):
        leases.release(lease)


# ---------------------------------------------------------------- effects


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return fakes.project_repo(tmp_path)


@pytest.fixture
def worktree(project: Path, tmp_path: Path) -> Path:
    return fakes.order_worktree(project, tmp_path)


def project_runner(project: Path, state: Path, bin_dir: Path) -> Runner:
    store = ensure_store(controller_store(project, env(state)))
    create_key(store)
    search = os.pathsep.join(
        [str(bin_dir), str(Path(sys.executable).parent), "/usr/bin", "/bin"]
    )
    return open_runner(project, env(state), search_path=search)


@pytest.fixture
def effects(project: Path, state: Path, bin_dir: Path) -> Effects:
    found = project_runner(project, state, bin_dir)
    chain = keychain(bin_dir, found.store.name)
    chain.write(BOT_TOKEN, TOKEN)
    return Effects(found, load_config(project), chain)


def request(effect: str, arguments: dict[str, Any], **more: Any) -> EffectRequest:
    return EffectRequest(
        effect=effect, arguments=arguments, run_id="r1", stage="publish", **more
    )


def test_a_commit_is_journaled_signed_and_performed_once(
    effects: Effects, worktree: Path, project: Path
) -> None:
    write(worktree, "docs/intro.md", "# Intro\n")
    commit = request("commit", {"branch": fakes.ORDER_BRANCH, "message": "Add intro"})
    observed = effects.perform(commit)
    assert observed["committed"] is True
    sha = git(worktree, "rev-parse", "HEAD")
    assert observed["sha"] == sha
    assert git(worktree, "log", "-1", "--format=%an") == "tac-bot"
    assert f"Tac-Effect: {effects.key(commit)}" in git(
        worktree, "log", "-1", "--format=%B"
    )
    entry = effects.entry(effects.key(commit))
    assert entry is not None and entry.state == "done"
    receipt = (
        effects.runner.store / "receipts" / "r1" / f"{observed['receipt_id']}.json"
    )
    signed = json.loads(receipt.read_text())
    assert signed["receipt"]["kind"] == "effect"
    # Asked again, the journal answers; no second commit.
    assert effects.perform(commit)["sha"] == sha
    assert git(worktree, "rev-list", "--count", "HEAD") == "2"


def test_a_push_goes_to_origin_over_the_runner_s_git(
    effects: Effects, worktree: Path, project: Path
) -> None:
    write(worktree, "docs/intro.md", "# Intro\n")
    sha = effects.perform(
        request("commit", {"branch": fakes.ORDER_BRANCH, "message": "Add intro"})
    )["sha"]
    push = request("push", {"branch": fakes.ORDER_BRANCH, "sha": sha})
    effects.perform(push)
    remote = git(project, "ls-remote", "origin", f"refs/heads/{fakes.ORDER_BRANCH}")
    assert remote.split()[0] == sha


def test_a_pending_push_already_on_the_remote_is_reconciled_not_repeated(
    effects: Effects, worktree: Path, project: Path
) -> None:
    sha = git(worktree, "rev-parse", "HEAD")
    git(worktree, "push", "-q", "origin", f"{sha}:refs/heads/{fakes.ORDER_BRANCH}")
    push = request("push", {"branch": fakes.ORDER_BRANCH, "sha": sha})
    # The runner died after the push and before marking it done.
    entry = effects.entry(effects.key(push))
    assert entry is None
    from tac.effects import JournalEntry

    effects._write(  # pyright: ignore[reportPrivateUsage]
        JournalEntry(
            key=effects.key(push),
            state="pending",
            repository="example/demo",
            effect="push",
            arguments=push.arguments,
            run_id="r1",
            stage="publish",
            order_id=None,
            created="2026-09-25T00:00:00Z",
            modified="2026-09-25T00:00:00Z",
        )
    )
    [settled] = effects.reconcile()
    assert settled.state == "done" and settled.observed["reconciled"] is True


def test_a_pending_commit_already_made_is_reconciled(
    effects: Effects, worktree: Path
) -> None:
    write(worktree, "docs/intro.md", "# Intro\n")
    commit = request("commit", {"branch": fakes.ORDER_BRANCH, "message": "Add intro"})
    observed = effects.perform(commit)
    entry = effects.entry(effects.key(commit))
    assert entry is not None
    effects._write(entry.model_copy(update={"state": "pending", "observed": {}}))  # pyright: ignore[reportPrivateUsage]
    [settled] = effects.reconcile()
    assert settled.observed["sha"] == observed["sha"]


def test_open_pr_hands_the_token_to_gh_alone_and_never_opens_twice(
    effects: Effects, worktree: Path, bin_dir: Path
) -> None:
    arguments = {
        "branch": fakes.ORDER_BRANCH,
        "base": "main",
        "title": "Add intro",
        "body": "Order demo-order.",
    }
    first = effects.perform(request("open-pr", arguments))
    assert first["number"] == 1
    assert first["url"] == "https://github.com/example/demo/pull/1"
    # A new request for the same branch finds the open pull request.
    second = effects.perform(request("open-pr", {**arguments, "title": "Again"}))
    assert second["number"] == 1
    gh = [c for c in fakes.calls(bin_dir) if "token" in c]
    assert gh and all(c["token"] for c in gh)
    assert all("GITHUB_TOKEN" not in c["env_keys"] for c in gh)


@pytest.mark.parametrize("effect", ["open-issue", "comment", "render"])
def test_effects_of_m4_are_journaled_and_refused(effects: Effects, effect: str) -> None:
    ask = request(effect, {"branch": fakes.ORDER_BRANCH})
    with pytest.raises(EffectError, match="M4"):
        effects.perform(ask)
    entry = effects.entry(effects.key(ask))
    assert entry is not None and entry.state == "failed"


@pytest.mark.parametrize(
    ("effect", "why"), [("merge", "owner"), ("deploy", "runner_only")]
)
def test_an_effect_outside_the_runner_s_policy_is_refused(
    effects: Effects, effect: str, why: str
) -> None:
    with pytest.raises(EffectError, match=why):
        effects.authorize([request(effect, {})], None, None)


def ask_approval(project: Path, requests: list[EffectRequest], sha: str) -> str:
    tool, arguments = action(requests)
    paths = human.human_paths(project)
    item = human.Item(
        id="A1",
        kind="approval",
        topic="approvals",
        rank=0,
        question="Push?",
        agent="runner",
        pipeline="order",
        stage="publish",
        tool=tool,
        arguments=arguments,
        revision=sha,
        asked_at=human.utc_text(human.utc_now()),
    )
    human.ask(paths, item)
    return item.id


def test_an_external_effect_needs_one_signed_approval_used_once(
    effects: Effects, project: Path, worktree: Path
) -> None:
    sha = git(worktree, "rev-parse", "HEAD")
    push = [request("push", {"branch": fakes.ORDER_BRANCH, "sha": sha})]
    with pytest.raises(EffectError, match="names none"):
        effects.authorize(push, None, sha)
    item = ask_approval(project, push, sha)
    with pytest.raises(EffectError, match="waiting"):
        effects.authorize(push, item, sha)
    approvals.approve(
        human.human_paths(project),
        item,
        private=effects.runner.private,
        store=effects.runner.store,
        repository="example/demo",
        decided_by="example",
        agent_identity="tac-bot",
        decision="approved",
        now=human.utc_now(),
    )
    effects.authorize(push, item, sha)
    consumed = (effects.runner.store / "approvals" / "consumed.jsonl").read_text()
    assert item in consumed
    with pytest.raises(EffectError, match="used before"):
        effects.authorize(push, item, sha)


# ---------------------------------------------------------------- the inbox


def inbox_line(**over: Any) -> str:
    body = {
        "id": "q1",
        "effect": "commit",
        "arguments": {"branch": fakes.ORDER_BRANCH, "message": "Add intro"},
        "order_id": None,
        "run_id": "chief-1",
        "requested_by": "chief",
        "approval_id": None,
        "ts": "2026-09-25T10:00:00Z",
        **over,
    }
    return json.dumps(body) + "\n"


def test_inbox_watch_once_performs_refuses_and_keeps_its_cursor(
    effects: Effects, project: Path, worktree: Path, state: Path
) -> None:
    write(worktree, "docs/intro.md", "# Intro\n")
    folder = project / ".git" / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "inbox.jsonl").write_text(
        inbox_line()
        + "not json\n"
        + inbox_line(id="q2", effect="merge", arguments={})
        + inbox_line(
            id="q3", effect="push", arguments={"branch": "x", "sha": "0" * 40}
        ),
        encoding="utf-8",
    )
    outcomes = inbox.watch_once(effects, env(state))
    assert [(o.id, o.outcome) for o in outcomes] == [
        ("q1", "done"),
        (None, "refused"),
        ("q2", "refused"),
        ("q3", "refused"),
    ]
    assert "names none" in outcomes[3].reason
    done = (folder / "inbox.done.jsonl").read_text().splitlines()
    assert len(done) == 4
    # A second pass starts after the cursor: nothing is done twice.
    assert inbox.watch_once(effects, env(state)) == []
    with (folder / "inbox.jsonl").open("a") as handle:
        handle.write(inbox_line(id="q4", effect="render", arguments={}))
    [again] = inbox.watch_once(effects, env(state))
    assert (again.id, again.outcome) == ("q4", "failed")


def test_inbox_watch_once_on_the_command_line(
    project: Path, state: Path, bin_dir: Path, worktree: Path
) -> None:
    project_runner(project, state, bin_dir)
    folder = project / ".git" / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "inbox.jsonl").write_text(inbox_line(id="q9"), encoding="utf-8")
    done = CliRunner().invoke(
        cli,
        ["inbox", "watch", "--once", "--repo", str(project)],
        env={**env(state), **NO_AGENT},
    )
    assert done.exit_code == 0, done.output
    assert "q9: done" in done.output


# ---------------------------------------------------------------- usage


def statusline(**limits: Any) -> str:
    base = json.loads((FIXTURES / "statusline.json").read_text("utf-8"))
    if limits:
        base["rate_limits"] = limits
    return json.dumps(base)


NOW = 1_790_000_000


def window(percent: float, resets_in: int = 3600) -> dict[str, Any]:
    return {"used_percentage": percent, "resets_at": NOW + resets_in}


@pytest.fixture
def config(project: Path) -> Any:
    return load_config(project)


@pytest.mark.parametrize(
    ("five", "week", "want"),
    [(10, 20, "ok"), (10, 61, "slow"), (86, 20, "stop"), (10, 86, "stop")],
)
def test_usage_reads_the_statusline_windows(
    project: Path, config: Any, five: float, week: float, want: str
) -> None:
    usage.record(
        project,
        config,
        "claude",
        statusline(five_hour=window(five), seven_day=window(week)),
        NOW,
    )
    [reading] = usage.readings(project, config, ["claude"], now=NOW + 10)
    assert reading.state == want


def test_the_shipped_statusline_fixture_reads(project: Path, config: Any) -> None:
    sample = usage.record(project, config, "claude", statusline(), NOW)
    assert sample.rate_limits is not None and sample.rate_limits.seven_day is not None


@pytest.mark.parametrize(
    ("given", "age", "why"),
    [
        (None, 0, "no sample"),
        ({"five_hour": window(10)}, 901, "max_age_s"),
        ("absent", 0, "no API response"),
    ],
)
def test_usage_is_unavailable_without_a_fresh_reading(
    project: Path, config: Any, given: Any, age: int, why: str
) -> None:
    if given == "absent":
        body = json.loads(statusline())
        del body["rate_limits"]
        usage.record(project, config, "claude", json.dumps(body), NOW)
    elif given is not None:
        usage.record(project, config, "claude", statusline(**given), NOW)
    [reading] = usage.readings(project, config, ["claude"], now=NOW + age)
    assert reading.state == "unavailable" and why in reading.reason
    assert reading.paused


def test_a_window_past_its_reset_counts_as_empty(project: Path, config: Any) -> None:
    usage.record(project, config, "claude", statusline(five_hour=window(99, -5)), NOW)
    [reading] = usage.readings(project, config, ["claude"], now=NOW)
    assert reading.state == "ok"


@pytest.mark.parametrize(
    "limits",
    [
        {"five_hours": window(10)},
        {"five_hour": {"used": 10, "resets_at": NOW}},
        {"five_hour": window(10) | {"extra": 1}},
        "not an object",
    ],
)
def test_an_unknown_statusline_shape_is_refused_not_guessed(
    project: Path, config: Any, limits: Any
) -> None:
    body = json.loads(statusline())
    body["rate_limits"] = limits
    with pytest.raises(usage.UsageError):
        usage.record(project, config, "claude", json.dumps(body), NOW)


def test_codex_usage_has_no_documented_shape_yet(project: Path, config: Any) -> None:
    with pytest.raises(usage.UsageError, match="no documented statusline shape"):
        usage.record(project, config, "openai", statusline(), NOW)


def test_tac_usage_exits_3_while_a_provider_pauses(project: Path) -> None:
    runner_ = CliRunner()
    recorded = runner_.invoke(
        cli,
        ["usage", "record", "--root", str(project), "--provider", "claude"],
        input=statusline(five_hour=window(10, 10**9), seven_day=window(20, 10**9)),
    )
    assert recorded.exit_code == 0 and "5h 10%" in recorded.output
    ok = runner_.invoke(cli, ["usage", "--root", str(project), "--provider", "claude"])
    assert ok.exit_code == 0 and "claude: ok" in ok.output
    paused = runner_.invoke(cli, ["usage", "--root", str(project)])
    assert paused.exit_code == 3 and "openai: unavailable" in paused.output


# ---------------------------------------------------------------- tac run


def fresh_sample(project: Path, provider: str) -> None:
    folder = project / ".git" / "agents" / "usage"
    folder.mkdir(parents=True, exist_ok=True)
    now = int(dt.datetime.now(dt.UTC).timestamp())
    sample = {
        "sample_version": 1,
        "provider": provider,
        "ts": now,
        "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": now + 9000}},
    }
    (folder / f"{provider}.json").write_text(json.dumps(sample), encoding="utf-8")


@pytest.fixture
def gates(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """The runner's gate op, stubbed: this session may already be sandboxed,
    and profiles do not nest. The receipt is signed as the real one is."""
    exits: dict[str, int] = {}
    ran: list[list[str]] = []

    def gate(self: Runner, request_: Gate) -> dict[str, Any]:
        ran.append(list(request_.argv))
        revision = resolve(self.root, "HEAD")
        binding = Binding(
            repository=self.repository,
            revision=revision,
            run_id=request_.run_id,
            stage=request_.stage,
            policy_hash=policy_hash(self.root, revision),
            order_id=request_.order_id,
        )
        code = exits.get(request_.argv[1], 0)
        if isinstance(code, list):
            code = code.pop(0) if code else 0
        return self.issue("gate", binding, {"argv": list(request_.argv), "exit": code})

    monkeypatch.setattr(Runner, "gate", gate)
    exits["__ran__"] = ran  # pyright: ignore[reportArgumentType]
    return exits


def context(
    project: Path,
    state: Path,
    bin_dir: Path,
    order: Any,
    out: list[str],
    pipeline: str = "order",
) -> run.Context:
    runner_ = project_runner(project, state, bin_dir)
    runner_ = run_runner(runner_, project)
    config = load_config(project)
    chain = keychain(bin_dir, runner_.store.name)
    if not chain.exists(BOT_TOKEN):
        chain.write(BOT_TOKEN, TOKEN)
    environ = {
        **env(state),
        "PATH": runner_.search_path,
        "HOME": str(state),
        "LANG": "C",
        "GITHUB_TOKEN": "must-not-reach-a-client",
    }
    return run.Context(
        root=project,
        config=config,
        runner=runner_,
        effects=Effects(runner_, config, chain),
        pipeline=run.runnable(project, config, pipeline),
        order=order,
        environ=environ,
        search_path=runner_.search_path,
        out=out.append,
    )


def run_runner(runner_: Runner, project: Path) -> Runner:
    return dataclasses.replace(runner_, recipes=run.gate_recipes(project))


def entry(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "issue.json"
    path.write_text(
        (Path(__file__).parent / "fixtures" / "handoffs" / "issue.json").read_text(),
        encoding="utf-8",
    )
    return {"issue": str(path)}


@pytest.fixture
def ready(project: Path, worktree: Path, bin_dir: Path) -> Path:
    fresh_sample(project, "claude")
    fresh_sample(project, "openai")
    write(worktree, "docs/intro.md", "# Intro\n")
    return project


def test_an_order_runs_to_the_owner_s_approval_then_to_the_merge_command(
    ready: Path, state: Path, bin_dir: Path, tmp_path: Path, gates: dict[str, Any]
) -> None:
    out: list[str] = []
    ctx = context(ready, state, bin_dir, fakes.demo_order(ready), out)
    first = run.advance(
        ctx, run.fresh(ctx, "claude", entry(tmp_path), fakes.ORDER_BRANCH)
    )
    stages = first.stages
    for sid in ("intake", "specify", "build"):
        assert stages[sid].state == "complete", (sid, stages[sid].reason)
    assert stages["review"].state == "reviewed"
    assert stages["signoff"].state == "skipped"
    assert stages["gate"].state == "verified"
    assert stages["publish"].state == "waiting-human"
    assert first.status == "waiting-human"
    # The builder's change was committed as tac-bot before the approval.
    worktree = tmp_path / "worktrees" / fakes.ORDER_BRANCH
    assert git(worktree, "log", "-1", "--format=%an") == "tac-bot"
    sha = git(worktree, "rev-parse", "HEAD")
    item = stages["publish"].approval_item
    assert item is not None
    # The review ran on the other provider, the rest on Claude.
    codex_calls = fakes.calls(bin_dir)
    assert any(c["argv"][:1] == ["exec"] for c in codex_calls)
    # The owner signs on the host; the run resumes and publishes.
    approvals.approve(
        human.human_paths(ready),
        item,
        private=ctx.runner.private,
        store=ctx.runner.store,
        repository="example/demo",
        decided_by="example",
        agent_identity="tac-bot",
        decision="approved",
        now=human.utc_now(),
    )
    second = run.advance(ctx, run.resume(ctx, first.run_id))
    assert second.stages["publish"].state == "complete"
    assert second.stages["recap"].state == "complete"
    assert second.stages["land"].state == "waiting-human"
    assert git(ready, "ls-remote", "origin", fakes.ORDER_BRANCH).split()[0] == sha
    command = (
        f"gh pr review 1 --approve && gh pr merge 1 --squash --match-head-commit {sha}"
    )
    assert command in out
    land = human.load_item(
        human.human_paths(ready), second.stages["land"].human_item or ""
    )
    assert f"`{command}`" in land.details
    assert command in (ready / "TODO.HUMAN.md").read_text()
    # The checkpoint on disk is the state the loop returned.
    assert run.load(ctx.runner.store, first.run_id) == second


def test_the_merge_command_is_squash_pinned_to_the_head_commit() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert run.merge_command(7, sha) == (
        f"gh pr review 7 --approve && gh pr merge 7 --squash --match-head-commit {sha}"
    )
    with pytest.raises(run.RunError):
        run.merge_command(7, "HEAD")


def test_each_dispatch_is_bound_to_its_prompt_and_billed_to_the_subscription(
    ready: Path, state: Path, bin_dir: Path, tmp_path: Path, gates: dict[str, Any]
) -> None:
    ctx = context(ready, state, bin_dir, fakes.demo_order(ready), [])
    done = run.advance(
        ctx, run.fresh(ctx, "claude", entry(tmp_path), fakes.ORDER_BRANCH)
    )
    session = done.stages["intake"].sessions[0]
    dispatch = ready / ".git" / "agents" / "dispatch"
    record = json.loads((dispatch / f"{session}.json").read_text())
    assert set(record) == {"run_id", "stage", "role", "token", "kind", "session_id"}
    launch_record = json.loads((dispatch / f"{session}.launch.json").read_text())
    assert set(launch_record) == {
        "provider",
        "harness",
        "model",
        "effort_requested",
        "effort_actual",
        "role",
    }
    [call] = [c for c in fakes.calls(bin_dir) if session in c.get("argv", [])]
    digest = hashlib.sha256(call["prompt"].encode()).hexdigest()
    token_file = (
        ctx.runner.store
        / "tokens"
        / f"{hashlib.sha256(record['token'].encode()).hexdigest()}.json"
    )
    assert json.loads(token_file.read_text())["sha256"] == digest
    assert "--bare" not in call["argv"]
    assert "GITHUB_TOKEN" not in call["env"]
    [receipt_id] = done.stages["intake"].receipts
    signed = json.loads(
        (ctx.runner.store / "receipts" / done.run_id / f"{receipt_id}.json").read_text()
    )
    assert signed["receipt"]["kind"] == "dispatch"
    assert signed["receipt"]["observed"]["billing_route"] == "subscription"
    assert (ctx.runner.store / "dispatched" / session).is_file()


def test_a_result_that_fails_twice_goes_to_the_owner(
    ready: Path, state: Path, bin_dir: Path, tmp_path: Path, gates: dict[str, Any]
) -> None:
    fakes.control(bin_dir, invalid={"order-request": 2})
    ctx = context(ready, state, bin_dir, fakes.demo_order(ready), [])
    done = run.advance(
        ctx, run.fresh(ctx, "claude", entry(tmp_path), fakes.ORDER_BRANCH)
    )
    intake = done.stages["intake"]
    assert (intake.state, done.status) == ("waiting-human", "waiting-human")
    assert len(intake.sessions) == 2
    item = human.load_item(human.human_paths(ready), intake.human_item or "")
    assert item.topic == "escalations"


def test_one_repair_pass_recovers_a_first_bad_result(
    ready: Path, state: Path, bin_dir: Path, tmp_path: Path, gates: dict[str, Any]
) -> None:
    fakes.control(bin_dir, invalid={"order-request": 1})
    ctx = context(ready, state, bin_dir, fakes.demo_order(ready), [])
    done = run.advance(
        ctx, run.fresh(ctx, "claude", entry(tmp_path), fakes.ORDER_BRANCH)
    )
    assert done.stages["intake"].state == "complete"
    envelope = json.loads(Path(done.stages["intake"].envelope or "").read_text())
    assert envelope["status"] == "degraded"


def test_a_failing_gate_is_repaired_at_most_retry_max_times(
    ready: Path, state: Path, bin_dir: Path, tmp_path: Path, gates: dict[str, Any]
) -> None:
    gates["work-check"] = [1, 1, 1, 1]  # pyright: ignore[reportArgumentType]
    ctx = context(ready, state, bin_dir, fakes.demo_order(ready), [])
    done = run.advance(
        ctx, run.fresh(ctx, "claude", entry(tmp_path), fakes.ORDER_BRANCH)
    )
    build = done.stages["build"]
    assert build.state == "waiting-human"
    ran = [argv[1] for argv in gates["__ran__"]]  # pyright: ignore[reportIndexIssue, reportGeneralTypeIssues]
    # First round, then two reruns each after one repair: retry.max = 2 and
    # blocks_no_progress = 2 end it at the same point.
    assert ran.count("work-check") == 3 and ran.count("work-repair") == 2
    assert build.gate_reruns == 2


def test_a_paused_usage_reading_blocks_the_stage(
    project: Path, worktree: Path, state: Path, bin_dir: Path, tmp_path: Path
) -> None:
    ctx = context(project, state, bin_dir, fakes.demo_order(project), [])
    done = run.advance(
        ctx, run.fresh(ctx, "claude", entry(tmp_path), fakes.ORDER_BRANCH)
    )
    assert done.status == "blocked"
    assert "usage claude is unavailable" in done.stages["intake"].reason
    assert [c for c in fakes.calls(bin_dir) if "prompt" in c] == []


def test_a_full_host_budget_blocks_the_stage(
    ready: Path, state: Path, bin_dir: Path, tmp_path: Path
) -> None:
    held = [
        leases.acquire(env(state), 4, repository="x/y", run_id="r", stage="s", role="b")
        for _ in range(4)
    ]
    ctx = context(ready, state, bin_dir, fakes.demo_order(ready), [])
    done = run.advance(
        ctx, run.fresh(ctx, "claude", entry(tmp_path), fakes.ORDER_BRANCH)
    )
    assert done.status == "blocked"
    assert "max_local_agents" in done.stages["intake"].reason
    for lease in held:
        leases.release(lease)


def test_a_cross_team_order_runs_its_signoff(
    ready: Path, state: Path, bin_dir: Path, tmp_path: Path, gates: dict[str, Any]
) -> None:
    ctx = context(ready, state, bin_dir, fakes.demo_order(ready, cross=("docs",)), [])
    done = run.advance(
        ctx, run.fresh(ctx, "claude", entry(tmp_path), fakes.ORDER_BRANCH)
    )
    assert done.stages["signoff"].state == "complete"


def test_resume_reloads_the_checkpoint_and_reconciles_first(
    ready: Path,
    state: Path,
    bin_dir: Path,
    tmp_path: Path,
    gates: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = context(ready, state, bin_dir, fakes.demo_order(ready), [])
    first = run.advance(
        ctx, run.fresh(ctx, "claude", entry(tmp_path), fakes.ORDER_BRANCH)
    )
    reconciled: list[bool] = []
    monkeypatch.setattr(
        Effects, "reconcile", lambda self: reconciled.append(True) or []
    )
    resumed = run.resume(ctx, first.run_id)
    assert reconciled == [True]
    assert resumed.stages["intake"] == first.stages["intake"]
    before = len(fakes.calls(bin_dir))
    again = run.advance(ctx, resumed)
    # Done stages are not dispatched again; publish still waits on the owner.
    assert len(fakes.calls(bin_dir)) == before
    assert again.stages["publish"].state == "waiting-human"


@pytest.mark.parametrize(
    ("edit", "why"),
    [
        ("disabled", r"not in \[pipelines\] enabled"),
        ("waiting", "waits on M2"),
    ],
)
def test_run_refuses_a_pipeline_that_may_not_run(
    project: Path, edit: str, why: str
) -> None:
    config = load_config(project)
    if edit == "disabled":
        enabled = config.knobs.pipelines.model_copy(update={"enabled": ("board",)})
        knobs = config.knobs.model_copy(update={"pipelines": enabled})
        config = dataclasses.replace(config, knobs=knobs)
    else:
        order = project / ".agents/config/pipelines/order.toml"
        shipped = REPO / ".agents/config/pipelines/order.toml"
        order.write_text(shipped.read_text("utf-8"), encoding="utf-8")
    with pytest.raises(run.RunError, match=why):
        run.runnable(project, config, "order")


def test_the_gate_allowlist_holds_each_arity_to_the_justfile() -> None:
    from tac.pipelines import recipes

    repo_root = Path(__file__).resolve().parents[1]
    table = run.gate_recipes(repo_root)
    known = recipes(repo_root)
    for name in (
        "work-check",
        "work-review",
        "work-accept",
        "work-repair",
        "changelog",
    ):
        assert name in table, name
    for name, arity in table.items():
        assert name in known, name
        assert known[name].takes(arity), (name, arity)


def test_tac_run_dry_run_prints_the_stage_order(project: Path) -> None:
    done = CliRunner().invoke(
        cli,
        ["run", "order", "--order", "demo-order", "--dry-run", "--repo", str(project)],
    )
    assert done.exit_code == 0, done.output
    assert done.output.splitlines()[0] == "intake (agent)"
    assert "land (human) after recap" in done.output


def test_tac_run_refuses_the_shipped_order_pipeline_until_its_gate_is_live(
    repo: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    done = CliRunner().invoke(
        cli, ["run", "order", "--order", "demo-order", "--dry-run", "--repo", str(root)]
    )
    assert done.exit_code == 1 and "waits on M2" in done.output


def test_a_receipt_signed_in_a_run_verifies_against_the_runner_key(
    ready: Path, state: Path, bin_dir: Path, tmp_path: Path, gates: dict[str, Any]
) -> None:
    ctx = context(ready, state, bin_dir, fakes.demo_order(ready), [])
    done = run.advance(
        ctx, run.fresh(ctx, "claude", entry(tmp_path), fakes.ORDER_BRANCH)
    )
    [receipt_id] = done.stages["intake"].receipts
    text = (
        ctx.runner.store / "receipts" / done.run_id / f"{receipt_id}.json"
    ).read_text()
    from tac.receipts import parse

    signed = parse(text)
    verified = verify(signed, ctx.runner.private.public_key(), signed.receipt.binding)
    assert verified.kind == "dispatch"


seatbelt = pytest.mark.skipif(
    default_sandbox() is None, reason="needs macOS sandbox-exec"
)


@seatbelt
def test_a_gate_stage_runs_the_runner_s_real_gate_in_the_order_worktree(
    project: Path, worktree: Path, state: Path, bin_dir: Path
) -> None:
    just = bin_dir / "just"
    just.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@" > "$TMPDIR/seen"\nexit 0\n')
    just.chmod(0o755)
    ctx = context(project, state, bin_dir, fakes.demo_order(project), [])
    assert ctx.runner.sandbox is not None
    state_ = run.fresh(ctx, "claude", {}, fakes.ORDER_BRANCH)
    [stage] = [s for s in ctx.pipeline.stages if s.id == "gate"]
    done = run.gate_stage(ctx, state_, stage)
    assert done.stages["gate"].state == "verified"
    [receipt_id] = done.stages["gate"].receipts
    signed = json.loads(
        (
            ctx.runner.store / "receipts" / state_.run_id / f"{receipt_id}.json"
        ).read_text()
    )
    assert signed["receipt"]["observed"]["argv"] == [
        "just",
        "work-accept",
        fakes.ORDER_BRANCH,
    ]
    assert signed["receipt"]["binding"]["revision"] == git(
        worktree, "rev-parse", "HEAD"
    )
