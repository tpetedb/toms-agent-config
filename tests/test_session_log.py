"""The session journal of design section 6: one JSON-lines file per session
with all four token fields, a per-day index that rolls each session into one
line, the secrets scan on every write, and one audited purge path."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from tac.cli import cli
from tac.session import (
    DAY_INDEX,
    PURGED_DIR,
    PURGES_FILE,
    SESSIONS_DIR,
    SessionEvent,
    Tokens,
    log,
    purge,
)
from tac.work import Bad
from tests._syncproject import copy_project, replace_in

NOW = dt.datetime(2026, 9, 24, 18, 0, tzinfo=dt.UTC)
DAY = "2026-09-24"
FIELDS = {
    "schema_version",
    "ts",
    "session_id",
    "provider",
    "harness",
    "model",
    "effort",
    "role",
    "event",
    "tool",
    "topic",
    "cost_usd",
    "tokens",
}


def ev(session: str, minute: int, **changes: object) -> SessionEvent:
    body: dict[str, object] = {
        "ts": f"{DAY}T14:{minute:02d}:00Z",
        "session_id": session,
        "event": "tool",
        "provider": "anthropic",
        "harness": "claude-code",
        "model": "claude-opus-5-5",
        "effort": "xhigh",
        "role": "builder",
    }
    body.update(changes)
    return SessionEvent.model_validate(body)


def test_log_writes_every_field_and_all_four_token_fields(tmp_path: Path) -> None:
    store = tmp_path / "store"
    path = log(store, ev("s1", 1, tool="Bash", tokens=Tokens(input=10)))
    assert path == store / SESSIONS_DIR / DAY / "s1.jsonl"
    [row] = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    assert set(row) == FIELDS
    assert row["tokens"] == {
        "input": 10,
        "output": None,
        "cache_read": None,
        "cache_write": None,
    }
    assert row["tool"] == "Bash" and row["cost_usd"] is None


def test_the_day_index_rolls_each_session_into_one_line(tmp_path: Path) -> None:
    store = tmp_path / "store"
    log(store, ev("s1", 1, cost_usd=0.5, tokens=Tokens(input=10, output=2)))
    log(store, ev("s1", 9, cost_usd=0.25, tokens=Tokens(input=5, cache_read=7)))
    log(store, ev("s2", 3))
    text = (store / SESSIONS_DIR / DAY / DAY_INDEX).read_text("utf-8")
    index = json.loads(text)
    assert index["date"] == DAY
    first, second = index["sessions"]
    assert first == {
        "session_id": "s1",
        "first_ts": f"{DAY}T14:01:00Z",
        "last_ts": f"{DAY}T14:09:00Z",
        "events": 2,
        "cost_usd": 0.75,
        "tokens": {"input": 15, "output": 2, "cache_read": 7, "cache_write": None},
    }
    assert second["session_id"] == "s2" and second["cost_usd"] is None
    # One line per session.
    assert sum('"session_id"' in line for line in text.splitlines()) == 2
    assert [json.loads(x.strip().rstrip(",")) for x in text.splitlines()[4:6]] == [
        first,
        second,
    ]


def test_a_secret_in_a_session_event_is_refused_by_name(tmp_path: Path) -> None:
    secret = "gh" + "p_" + "Q" * 36
    with pytest.raises(Bad) as refused:
        log(tmp_path / "store", ev("s1", 1, tool=f"curl -H {secret}"))
    assert "github-token" in str(refused.value)
    assert secret not in str(refused.value)
    assert not (tmp_path / "store" / SESSIONS_DIR / DAY / "s1.jsonl").exists()


def test_purge_moves_the_file_and_leaves_an_audit_line(tmp_path: Path) -> None:
    store = tmp_path / "store"
    path = log(store, ev("s1", 1))
    log(store, ev("s2", 2))
    data = path.read_bytes()
    done = purge(store, "s1", reason="pasted a customer name", by="owner", now=NOW)
    assert not path.exists()
    [moved] = done.moved
    assert moved == store / SESSIONS_DIR / PURGED_DIR / DAY / "s1.jsonl"
    assert moved.read_bytes() == data
    [audit] = [
        json.loads(x)
        for x in (store / SESSIONS_DIR / PURGES_FILE).read_text("utf-8").splitlines()
    ]
    assert audit == {
        "at": "2026-09-24T18:00:00Z",
        "by": "owner",
        "date": DAY,
        "reason": "pasted a customer name",
        "session_id": "s1",
        "sha256": hashlib.sha256(data).hexdigest(),
        "to": f"{PURGED_DIR}/{DAY}/s1.jsonl",
    }
    index = json.loads((store / SESSIONS_DIR / DAY / DAY_INDEX).read_text("utf-8"))
    assert [s["session_id"] for s in index["sessions"]] == ["s2"]
    with pytest.raises(Bad, match="no session s1"):
        purge(store, "s1", reason="again", by="owner", now=NOW)
    with pytest.raises(Bad, match="needs a --reason"):
        purge(store, "s2", reason=" ", by="owner", now=NOW)


@pytest.mark.parametrize(
    "session_id", ["../../memory/records", "*", "s?", "[s]1", "a/b", "..", "a..b"]
)
def test_purge_refuses_an_id_that_is_not_a_session_id(
    tmp_path: Path, session_id: str
) -> None:
    store = tmp_path / "store"
    log(store, ev("s1", 1))
    journal = store / "memory" / "records.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"sequence": 1}\n', encoding="utf-8")
    with pytest.raises(Bad, match="is not a session id"):
        purge(store, session_id, reason="cleanup", by="agent", now=NOW)
    assert journal.read_text("utf-8") == '{"sequence": 1}\n'
    assert (store / SESSIONS_DIR / DAY / "s1.jsonl").is_file()
    assert not (store / SESSIONS_DIR / PURGED_DIR).exists()
    assert not (store / SESSIONS_DIR / PURGES_FILE).exists()


def test_purge_reads_only_dated_folders(tmp_path: Path) -> None:
    store = tmp_path / "store"
    odd = store / SESSIONS_DIR / "not-a-day" / "s1.jsonl"
    odd.parent.mkdir(parents=True)
    odd.write_text("{}\n", encoding="utf-8")
    with pytest.raises(Bad, match="no session s1"):
        purge(store, "s1", reason="cleanup", by="agent", now=NOW)
    assert odd.is_file()


def test_the_cli_refuses_a_purge_that_walks_out_of_the_journal(
    tmp_path: Path,
) -> None:
    root = copy_project(tmp_path / "proj")
    result = CliRunner().invoke(
        cli,
        [
            "session",
            "purge",
            "../../memory/records",
            "--root",
            str(root),
            "--reason",
            "cleanup",
        ],
    )
    assert result.exit_code == 1
    assert "is not a session id" in result.output


def test_the_cli_logs_from_stdin_and_flags_into_the_redirected_store(
    tmp_path: Path,
) -> None:
    root = copy_project(tmp_path / "proj")
    replace_in(
        root / ".agents/config.toml",
        'runtime_store = "git-common-dir"',
        f'runtime_store = "{tmp_path / "store"}"',
    )
    payload = {"ts": f"{DAY}T14:00:00Z", "session_id": "cli-1", "event": "start"}
    result = CliRunner().invoke(
        cli,
        [
            "session",
            "log",
            "--root",
            str(root),
            "--from",
            "-",
            "--tool",
            "Read",
            "--tokens-output",
            "42",
        ],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    path = tmp_path / "store" / SESSIONS_DIR / DAY / "cli-1.jsonl"
    [row] = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    assert (row["event"], row["tool"], row["tokens"]["output"]) == ("start", "Read", 42)
    purged = CliRunner().invoke(
        cli,
        [
            "session",
            "purge",
            "cli-1",
            "--root",
            str(root),
            "--reason",
            "test",
            "--by",
            "t",
        ],
    )
    assert purged.exit_code == 0, purged.output
    assert not path.exists()
