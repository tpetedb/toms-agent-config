"""The selector of design section 6: deterministic, pure, capped, and never
truncating a mandatory policy record. Each ordering rule is proved on its own
with hand-made records that differ in that one respect."""

from __future__ import annotations

import random

import pytest

from tac.memory import SELECTOR_VERSION, Event, Record, Selection, render_line, select
from tac.work import Bad
from tests.fixtures.memory.build import NOW_TEXT, REPOSITORY, event, record, review

CAP = 6000


def run(
    records: list[Record],
    events: list[Event],
    *,
    order: str | None = None,
    paths: tuple[str, ...] = (),
    cap: int = CAP,
    topics: tuple[str, ...] = (),
    now: str = NOW_TEXT,
) -> Selection:
    return select(
        records,
        events,
        stage="build",
        order=order,
        paths=paths,
        cap_chars=cap,
        now=now,
        topics=topics,
        repository=REPOSITORY,
    )


def confirmed(*records: Record, basis: str = "reviewed") -> list[Event]:
    return [review(100 + i, r.id, basis) for i, r in enumerate(records)]


def test_decisions_come_first() -> None:
    lesson = record(1, kind="lesson")
    decision = record(2, kind="decision")
    chosen = run([lesson, decision], confirmed(lesson, decision))
    assert chosen.ids == (decision.id, lesson.id)


def test_an_exact_path_match_with_the_orders_owns_ranks_next() -> None:
    other = record(1, paths=["src/other.py"])
    owned = record(2, paths=["src/tac/memory.py"])
    chosen = run([other, owned], confirmed(other, owned), paths=("src/tac/memory.py",))
    assert chosen.ids == (owned.id, other.id)
    # A folder the order owns is not an exact match for a file under it.
    folder = run([other, owned], confirmed(other, owned), paths=("src/",))
    assert folder.ids == (other.id, owned.id)


def test_the_exact_order_ranks_after_the_path() -> None:
    elsewhere = record(1, order_id="tac-core")
    mine = record(2, order_id="tac-memory")
    chosen = run([elsewhere, mine], confirmed(elsewhere, mine), order="tac-memory")
    assert chosen.ids == (mine.id, elsewhere.id)
    by_path = record(3, paths=["a.py"], order_id="tac-core")
    chosen = run(
        [mine, by_path],
        confirmed(mine, by_path),
        order="tac-memory",
        paths=("a.py",),
    )
    assert chosen.ids == (by_path.id, mine.id)


def test_the_evidence_basis_ranks_test_receipt_then_reviewed_then_owner() -> None:
    owner, reviewed, receipt = record(1), record(2), record(3)
    events = [
        review(10, owner.id, "owner"),
        review(11, reviewed.id, "reviewed"),
        review(12, receipt.id, "test-receipt"),
    ]
    chosen = run([owner, reviewed, receipt], events)
    assert chosen.ids == (receipt.id, reviewed.id, owner.id)


def test_recency_ranks_after_the_basis() -> None:
    old = record(1, valid_from="2026-09-01T00:00:00Z")
    new = record(2, valid_from="2026-09-10T00:00:00Z")
    chosen = run([old, new], confirmed(old, new))
    assert chosen.ids == (new.id, old.id)


def test_the_id_is_the_stable_tiebreak() -> None:
    a, b = record(1), record(2)
    assert run([b, a], confirmed(a, b)).ids == (a.id, b.id)


@pytest.mark.parametrize("seed", range(5))
def test_the_same_input_in_any_order_selects_the_same_bytes(seed: int) -> None:
    records = [
        record(1, kind="decision", paths=["a.py"]),
        record(2, order_id="tac-memory"),
        record(3, valid_from="2026-09-10T00:00:00Z"),
        record(4, kind="lesson"),
        record(5),
    ]
    events = confirmed(*records)
    first = run(records, events, order="tac-memory", paths=("a.py",))
    shuffled_records, shuffled_events = records[:], events[:]
    random.Random(seed).shuffle(shuffled_records)
    random.Random(seed + 100).shuffle(shuffled_events)
    again = run(shuffled_records, shuffled_events, order="tac-memory", paths=("a.py",))
    assert again == first
    assert again.text.encode() == first.text.encode()


