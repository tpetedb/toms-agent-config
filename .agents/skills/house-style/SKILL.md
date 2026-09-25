---
name: house-style
description: The house style for Python with uv, SQL, bash 3.2 shell, Markdown docs and commit messages. Use when writing or editing a .py, .sh, .sql or .md file, a justfile recipe, or a commit message in a repository that uses TAC.
metadata:
  source: "the owner's dotfiles toolbox, config/claude/skills/toms-style/SKILL.md"
  author: "the owner"
  licence: "MIT, the owner's own work"
  changes: "renamed from toms-style; the toolbox-only paths (bin/, templates/utils/) and the personal voice section were dropped; the rules now follow this repository's standards.toml: Python 3.12, ruff at 88, basedpyright basic, no em dash, the one-line what-and-why commit with trailers"
---
# House style

Match the surrounding file first; these rules are the default where a file has no style of its own. `.agents/config/standards.toml` is the machine-readable version, and `just verify` holds most of it.

## Attitude

- Reason over ritual. Every abstraction, lint rule and dependency needs a why. A dependency is welcome when it removes real work.
- Validate at boundaries, fail loudly with a message that names the offending value; trust internal invariants once established.
- Honest critique first. Name the trade-off rather than pretend there is one obvious path.
- Precise names: "YAML", not "yml"; the product's own spelling of a name.

## Python

- Python 3.12, `from __future__ import annotations` at the top of every module, a `__main__` guard on a script.
- `uv` for environments and dependencies: `uv sync`, `uv run --frozen`, `uv add`. Never bare pip. `pyproject.toml` is the one source; `uv.lock` is committed.
- `ruff check` and `ruff format` at line length 88; `basedpyright` in basic mode. `just verify` runs all three.
- Builtin generics and unions: `list[str]`, `dict[str, int] | None`. Never `List`, `Dict`, `Optional` or `Union` from `typing`.
- Frozen, slotted dataclasses for plain data; pydantic models with `extra = "forbid"` where input is validated (every config file is read that way here).
- `pathlib.Path` everywhere. Configs are inputs passed as parameters, never globals. Pass clocks, sessions and loggers in rather than reaching for them.
- Specific exceptions with a clear message. `except Exception` only with a comment saying why the wide net.
- Keyword-only arguments (`*`) where the call site reads better for it.
- Docstrings say what and why in a few lines; a tiny private helper needs none. Comments state a constraint or a reason, never narrate a change, never carry a date or a line number from another file.
- Tests: pytest, named `test_<what holds>`, fixtures over mocks, real files in `tmp_path`. A test never sleeps on wall-clock time; it waits on a state it can observe. Network tests are marked `integration`.

## SQL

- Keywords in one case throughout a file (match the file), one clause per line, a comment above each query saying the question it answers.
- Fully qualified identifiers; no `USE` state leaking between scripts.
- Explicit column lists in `SELECT`, `INSERT` and `MERGE`; no `SELECT *` in committed code.
- Bind parameters (`%s`, `?`, `:name`); never format untrusted input into SQL.
- Store UTC, convert at display.

## Bash

- Bash 3.2, the one macOS ships: no associative arrays, no `mapfile`, no `${var,,}`.
- `set -euo pipefail` at the top. Quote every expansion. `shellcheck`-clean (`just lint-ci`).
- `--help` exits 0 and prints the usage; a health-check or `--dry-run` mode where it makes sense.
- Idempotent: safe to rerun; skip what is done rather than tear down and redo.
- A value a person or an issue chose reaches a command as an argument or a variable, never as script text.

## Markdown and docs

- One H1, the title. H2 for sections. Tables for structured facts, bullets for steps, prose for reasoning.
- No em dash anywhere: use a comma, a colon or a full stop. No emoji or pictographs.
- A language tag on every code fence. Link a source the first time it is named.
- ISO 8601 for every date, UTC for every time, the basic form (`20260925T1425Z`) in file names.
- Diagrams follow the `mermaid-diagrams` skill.

## Commit messages

- One line saying what changed and why, at most 72 characters, present tense. Below it, after one blank line, only trailers: an agent's commit carries `Co-Authored-By` and the session line the run names. `tac check --commit-msg` judges it.
- No em dash, no emoji.
- One logical change per commit; the changelog fragment travels in the same commit (the `changelog` skill).

## Never

- Emoji in code, commits, docs or reports.
- `except Exception: pass` without a reason.
- String-built SQL with user input.
- Wildcard imports.
- A 200-word docstring on a five-line helper.
