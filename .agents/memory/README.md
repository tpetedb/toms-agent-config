# .agents/memory

The promoted memory of this repository (design section 6). Every file here is
written by `tac memory` and never by hand:

- `records/<id>.json`: one promoted record each, copied from the live journal
  by `tac memory promote` once its basis is test-receipt, reviewed or owner,
  its derived status is confirmed and the secrets scan finds nothing. A record
  is never edited after it lands; supersession and correction are events.
- `events/<UTC date>.jsonl`: the append-only events about promoted records
  (supersede, review, decay, correct, conflict), one JSON line each.
- `_index/`: `by-topic.json`, `by-order.json`, `by-status.json` and a README
  with the count and each file's sha256, rebuilt by `tac memory index`.
  Status and confidence are derived there from the events, never stored on a
  record.

The live journal is not here: it sits in the worker store under the git
common directory (`memory/records.jsonl`, `events.jsonl`, `quarantine.jsonl`),
shared by every worktree and never committed. `just memory-lint` checks both.
Promotion reaches main through a pull request like any other change.