def test_only_confirmed_and_valid_records_are_selected() -> None:
    kept = record(1)
    unreviewed = record(2)
    weak = record(3, source_refs=["docs/DESIGN.md"])
    expired = record(4, valid_to="2026-09-10T00:00:00Z")
    future = record(5, valid_from="2026-12-01T00:00:00Z")
    old, new = record(6), record(7, statement="The replacement.")
    events = [
        *confirmed(kept, expired, future, old, new),
        review(20, weak.id, "single-source"),
        event(
            30, "supersede", old.id, supersedes_with=new.id, at="2026-09-06T00:00:00Z"
        ),
    ]
    chosen = run([kept, unreviewed, weak, expired, future, old, new], events)
    # The same basis and valid_from: the id breaks the tie.
    assert chosen.ids == (kept.id, new.id)
    # As of before the supersession and the expiry, the old ones are back.
    earlier = run([kept, expired, old, new], events, now="2026-09-05T00:00:00Z")
    assert set(earlier.ids) == {kept.id, expired.id, old.id, new.id}


def test_a_conflict_is_left_out_until_one_side_is_closed() -> None:
    a = record(1, kind="decision", statement="Use A.")
    b = record(2, kind="decision", statement="Use B.")
    events = [*confirmed(a, b), event(40, "conflict", b.id, supersedes_with=a.id)]
    assert run([a, b], events).ids == ()
    closed = [*events, event(41, "correct", a.id, supersedes_with=b.id)]
    assert run([a, b], closed).ids == (b.id,)


def test_scope_repository_and_topic_filter() -> None:
    project = record(1)
    session = record(2, scope="session")
    mine = record(3, scope="order", order_id="tac-memory")
    theirs = record(4, scope="order", order_id="tac-core")
    foreign = record(5, repository="example/other")
    off_topic = record(6, topic=["codex.sandbox"])
    everything = [project, session, mine, theirs, foreign, off_topic]
    chosen = run(everything, confirmed(*everything), order="tac-memory")
    assert set(chosen.ids) == {project.id, mine.id, off_topic.id}
    narrowed = run(
        everything,
        confirmed(*everything),
        order="tac-memory",
        topics=("harness.config",),
    )
    assert set(narrowed.ids) == {project.id, mine.id}


def test_the_cap_keeps_whole_lines_and_counts_them() -> None:
    records = [record(n, statement=f"{'x' * 50} {n}.") for n in range(1, 6)]
    line = len(render_line(records[0], "reviewed"))
    chosen = run(records, confirmed(*records), cap=line * 3 + 1)
    assert len(chosen.ids) == 3
    assert chosen.chars == len(chosen.text) == line * 3
    assert chosen.chars <= chosen.cap_chars
    assert chosen.text.count("\n") == 3


def test_a_record_that_does_not_fit_is_skipped_and_filling_goes_on() -> None:
    long = record(1, kind="decision", statement="l" * 200)
    short = record(2, kind="lesson", statement="Short.")
    room = len(render_line(short, "reviewed"))
    assert len(render_line(long, "reviewed")) > room
    chosen = run([long, short], confirmed(long, short), cap=room)
    # The decision ranks first but does not fit; the lesson after it still does.
    assert chosen.ids == (short.id,)


def test_the_text_is_one_line_per_record_with_its_id_and_basis() -> None:
    one = record(1, kind="decision", statement="The knob file\nis config.toml.")
    chosen = run([one], [review(10, one.id, "test-receipt")])
    assert chosen.text == (
        f"- [decision] The knob file is config.toml. ({one.id}, test-receipt)\n"
    )


def test_a_mandatory_policy_record_is_never_truncated_but_refused() -> None:
    policy = record(1, topic=["policy"], statement="p" * 300)
    with pytest.raises(Bad) as refused:
        run([policy], confirmed(policy), cap=100)
    assert policy.id in str(refused.value)
    assert "never truncated" in str(refused.value)


def test_a_mandatory_record_has_its_room_before_any_other() -> None:
    decision = record(1, kind="decision", statement="d" * 60)
    policy = record(2, topic=["policy"], statement="p" * 60)
    room = len(render_line(policy, "reviewed"))
    chosen = run([decision, policy], confirmed(decision, policy), cap=room)
    assert chosen.ids == (policy.id,)


def test_the_selection_carries_the_selector_version_and_the_hashes() -> None:
    a, b = record(1), record(2)
    chosen = run([a, b], confirmed(a, b))
    assert chosen.selector_version == SELECTOR_VERSION == 1
    assert chosen.content_hashes == (a.content_hash, b.content_hash)
    body = chosen.as_dict()
    assert body["selector_version"] == 1
    assert body["ids"] == [a.id, b.id]


def test_an_unknown_selector_version_is_refused() -> None:
    with pytest.raises(Bad, match="selector_version 2 is unknown"):
        select(
            [],
            [],
            stage="build",
            order=None,
            paths=(),
            cap_chars=CAP,
            now=NOW_TEXT,
            selector_version=2,
        )
