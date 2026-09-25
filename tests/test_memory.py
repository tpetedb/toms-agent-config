"""The memory bus of design section 6: append-only records and events, derived
status, quarantine, promotion under its three rules, the index, the lint and the
secrets scan. Every store is under tmp_path; ids and times are injected."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft7Validator

from tac import memory, secrets_scan
from tac.cli import cli
from tac.contracts import strict_subset_problems
from tac.memory import (
    EVENTS_DIR,
    EVENTS_FILE,
    INDEX_DIR,
    INDEX_FILES,
    INDEX_README,
    MAX_STATEMENT,
    PROMOTED_DIR,
    QUARANTINE_FILE,
    RECORD_CONTRACT,
    RECORDS_DIR,
    RECORDS_FILE,
    TOOL_WRITTEN,
    Evidence,
    Record,
    Stores,
    add,
    append_event,
    build_index,
    known,
    lint,
    promote,
    read_launch,
    record_json_schema,
    record_text,
    schema_text,
    write_index,
)
from tac.receipts import Binding, policy_hash, sign
from tac.runner import pub_file_text
from tac.session import SESSION_CONTRACT, session_json_schema
from tac.work import Bad
from tests._gitrepo import commit_all, git, make_repo
from tests._syncproject import REPO, copy_project, replace_in
from tests.fixtures.memory.build import (
    NOW,
    NOW_TEXT,
    REPOSITORY,
    event,
    record,
    review,
    suffixes,
)

FIXTURES = REPO / "tests" / "fixtures" / "memory"
LATER = NOW + dt.timedelta(hours=1)
LATER_TEXT = "2026-09-24T15:25:00Z"
# Secret-shaped strings are assembled at run time, so no literal one sits in
# this file for a scanner to trip on.
FAKE_OPENAI = "sk-" + "a1B2" * 6
FAKE_GITHUB = "gh" + "p_" + "Z9y8" * 9


def draft(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "decision",
        "scope": "project",
        "topic": ["harness.config"],
        "paths": [".agents/config.toml"],
        "statement": "The knob file is .agents/config.toml.",
    }
    body.update(changes)
    return body


@pytest.fixture
def stores(tmp_path: Path) -> Stores:
    return Stores(
        promoted=tmp_path / "checkout" / PROMOTED_DIR,
        live=tmp_path / "store" / memory.LIVE_DIR,
    )


def live(stores: Stores) -> Path:
    assert stores.live is not None
    return stores.live


def put(stores: Stores, **changes: Any) -> memory.Added:
    now = changes.pop("now", NOW)
    suffix = changes.pop("suffix", None) or suffixes()
    return add(
        stores,
        draft(**changes),
        repository=REPOSITORY,
        launch=None,
        now=now,
        suffix=suffix,
    )


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x]


# ---------------------------------------------------------------- add


def test_add_writes_the_tool_metadata_from_the_launch_record(stores: Stores) -> None:
    launch = read_launch(FIXTURES / "launch.json")
    added = add(
        stores,
        draft(),
        repository=REPOSITORY,
        launch=launch,
        now=NOW,
        suffix=suffixes(),
    )
    got = added.record
    assert got.id == "mem:20260924T142500Z:0000"
    assert got.sequence == 1
    assert (got.provider, got.harness, got.model) == (
        "anthropic",
        "claude-code",
        "claude-opus-5-5",
    )
    assert (got.effort_requested, got.effort_actual, got.role) == (
        "xhigh",
        "xhigh",
        "builder",
    )
    assert got.created == got.valid_from == NOW_TEXT
    [row] = lines(live(stores) / RECORDS_FILE)
    # Every property present, nulls explicit, nothing derived stored.
    assert set(row) == set(Record.model_fields)
    assert row["valid_to"] is None and row["commit_sha"] is None
    assert "status" not in row and "confidence" not in row


def test_without_a_launch_record_the_tool_fields_are_null(stores: Stores) -> None:
    got = put(stores).record
    assert all(getattr(got, name) is None for name in TOOL_WRITTEN)


@pytest.mark.parametrize("name", TOOL_WRITTEN)
def test_a_tool_written_field_typed_into_the_payload_is_refused(
    stores: Stores, name: str
) -> None:
    with pytest.raises(Bad, match="written by the tool from the launch record"):
        add(
            stores,
            {**draft(), name: "typed-by-a-model"},
            repository=REPOSITORY,
            launch=read_launch(FIXTURES / "launch.json"),
            now=NOW,
            suffix=suffixes(),
        )
    assert not (live(stores) / RECORDS_FILE).exists()


@pytest.mark.parametrize("name", ["id", "sequence", "content_hash", "status"])
def test_a_computed_or_derived_field_in_the_payload_is_refused(
    stores: Stores, name: str
) -> None:
    with pytest.raises(Bad, match=name):
        put(stores, **{name: "x"})


def test_a_basis_in_the_payload_is_ignored_and_said_so(stores: Stores) -> None:
    added = put(stores, basis="test-receipt")
    assert any("basis ignored" in note for note in added.notes)
    assert "basis" not in lines(live(stores) / RECORDS_FILE)[0]


def test_sequence_is_the_last_plus_one_and_ids_are_reproducible(
    stores: Stores, tmp_path: Path
) -> None:
    suffix = suffixes()
    got = [put(stores, statement=f"Claim {n}.", suffix=suffix).record for n in range(3)]
    assert [r.sequence for r in got] == [1, 2, 3]
    assert [r.id[-4:] for r in got] == ["0000", "0001", "0002"]
    # The same inputs into a fresh store write the same bytes.
    other = Stores(tmp_path / "b" / PROMOTED_DIR, tmp_path / "b" / "store")
    suffix = suffixes()
    for n in range(3):
        put(other, statement=f"Claim {n}.", suffix=suffix)
    assert (live(other) / RECORDS_FILE).read_bytes() == (
        live(stores) / RECORDS_FILE
    ).read_bytes()


def test_a_torn_line_is_quarantined_once_never_dropped(stores: Stores) -> None:
    put(stores, statement="First.")
    journal = live(stores) / RECORDS_FILE
    fragment = '{"schema_version": 1, "id": "mem:2026'
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(fragment)
    assert any(
        p.startswith(f"live/{RECORDS_FILE}:2: does not parse") for p in lint(stores)
    )
    second = put(stores, statement="Second.", suffix=suffixes(1))
    assert second.record.sequence == 2
    assert "quarantined 1 line(s) of the live journal" in second.notes
    # The fragment stays on its own line; the new record never joins it.
    raw = journal.read_text("utf-8").splitlines()
    assert raw[1] == fragment
    assert json.loads(raw[2])["id"] == second.record.id
    [held] = lines(live(stores) / QUARANTINE_FILE)
    assert held["raw"] == fragment
    assert held["reason"].startswith("not JSON")
    assert (held["file"], held["line"]) == (f"live/{RECORDS_FILE}", 2)
    assert lint(stores) == []
    put(stores, statement="Third.", suffix=suffixes(2))
    assert len(lines(live(stores) / QUARANTINE_FILE)) == 1


def test_an_unknown_schema_version_is_refused_loudly(stores: Stores) -> None:
    put(stores)
    future = {**lines(live(stores) / RECORDS_FILE)[0], "schema_version": 7}
    with (live(stores) / RECORDS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(future) + "\n")
    with pytest.raises(Bad, match="schema_version 7 is unknown; this tac reads 1"):
        put(stores, statement="Another.", suffix=suffixes(5))
    assert any("schema_version 7 is unknown" in p for p in lint(stores))
    assert memory.MIGRATIONS.keys() == {1}


# ---------------------------------------------------------------- events


def test_events_derive_status_and_never_rewrite_a_record(stores: Stores) -> None:
    suffix = suffixes()
    a = put(stores, statement="Use A.", suffix=suffix).record
    append_event(
        stores,
        kind="review",
        record=a.id,
        other=None,
        note=None,
        by="test",
        basis="reviewed",
        now=NOW,
        suffix=suffix,
    )
    assert known(stores).derived[a.id].status == "confirmed"

    # A contradicting record on the same topic set and kind marks both.
    b_added = put(stores, statement="Use B.", suffix=suffix)
    b = b_added.record
    [conflict] = b_added.events
    assert (conflict.kind, conflict.record, conflict.supersedes_with) == (
        "conflict",
        b.id,
        a.id,
    )
    derived = known(stores).derived
    assert derived[a.id].status == derived[b.id].status == "conflict"
    assert derived[a.id].conflicts_with == (b.id,)

    # A correct event closes one side through the event only.
    append_event(
        stores,
        kind="correct",
        record=a.id,
        other=b.id,
        note="B replaces A",
        by="test",
        basis=None,
        now=LATER,
        suffix=suffix,
    )
    derived = known(stores).derived
    assert derived[a.id].status == "superseded"
    assert derived[a.id].valid_to == LATER_TEXT
    assert derived[b.id].status == "unconfirmed"
    assert derived[b.id].conflicts_with == ()
    rows = lines(live(stores) / RECORDS_FILE)
    assert rows[0]["valid_to"] is None
    assert [e["sequence"] for e in lines(live(stores) / EVENTS_FILE)] == [1, 2, 3]


def test_a_record_that_supersedes_another_closes_it_by_event(stores: Stores) -> None:
    suffix = suffixes()
    old = put(stores, statement="Old.", suffix=suffix).record
    new = put(stores, statement="New.", supersedes=old.id, now=LATER, suffix=suffix)
    [closing] = new.events
    assert (closing.kind, closing.record, closing.supersedes_with) == (
        "supersede",
        old.id,
        new.record.id,
    )
    derived = known(stores).derived
    assert derived[old.id].status == "superseded"
    assert derived[old.id].valid_to == LATER_TEXT
    with pytest.raises(Bad, match="which no store holds"):
        put(stores, supersedes="mem:20200101T000000Z:ffff", suffix=suffix)


def test_decay_lowers_the_confidence_level(stores: Stores) -> None:
    got = put(stores).record
    events = [review(1, got.id, "test-receipt"), event(2, "decay", got.id)]
    view = memory.derive([got], events)[got.id]
    assert (view.status, view.level, view.basis) == (
        "confirmed",
        "medium",
        "test-receipt",
    )


# ---------------------------------------------------------------- promotion


def stub_evidence(monkeypatch: pytest.MonkeyPatch, basis: str = "reviewed") -> None:
    monkeypatch.setattr(
        memory,
        "verify_evidence",
        lambda _root, _record, _item, _now: Evidence(basis, "stub"),  # type: ignore[arg-type]
    )


def promote_here(stores: Stores, rid: str, evidence: list[str], **kw: Any) -> Any:
    return promote(
        stores.promoted.parent.parent,
        stores,
        rid,
        evidence,
        now=kw.pop("now", LATER),
        environ=kw.pop("environ", {}),
        ancestors=kw.pop("ancestors", []),
        suffix=kw.pop("suffix", suffixes(0x100)),
    )


def test_promote_is_refused_inside_an_agent_session(stores: Stores) -> None:
    got = put(stores).record
    with pytest.raises(Bad, match=r"promote refused: .*CLAUDECODE is set"):
        promote_here(stores, got.id, [], environ={"CLAUDECODE": "1"})
    with pytest.raises(Bad, match="claude is among its parents"):
        promote_here(stores, got.id, [], ancestors=["/usr/local/bin/claude -p"])


def test_promote_without_evidence_names_the_basis_rule(stores: Stores) -> None:
    bare = put(stores).record
    with pytest.raises(Bad, match=r"rule basis: .* is inferred"):
        promote_here(stores, bare.id, [])
    sourced = put(
        stores, statement="Cited.", source_refs=["docs/DESIGN.md"], suffix=suffixes(9)
    ).record
    with pytest.raises(Bad, match=r"rule basis: .* is single-source"):
        promote_here(stores, sourced.id, [])
    assert not (stores.promoted / RECORDS_DIR).exists()


def test_promote_refuses_a_record_in_conflict(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    suffix = suffixes()
    a = put(stores, statement="Use A.", suffix=suffix).record
    append_event(
        stores,
        kind="review",
        record=a.id,
        other=None,
        note=None,
        by="test",
        basis="reviewed",
        now=NOW,
        suffix=suffix,
    )
    b = put(stores, statement="Use B.", suffix=suffix).record
    stub_evidence(monkeypatch)
    with pytest.raises(Bad, match=f"rule status: {b.id} is conflict.*{a.id}"):
        promote_here(stores, b.id, ["review:demo"])


def test_promote_refuses_a_secret_like_line_and_never_repeats_it(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Written around `add`, which would have refused it: the rule still holds.
    planted = record(1, statement=f"The key is {FAKE_OPENAI} for now.")
    path = live(stores) / RECORDS_FILE
    path.parent.mkdir(parents=True)
    path.write_text(planted.line() + "\n", encoding="utf-8")
    stub_evidence(monkeypatch)
    with pytest.raises(Bad) as refused:
        promote_here(stores, planted.id, ["review:demo"])
    assert "openai-key" in str(refused.value)
    assert FAKE_OPENAI not in str(refused.value)
    assert not (stores.promoted / RECORDS_DIR).exists()
    assert any("openai-key" in p for p in lint(stores))
    assert not any(FAKE_OPENAI in p for p in lint(stores))


def test_promote_keeps_a_secret_record_in_the_worker_store(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = put(stores, sensitivity="secret").record
    stub_evidence(monkeypatch)
    with pytest.raises(Bad, match="rule sensitivity"):
        promote_here(stores, got.id, ["review:demo"])


@pytest.fixture
def runner_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def repo(tmp_path: Path, runner_key: Ed25519PrivateKey) -> Path:
    root = tmp_path / "demo"
    make_repo(root, runner_pub=pub_file_text(runner_key))
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('observed')\n", encoding="utf-8")
    commit_all(root, "candidate work")
    return root


def receipt_file(
    root: Path,
    key: Ed25519PrivateKey,
    target: Path,
    exit_code: int = 0,
    revision: str = "HEAD",
) -> Path:
    head = git(root, "rev-parse", revision)
    signed = sign(
        key,
        "gate",
        Binding(
            repository=REPOSITORY,
            revision=head,
            run_id="demo",
            stage="verify",
            policy_hash=policy_hash(root, head),
            order_id="demo",
        ),
        {"argv": ["just", "verify"], "exit": exit_code},
    )
    target.write_text(signed.to_json(), encoding="utf-8")
    return target


def test_promote_accepts_a_record_with_a_verified_receipt(
    repo: Path, runner_key: Ed25519PrivateKey, tmp_path: Path
) -> None:
    stores = Stores.at(repo, tmp_path / "store")
    got = put(stores, order_id="demo").record
    receipt = receipt_file(repo, runner_key, tmp_path / "receipt.json")
    done = promote_here(stores, got.id, [f"receipt:{receipt}"])
    assert done.basis == "test-receipt"
    written = repo / PROMOTED_DIR / RECORDS_DIR / f"{got.id.replace(':', '_')}.json"
    assert done.path == written
    assert written.read_text("utf-8") == record_text(got)
    [day] = sorted((repo / PROMOTED_DIR / EVENTS_DIR).iterdir())
    assert day.name == "2026-09-24.jsonl"
    [copied] = lines(day)
    assert (copied["kind"], copied["basis"], copied["sequence"]) == (
        "review",
        "test-receipt",
        1,
    )
    status = json.loads(
        (repo / PROMOTED_DIR / INDEX_DIR / "by-status.json").read_text("utf-8")
    )
    assert status["status"]["confirmed"] == [got.id]
    assert status["records"][got.id]["confidence"] == {
        "level": "high",
        "basis": "test-receipt",
    }
    assert lint(stores) == []
    with pytest.raises(Bad, match="promoted already"):
        promote_here(stores, got.id, [f"receipt:{receipt}"])


def test_a_receipt_from_another_key_or_a_failing_run_earns_nothing(
    repo: Path, runner_key: Ed25519PrivateKey, tmp_path: Path
) -> None:
    stores = Stores.at(repo, tmp_path / "store")
    got = put(stores, order_id="demo").record
    forged = receipt_file(repo, Ed25519PrivateKey.generate(), tmp_path / "f.json")
    with pytest.raises(Bad, match="not the trusted runner key"):
        promote_here(stores, got.id, [f"receipt:{forged}"])
    failed = receipt_file(repo, runner_key, tmp_path / "r.json", exit_code=1)
    with pytest.raises(Bad, match="does not record a passing gate"):
        promote_here(stores, got.id, [f"receipt:{failed}"])
    other = put(stores, statement="Elsewhere.", order_id="other", suffix=suffixes(7))
    good = receipt_file(repo, runner_key, tmp_path / "g.json")
    with pytest.raises(Bad, match="bound elsewhere"):
        promote_here(stores, other.record.id, [f"receipt:{good}"])
    assert not (repo / PROMOTED_DIR / RECORDS_DIR).exists()


def test_a_receipt_taken_at_a_revision_outside_head_earns_nothing(
    repo: Path, runner_key: Ed25519PrivateKey, tmp_path: Path
) -> None:
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "src" / "side.py").write_text("print('side')\n", encoding="utf-8")
    commit_all(repo, "work that never reached main")
    side = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    stores = Stores.at(repo, tmp_path / "store")
    got = put(stores, order_id="demo").record
    receipt = receipt_file(repo, runner_key, tmp_path / "side.json", revision=side)
    with pytest.raises(Bad, match=f"revision {side[:12]} is not in HEAD"):
        promote_here(stores, got.id, [f"receipt:{receipt}"])
    assert not (repo / PROMOTED_DIR / RECORDS_DIR).exists()


def test_a_receipt_at_another_commit_than_the_record_names_earns_nothing(
    repo: Path, runner_key: Ed25519PrivateKey, tmp_path: Path
) -> None:
    first = git(repo, "rev-list", "--max-parents=0", "HEAD")
    head = git(repo, "rev-parse", "HEAD")
    assert first != head
    stores = Stores.at(repo, tmp_path / "store")
    got = put(stores, order_id="demo", commit_sha=first).record
    receipt = receipt_file(repo, runner_key, tmp_path / "head.json")
    with pytest.raises(Bad, match=f"taken at {head[:12]}, the record is about"):
        promote_here(stores, got.id, [f"receipt:{receipt}"])
    assert not (repo / PROMOTED_DIR / RECORDS_DIR).exists()


def test_review_and_approval_evidence_is_checked_not_believed(
    repo: Path, tmp_path: Path
) -> None:
    stores = Stores.at(repo, tmp_path / "store")
    got = put(stores, order_id="demo").record
    with pytest.raises(Bad, match="review:other: the record belongs to order demo"):
        promote_here(stores, got.id, ["review:other"])
    with pytest.raises(Bad, match="review:demo: cannot read the order"):
        promote_here(stores, got.id, ["review:demo"])
    with pytest.raises(Bad, match="approval:Q99"):
        promote_here(stores, got.id, ["approval:Q99"])
    with pytest.raises(Bad, match="the kinds are receipt, review and approval"):
        promote_here(stores, got.id, ["hearsay:x"])


# ---------------------------------------------------------------- index


def promoted_fixture(folder: Path) -> list[Record]:
    records = [
        record(1, topic=["harness.config", "codex.sandbox"], order_id="tac-core"),
        record(2, kind="decision", order_id="tac-memory"),
        record(3, topic=["policy"]),
    ]
    (folder / RECORDS_DIR).mkdir(parents=True)
    for one in records:
        name = f"{one.id.replace(':', '_')}.json"
        (folder / RECORDS_DIR / name).write_text(record_text(one), encoding="utf-8")
    events = [
        review(1, records[0].id, "test-receipt"),
        event(2, "conflict", records[1].id, supersedes_with=records[2].id),
    ]
    (folder / EVENTS_DIR).mkdir()
    (folder / EVENTS_DIR / "2026-09-02.jsonl").write_text(
        "".join(e.line() + "\n" for e in events), encoding="utf-8"
    )
    return records


def test_the_index_is_deterministic_and_byte_identical(tmp_path: Path) -> None:
    folder = tmp_path / PROMOTED_DIR
    records = promoted_fixture(folder)
    store = memory.read_promoted(folder)
    first = build_index(store.records, store.events)
    assert first == build_index(reversed(store.records), reversed(store.events))
    assert write_index(folder) == [*INDEX_FILES, INDEX_README]
    before = {n: (folder / INDEX_DIR / n).read_bytes() for n in first}
    assert write_index(folder) == []
    assert {n: (folder / INDEX_DIR / n).read_bytes() for n in first} == before
    topics = json.loads(before["by-topic.json"])["topics"]
    assert topics == {
        "codex.sandbox": [records[0].id],
        "harness.config": [records[0].id, records[1].id],
        "policy": [records[2].id],
    }
    orders = json.loads(before["by-order.json"])["orders"]
    assert orders == {"tac-core": [records[0].id], "tac-memory": [records[1].id]}
    status = json.loads(before["by-status.json"])["status"]
    assert status["confirmed"] == [records[0].id]
    assert status["conflict"] == [records[1].id, records[2].id]
    readme = before[INDEX_README].decode()
    assert "Records: 3." in readme and "never edit" in readme
    for name in INDEX_FILES:
        text = before[name]
        assert text.endswith(b"\n") and not text.endswith(b"\n\n")
        assert memory.hashlib.sha256(text).hexdigest() in readme


def test_the_checkouts_own_memory_is_empty_and_clean() -> None:
    folder = REPO / PROMOTED_DIR
    assert (folder / "README.md").is_file()
    assert memory.index_problems(folder) == []
    assert memory.read_promoted(folder).records == []


# ---------------------------------------------------------------- lint


def write_lines(path: Path, items: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(i.line() + "\n" for i in items), encoding="utf-8")


def test_lint_names_gaps_repeats_and_dangling_events(stores: Stores) -> None:
    write_lines(live(stores) / RECORDS_FILE, [record(1), record(3), record(3)])
    write_lines(
        live(stores) / EVENTS_FILE,
        [review(1, record(1).id), review(2, "mem:20200101T000000Z:ffff")],
    )
    problems = lint(stores)
    assert f"live/{RECORDS_FILE}: sequence jumps from 1 to 3" in problems
    assert f"live/{RECORDS_FILE}: sequence 3 repeats or goes back" in problems
    assert any("names mem:20200101T000000Z:ffff" in p for p in problems)


def test_add_against_a_promoted_only_record_leaves_lint_clean(
    stores: Stores,
) -> None:
    # A fresh clone: the promoted store holds a confirmed decision the local
    # live journal never saw.
    first = record(1, kind="decision", topic=["x"], statement="Use A.")
    folder = stores.promoted
    (folder / RECORDS_DIR).mkdir(parents=True)
    name = f"{first.id.replace(':', '_')}.json"
    (folder / RECORDS_DIR / name).write_text(record_text(first), encoding="utf-8")
    write_lines(folder / EVENTS_DIR / "2026-09-02.jsonl", [review(1, first.id)])
    write_index(folder)
    assert lint(stores) == []
    added = put(stores, kind="decision", topic=["x"], statement="Use B.")
    [conflict] = added.events
    assert (conflict.kind, conflict.supersedes_with) == ("conflict", first.id)
    assert lint(stores) == []


def test_lint_names_a_record_over_the_cap_and_a_stale_index(tmp_path: Path) -> None:
    folder = tmp_path / PROMOTED_DIR
    promoted_fixture(folder)
    stores = Stores(folder, None)
    problems = lint(stores)
    assert (
        f"{PROMOTED_DIR}/{INDEX_DIR}/by-topic.json: missing; run tac memory index"
        in (problems)
    )
    write_index(folder)
    assert lint(stores) == []
    long = record(9).model_dump(mode="json")
    long["statement"] = "x" * (MAX_STATEMENT + 1)
    long["content_hash"] = memory.content_hash(
        long["statement"], long["topic"], "observation"
    )
    (folder / RECORDS_DIR / "long.json").write_text(json.dumps(long), encoding="utf-8")
    problems = lint(stores)
    assert any(f"over the cap of {MAX_STATEMENT}" in p for p in problems)
    (folder / RECORDS_DIR / "long.json").unlink()
    extra = record(4)
    (folder / RECORDS_DIR / "wrong-name.json").write_text(record_text(extra), "utf-8")
    problems = lint(stores)
    assert any("the file name says otherwise" in p for p in problems)
    assert any("differs from a fresh build" in p for p in problems)


# ---------------------------------------------------------------- secrets scan

POSITIVE = [
    ("private-key-block", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    ("private-key-block", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
    ("aws-access-key-id", "AKIA" + "ABCDEFGHIJKLMNOP"),
    ("github-token", FAKE_GITHUB),
    ("github-token", "gh" + "s_" + "Q" * 36),
    ("github-fine-grained-token", "github_" + "pat_" + "A1" * 20),
    ("anthropic-key", "sk-" + "ant-" + "api03" + "x" * 20),
    ("openai-project-key", "sk-" + "proj-" + "y" * 24),
    ("openai-key", FAKE_OPENAI),
    ("slack-token", "xo" + "xb-" + "1234567890-abc"),
    ("jwt", "eyJ" + "hbGciOiJI" + ".eyJ" + "zdWIiOiIx" + ".abcdefghij"),
    ("bearer-token", "Authorization: Bearer " + "abcdef0123456789xyz"),
    ("assigned-secret", "pass" + "word=" + "hunter2hunter2"),
    ("assigned-secret", "access_to" + "ken=" + "abcdefgh12"),
]

NEGATIVE = [
    "sha256:" + "0f" * 32,
    "commit 1f2e3d4c5b6a79881f2e3d4c5b6a79881f2e3d4c",
    "mem:20260924T142500Z:9f2a",
    "Tokens are rotated monthly; the password policy is in docs/SECURITY.md.",
    "Bearer tokens are short-lived.",
    "token = the runner's single-use dispatch token",
    "sk-short",
]


@pytest.mark.parametrize(("name", "text"), POSITIVE, ids=lambda v: v[:24])
def test_each_pattern_is_found_and_named_without_the_value(
    name: str, text: str
) -> None:
    found = secrets_scan.findings(f"before\n{text}\nafter")
    assert name in [f.pattern for f in found]
    assert all(f.line == 2 for f in found)
    assert all(text not in str(f) for f in found)


@pytest.mark.parametrize("text", NEGATIVE)
def test_hashes_ids_and_prose_are_not_secrets(text: str) -> None:
    assert secrets_scan.findings(text) == []


def test_a_write_that_trips_the_scan_is_refused_by_name_only(stores: Stores) -> None:
    with pytest.raises(Bad) as refused:
        put(stores, statement=f"Use {FAKE_GITHUB} to push.")
    assert "github-token" in str(refused.value)
    assert FAKE_GITHUB not in str(refused.value)
    with pytest.raises(Bad, match="openai-key"):
        put(stores, source_refs=[f"https://example.com/?k={FAKE_OPENAI}"])
    assert not (live(stores) / RECORDS_FILE).exists()


def test_a_secret_in_any_field_of_a_record_is_refused(stores: Stores) -> None:
    changes: list[dict[str, Any]] = [
        {"paths": [f"notes/{FAKE_GITHUB}.md"]},
        {"repository": f"x/{FAKE_GITHUB}"},
    ]
    for change in changes:
        with pytest.raises(Bad) as refused:
            put(stores, **change)
        assert "github-token" in str(refused.value)
        assert FAKE_GITHUB not in str(refused.value)
    assert not (live(stores) / RECORDS_FILE).exists()


def test_a_secret_in_an_events_by_or_note_is_refused(stores: Stores) -> None:
    got = put(stores).record
    for by, note in ((FAKE_GITHUB, None), ("test", f"see {FAKE_OPENAI}")):
        with pytest.raises(Bad) as refused:
            append_event(
                stores,
                kind="decay",
                record=got.id,
                other=None,
                note=note,
                by=by,
                basis=None,
                now=NOW,
                suffix=suffixes(0x50),
            )
        assert "the secrets scan found" in str(refused.value)
        assert FAKE_GITHUB not in str(refused.value)
        assert FAKE_OPENAI not in str(refused.value)
    assert not (live(stores) / EVENTS_FILE).exists()


def test_promote_and_lint_scan_paths_and_event_fields(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Written around `add` and `append_event`, which would have refused them.
    planted = record(1, paths=[f"notes/{FAKE_GITHUB}.md"])
    clean = record(2)
    write_lines(live(stores) / RECORDS_FILE, [planted, clean])
    write_lines(
        live(stores) / EVENTS_FILE,
        [event(1, "decay", clean.id, by=FAKE_GITHUB)],
    )
    stub_evidence(monkeypatch, "owner")
    for rid in (planted.id, clean.id):
        with pytest.raises(Bad) as refused:
            promote_here(stores, rid, ["approval:Q1"])
        assert "github-token" in str(refused.value)
        assert FAKE_GITHUB not in str(refused.value)
    assert not (stores.promoted / RECORDS_DIR).exists()
    problems = lint(stores)
    assert any(planted.id in p and "github-token" in p for p in problems)
    assert any(event(1, "decay", clean.id).id in p for p in problems)
    assert not any(FAKE_GITHUB in p for p in problems)


# JSON writes a newline as a backslash and an n, so a token scanned in its JSON
# form would follow a word character and slip past every \\b-anchored pattern.
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
AFTER_A_BREAK = [
    ("github-token", "Deploy key:\n" + FAKE_GITHUB),
    ("github-token", "key\t" + FAKE_GITHUB),
    ("aws-access-key-id", "The id is\n" + FAKE_AWS),
]


@pytest.mark.parametrize(("name", "text"), AFTER_A_BREAK, ids=["nl", "tab", "aws"])
def test_a_secret_after_a_newline_or_tab_is_refused_by_add(
    stores: Stores, name: str, text: str
) -> None:
    with pytest.raises(Bad) as refused:
        put(stores, statement=text)
    assert name in str(refused.value)
    assert text.split()[-1] not in str(refused.value)
    assert not (live(stores) / RECORDS_FILE).exists()


def test_a_secret_after_a_newline_in_an_event_note_is_refused(stores: Stores) -> None:
    got = put(stores).record
    for note in ("n\n" + FAKE_GITHUB, "n\t" + FAKE_GITHUB):
        with pytest.raises(Bad, match="github-token"):
            append_event(
                stores,
                kind="decay",
                record=got.id,
                other=None,
                note=note,
                by="test",
                basis=None,
                now=NOW,
                suffix=suffixes(0x50),
            )
    assert not (live(stores) / EVENTS_FILE).exists()


def test_promote_and_lint_find_a_secret_after_a_newline(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Written around `add`, which would have refused it.
    planted = record(1, statement="Deploy key:\n" + FAKE_GITHUB)
    write_lines(live(stores) / RECORDS_FILE, [planted])
    stub_evidence(monkeypatch, "owner")
    with pytest.raises(Bad, match="github-token"):
        promote_here(stores, planted.id, ["approval:Q1"])
    assert not (stores.promoted / RECORDS_DIR).exists()
    problems = lint(stores)
    assert any(planted.id in p and "github-token" in p for p in problems)
    assert not any(FAKE_GITHUB in p for p in problems)


@pytest.mark.parametrize(
    "changes",
    [{"by": "x\n" + FAKE_GITHUB}, {"note": "see\n" + FAKE_OPENAI}],
    ids=["by", "note"],
)
def test_lint_scans_the_promoted_events(
    stores: Stores, changes: dict[str, Any]
) -> None:
    kept = record(1)
    (stores.promoted / RECORDS_DIR).mkdir(parents=True)
    (stores.promoted / RECORDS_DIR / f"{kept.id.replace(':', '_')}.json").write_text(
        record_text(kept), encoding="utf-8"
    )
    planted = event(1, "decay", kept.id, **changes)
    write_lines(stores.promoted / EVENTS_DIR / "2026-09-24.jsonl", [planted])
    problems = lint(stores)
    assert any(
        f"{PROMOTED_DIR}/{EVENTS_DIR}" in p and planted.id in p and "looks like" in p
        for p in problems
    )
    assert not any(FAKE_GITHUB in p or FAKE_OPENAI in p for p in problems)


def test_the_cli_refuses_an_event_note_holding_a_secret(
    project: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    base = ["--root", str(project)]
    added = runner.invoke(
        cli,
        [
            "memory",
            "add",
            *base,
            "--repository",
            REPOSITORY,
            "--kind",
            "observation",
            "--scope",
            "project",
            "--statement",
            "A plain claim.",
        ],
    )
    assert added.exit_code == 0, added.output
    [row] = lines(tmp_path / "store" / memory.LIVE_DIR / RECORDS_FILE)
    result = runner.invoke(
        cli,
        [
            "memory",
            "event",
            "decay",
            *base,
            "--record",
            row["id"],
            "--note",
            f"token {FAKE_OPENAI}",
        ],
    )
    assert result.exit_code == 1
    assert "openai-key" in result.output
    assert FAKE_OPENAI not in result.output
    assert not (tmp_path / "store" / memory.LIVE_DIR / EVENTS_FILE).exists()


# ---------------------------------------------------------------- time fields


@pytest.mark.parametrize("name", ["valid_from", "valid_to"])
def test_an_impossible_date_is_refused_on_add(stores: Stores, name: str) -> None:
    with pytest.raises(Bad, match=f"{name}: '2026-02-30T00:00:00Z' is not a real"):
        put(stores, **{name: "2026-02-30T00:00:00Z"})
    assert not (live(stores) / RECORDS_FILE).exists()


def test_an_impossible_date_is_refused_on_a_record_and_an_event() -> None:
    with pytest.raises(ValueError, match=r"created: .* is not a real UTC time"):
        record(1, created="2026-13-01T00:00:00Z")
    with pytest.raises(ValueError, match=r"at: .* is not a real UTC time"):
        event(1, "decay", record(1).id, at="2026-09-31T00:00:00Z")


def test_lint_names_a_promoted_record_with_an_impossible_date(tmp_path: Path) -> None:
    folder = tmp_path / PROMOTED_DIR
    promoted_fixture(folder)
    write_index(folder)
    stores = Stores(folder, None)
    assert lint(stores) == []
    bad = record(5).model_dump(mode="json")
    bad["valid_from"] = "2026-02-30T00:00:00Z"
    name = f"{bad['id'].replace(':', '_')}.json"
    (folder / RECORDS_DIR / name).write_text(json.dumps(bad), encoding="utf-8")
    assert any("is not a real UTC time" in p for p in lint(stores))


# ---------------------------------------------------------------- contracts


def test_the_record_contract_matches_the_model_and_the_strict_subset() -> None:
    committed = (REPO / RECORD_CONTRACT).read_text("utf-8")
    assert committed == schema_text(record_json_schema()), (
        "run: uv run tac memory schema --write"
    )
    schema = json.loads(committed)
    assert strict_subset_problems(schema) == []
    assert set(schema["required"]) == set(Record.model_fields)
    validator = Draft7Validator(schema)
    one = record(1).model_dump(mode="json")
    assert list(validator.iter_errors(one)) == []
    assert list(
        validator.iter_errors({k: v for k, v in one.items() if k != "valid_to"})
    )
    assert list(validator.iter_errors({**one, "basis": "owner"}))


def test_the_session_contract_matches_the_model() -> None:
    committed = (REPO / SESSION_CONTRACT).read_text("utf-8")
    assert committed == schema_text(session_json_schema())


# ---------------------------------------------------------------- the command line


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A copy of this checkout's configuration whose worker store is a tmp path,
    never the git common dir the worktrees share."""
    root = copy_project(tmp_path / "proj")
    replace_in(
        root / ".agents/config.toml",
        'runtime_store = "git-common-dir"',
        f'runtime_store = "{tmp_path / "store"}"',
    )
    return root


