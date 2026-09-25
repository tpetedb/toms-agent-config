Skills are linked from `.agents/skills/` into `.claude/skills/` by `tac sync`; they are not committed. The subagents under `.claude/agents/` are rendered from `.agents/config/roles/`, one per role with a seat on Claude.

The sandbox is on and fails closed: commands cannot write `.agents/` or any generated file, and cannot read secrets or the runner's store. Call the deployed checker as `uv run --frozen --no-sync --project .agents tac <command>`.
