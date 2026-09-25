---
name: shared-memory
description: How to read and write the shared memory bus with tac memory. Add a record, search, index, promote, lint, select for a stage, and append events. Covers what never goes in, where provenance comes from and why status is derived. Use before recording a decision, lesson or observation, or when asked what the team already knows.
metadata:
  source: "written for this repository from docs/DESIGN.md section 6 and uv run --frozen tac memory --help"
  author: "the owner (written by Claude on the owner's behalf)"
  licence: "MIT, the owner's own work"
  changes: "new; replaces the shared-memory notes of the owner's earlier projects with the tac memory commands"
---
# Shared memory

Memory is append-only and never rewritten. A record says one thing once; everything that happens to it later (reviewed, superseded, corrected, in conflict) is an event. `status` and `confidence` are derived from the events when read, never stored. `docs/DESIGN.md` section 6 is the design; `uv run --frozen --no-sync --project .agents tac memory --help` is the truth for flags. Below, `tac` means that deployed command (the `just memory` recipe wraps it).

## Two stores

- **The live journal** in the worker store (the git common dir's `agents/` by default, `memory.runtime_store` in `.agents/config.toml`): `records.jsonl`, `events.jsonl` and `quarantine.jsonl`. Agents write here.
- **The promoted store** in the repository: `.agents/memory/records/<id>.json`, `.agents/memory/events/<UTC date>.jsonl` and the index in `.agents/memory/_index/`. Only the trusted runner writes here, through `tac memory promote`, and it reaches main through a pull request like any other change.

## The commands

| Command | What it does |
|---|---|
| `tac memory add --kind decision --scope project --statement "..." --topic harness.config --path .agents/config.toml --source-ref docs/DESIGN.md#3-configuration --launch <launch record>` | Append one record to the live journal. `--payload -` reads a JSON object from stdin; flags override it. |
| `tac memory event review --record <id> --evidence receipt:<path>` | Append one event: `supersede`, `review`, `decay`, `correct` or `conflict`. The only way a record changes. |
| `tac memory search --topic <t> --status confirmed` | Records that match every filter, oldest first; `--store live`, `promoted` or `all`; `--json` for one object per line. |
| `tac memory index` | Rebuild `.agents/memory/_index/` from the promoted store; `--check` exits 1 when it is stale. Never edit the index by hand. |
| `tac memory promote <id> --evidence review:<order>` | Host or runner only. Copies a live record into the promoted store when its recomputed basis, derived status and the secrets scan allow it. Refused inside an agent session. |
| `tac memory lint` | Schema, caps, sequence, event targets, the secrets scan and the index over both stores; one line per finding. `just memory-lint`. |
| `tac select --stage <id> --order <id>` | What a stage prompt receives: the promoted records of this repository, deterministic, decisions first, capped at `memory.select_cap_chars`. A mandatory policy record that does not fit is a refusal, never a truncation. |

## Writing a record

- **Kinds**: observation, decision, lesson, question, contract, component, session-digest, trace-digest. Pick the narrowest.
- **Scope**: project, team, order or session. `paths` and `topic` are what selection ranks on, so name the exact files and a dotted topic.
- **One plain paragraph** in `statement`: the claim, not the story of how you found it.
- **Sources**: `--source-ref` for every document, URL or file the claim rests on.
- **Provenance is never typed by a model.** Provider, harness, model, effort and role come only from the launch record the launcher wrote (`--launch`); a value in the payload is ignored.
- **Basis is recomputed, never claimed.** A record's `confidence.basis` is derived from evidence the writer checks itself: `receipt:<path>` (a passing, signed gate or probe receipt, test-receipt), `review:<order>` (the order's review gate passes, reviewed) or `approval:<id>` (a host-signed approval naming the record, owner). Without evidence it is single-source or inferred, and promotion is refused with the rule named.
- **Change by event, never by edit.** Supersede with a new record and a `supersede` event; the old one gets its `valid_to` through the event. A contradiction is a `conflict` event, and both stay visible until adjudicated.

## Never store

- A secret, token, key, password or the content of an env file. The secrets scan runs on add, on every event and in lint; a hit is refused. A session journal has one audited purge path (`just session purge <id> --reason "..."`).
- Raw provider reasoning or a whole transcript. A stage stores the plan it produced, capped; sessions are journaled by `tac session log`.
- Personal data about anyone, or private names of people, employers or hosts: the repository is public.
- A guess stated as fact. Say what you observed, with its source, and let review raise its basis.
- Anything already in the repository's own files: link it with `--path` or `--source-ref` instead of copying it.

## Reading

Search before you add: `tac memory search --text "<phrase>"` and `--topic`. If a confirmed record already says it, cite its id instead of adding a duplicate; if you disagree with one, add yours and a `conflict` event, never a silent second answer.
