"""A second `tac sync` writes nothing: the renders, the lock and the skill links
are a function of the inputs alone, so syncing twice never shows up as a diff."""

from __future__ import annotations

from pathlib import Path

from tac.sync import check_tree, sync
from tests._syncproject import synced


def test_a_second_sync_writes_nothing_and_checks_clean(tmp_path: Path) -> None:
    root = synced(tmp_path)
    assert sync(root) == []
    assert check_tree(root) == []


def test_a_second_sync_leaves_every_file_byte_for_byte(tmp_path: Path) -> None:
    root = synced(tmp_path)
    before = {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
    sync(root)
    after = {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
    assert after == before
