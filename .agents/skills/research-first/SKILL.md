---
name: research-first
description: Reads authoritative first-party docs, and looks for an existing offering, before a design decision or new tooling. Use before choosing an approach for CI, a data pattern, the Python, uv or pytest toolchain, a config key, or before writing a new skill, hook, agent or MCP server.
metadata:
  source: "the owner's dotfiles toolbox, config/claude/skills/research-first/SKILL.md"
  author: "the owner"
  licence: "MIT, the owner's own work"
  changes: "Codex, the Agent Skills specification and this repository's config checker were added to the sources; the rule on recording a verified claim with its URL and date was added"
---
# Research first, build second

Before committing to a design decision or building new tooling, check the source of truth, and check whether someone already built it. Getting the conventions right matters more than moving fast.

## Consult the authoritative source before deciding

Read the canonical docs before reasoning from memory. Editors, CI systems and config-driven tools silently ignore unknown keys, so a wrong assumption looks fine and does nothing. Use a documentation MCP server when one is enabled, otherwise fetch the first-party docs:

- Claude Code: https://code.claude.com/docs/en/overview
- Codex: https://developers.openai.com/codex
- Agent Skills specification: https://agentskills.io/specification
- Python and PEP 8: https://www.python.org/ and https://peps.python.org/pep-0008/
- uv: https://docs.astral.sh/uv/
- pytest: https://docs.pytest.org/en/stable/
- Whatever CI, database or cloud platform the project targets: its vendor docs, not a blog post.

For a non-trivial decision (a CI architecture, a schema migration pattern, a client's behaviour), read two or more sources and verify the key claim against a primary source before acting on it.

## Record what you verified

- Write the URL and the date you checked it next to the claim: in the docstring of the test that depends on it, in the ADR, or in `docs/DESIGN.md`. A number a test asserts (a budget, a limit, a version) cites where it came from.
- A client version or a documented default can change under you. Where it matters, a receipt or a test pins the version the claim was checked against.
- In this repository a config key that is not in `src/tac/config_schema.py` is refused by `tac config check`; never add a key to `.agents/config/` to find out whether a client reads it.

## Prefer an existing offering before building your own

Check for a first-party or vendor skill, hook, agent, tool or MCP server before you hand-roll one:

- Documentation lookup: a documentation MCP server (read-only, network only).
- Code and pull request review: the client's own review command, plus this repository's reviewer role.
- Secret scanning: this repository's `tac` secrets scan and private scan run deterministically; an LLM reviewer is a backstop, not a replacement.
- Hook authoring: the `hooks-authoring` skill and `hooks/run.py`.

If you build something bespoke instead of adopting an offering, say why in one line. The choice is the owner's to confirm.