def test_the_cli_adds_searches_and_lints_in_the_redirected_store(
    project: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    base = ["--root", str(project)]
    added = runner.invoke(
        cli,
        [
            "memory",
            "add",
            *base,
            "--repository",
            REPOSITORY,
            "--kind",
            "decision",
            "--scope",
            "project",
            "--topic",
            "harness.config",
            "--statement",
            "The knob file is .agents/config.toml.",
            "--launch",
            str(FIXTURES / "launch.json"),
        ],
    )
    assert added.exit_code == 0, added.output
    journal = tmp_path / "store" / memory.LIVE_DIR / RECORDS_FILE
    [row] = lines(journal)
    assert row["provider"] == "anthropic"
    found = runner.invoke(cli, ["memory", "search", *base, "--json", "--text", "KNOB"])
    assert found.exit_code == 0, found.output
    [hit] = [json.loads(x) for x in found.output.splitlines()]
    assert hit["id"] == row["id"] and hit["status"] == "unconfirmed"
    none = runner.invoke(cli, ["memory", "search", *base, "--json", "--kind", "lesson"])
    assert none.output == ""
    linted = runner.invoke(cli, ["memory", "lint", *base])
    assert linted.exit_code == 0, linted.output
    chosen = runner.invoke(
        cli,
        ["select", *base, "--stage", "build", "--repository", REPOSITORY, "--json"],
    )
    assert chosen.exit_code == 0, chosen.output
    body = json.loads(chosen.output)
    assert body["selector_version"] == 1 and body["cap_chars"] == 6000
    assert body["ids"] == []


def test_the_cli_selects_only_this_repositorys_records(project: Path) -> None:
    mine = record(1, kind="decision")
    foreign = record(2, kind="decision", repository="someone/else")
    folder = project / PROMOTED_DIR
    (folder / RECORDS_DIR).mkdir(parents=True, exist_ok=True)
    for one in (mine, foreign):
        name = f"{one.id.replace(':', '_')}.json"
        (folder / RECORDS_DIR / name).write_text(record_text(one), encoding="utf-8")
    write_lines(
        folder / EVENTS_DIR / "2026-09-02.jsonl",
        [review(1, mine.id), review(2, foreign.id)],
    )
    write_index(folder)
    runner = CliRunner()
    base = ["select", "--root", str(project), "--stage", "build", "--json"]
    chosen = runner.invoke(cli, [*base, "--repository", REPOSITORY])
    assert chosen.exit_code == 0, chosen.output
    assert json.loads(chosen.output)["ids"] == [mine.id]
    theirs = runner.invoke(cli, [*base, "--repository", "someone/else"])
    assert json.loads(theirs.output)["ids"] == [foreign.id]
    # No origin remote and no --repository: a refusal, never an unfiltered pick.
    bare = runner.invoke(cli, base)
    assert bare.exit_code == 1
    assert "pass --repository owner/name" in bare.output


def test_the_cli_refuses_a_secret_and_never_prints_it(project: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "memory",
            "add",
            "--root",
            str(project),
            "--repository",
            REPOSITORY,
            "--kind",
            "observation",
            "--scope",
            "project",
            "--statement",
            f"export OPENAI_API_KEY={FAKE_OPENAI}",
        ],
    )
    assert result.exit_code == 1
    assert "openai-key" in result.output
    assert FAKE_OPENAI not in result.output


def test_the_cli_refuses_a_tool_field_in_the_payload(
    project: Path, tmp_path: Path
) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({**draft(), "model": "gpt-9"}), encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "memory",
            "add",
            "--root",
            str(project),
            "--repository",
            REPOSITORY,
            "--payload",
            str(payload),
        ],
    )
    assert result.exit_code == 1
    assert "model is written by the tool" in result.output


def test_a_passing_review_gate_earns_reviewed(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gate itself is tac work's, tested there; this proves promote asks it
    # and maps a pass to `reviewed`, and a finding to a refusal.
    got = put(stores, order_id="demo").record
    monkeypatch.setattr("tac.work.find", lambda oid, _root: oid)
    monkeypatch.setattr("tac.work.base_of", lambda _root, _base: "main")
    monkeypatch.setattr("tac.work.review_gate", lambda _o, _b: ["no review yet"])
    with pytest.raises(Bad, match="review:demo: no review yet"):
        promote_here(stores, got.id, ["review:demo"])
    monkeypatch.setattr("tac.work.review_gate", lambda _o, _b: [])
    done = promote_here(stores, got.id, ["review:demo"])
    assert done.basis == "reviewed"
    assert lint(stores) == []
