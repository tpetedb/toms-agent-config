---
name: hooks-authoring
description: Pitfalls and the proven flow for writing a Claude Code or Codex hook check in TAC. The stdin and heredoc trap, pipe tests with fixtures, exit 2 semantics, loop-proof Stop, one registration rendered for both clients. Use when adding or debugging a hook check or editing .agents/config/hooks.toml.
metadata:
  source: "the owner's dotfiles toolbox, config/claude/skills/claude-hooks-authoring/SKILL.md"
  author: "the owner"
  licence: "MIT, the owner's own work"
  changes: "renamed from claude-hooks-authoring and made generic: the toolbox hook folder and live settings merge became hooks/run.py, .agents/config/hooks.toml, tac sync and tests/test_hooks.py; Codex was added next to Claude; the exit 2 and stop semantics now follow the guard"
---
# Authoring hooks

Every hook a client runs in this repository is rendered by `tac sync` from `.agents/config/hooks.toml` into `.claude/settings.json` and `.codex/hooks.json`, and every one calls the same stamped guard, `.agents/hooks/run.py` (source: `hooks/run.py`), which runs `tac hook run` from the deployed venv. You never write a client's settings by hand and never write a separate script per client. Contracts: https://code.claude.com/docs/en/hooks and https://developers.openai.com/codex/hooks; cite the page and the date you checked in the check's docstring.

## The traps

- **A heredoc eats stdin.** `python3 - <<'PY'` makes the heredoc the script's stdin, so the event JSON is gone and `json.load(sys.stdin)` fails silently. Read the event first (`payload="$(cat)"`), or better, never shell out: the guard reads stdin once in Python.
- **The guard must comply with itself.** A check that refuses a character cannot contain it as a literal; write the codepoint escaped.
- **No jq.** Stock macOS has none; parse JSON in Python.
- **The system Python is 3.9.** `hooks/run.py` is stdlib only and 3.9 compatible; `tests/test_guard_py39.py` runs it under 3.9.
- **A hook runs outside the client's sandbox**, from an absolute interpreter with `python -I` and a fixed environment. Never import from the checkout, never read `PATH` or a project config to decide what to run (build condition C2, `tests/test_hook_isolation.py`).

## Semantics that matter

- **PreToolUse, deny class:** exit 2 refuses the tool call, and the reason reaches the model. A guard that crashes, times out or answers garbage on a deny-class event also refuses: fail closed.
- **Every other event is reminder class:** it cannot undo what happened. A reminder is `additionalContext` at exit 0, since both clients send stderr at exit 0 only to a debug log. On Codex only a refusal replaces a tool result.
- **A timed-out Claude hook lets the call continue.** That is why the guard's `deadline_s` sits under the rendered `timeout_s`, and why the enterprise profile turns native delegation off rather than trust a hook.
- **Stop loops forever** unless gated. Send an agent back at most once per state: the work stop check keys on the order and the tree, and a second stop in the same state ends.
- **A hook is a reminder for a cooperative agent.** An edit through a shell is seen by no file hook. What decides (`just check`, `just work-check <id>`, CI) fails closed on its own.

## The proven flow

1. **Declare it**: add `[checks.<name>]` to `.agents/config/hooks.toml` with `event`, `kind` (deny, reminder, record) and the matcher for each client by the tool names its docs give (`claude = "Edit|Write|MultiEdit|NotebookEdit"`, `codex = "apply_patch"`); an empty matcher means not wired on that client. That one table is the dual registration: `tac sync` renders it for both.
2. **Implement it** in `src/tac/hook.py`, the checker, not in the guard. Keep the guard a thin, hashed launcher.
3. **Pipe-test every branch** the way a client runs it: a subprocess from an absolute interpreter with the event on stdin, for the violation, the clean case and the edge. `tests/test_hooks.py` has the fixtures (`tests/_guard.py`: `guarded`, `tool_event`, `run_guard`); add a case per branch for each client that fires the event.
4. **Render and judge**: `just sync`, then `just check`. The hook commands, the lock and the stamped guard must agree; the guard refuses to run when a file beside it does not match `generated.lock`.
5. **Prove it fires live** on each client with a receipt (`tac receipt client`, run by the owner or the runner). A Python fixture calling `run.py` is never evidence that a client fired it.
