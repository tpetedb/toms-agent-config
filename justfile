# Every task a human or an agent runs. `just` lists them.

set shell := ["bash", "-euo", "pipefail", "-c"]

# The deployed checker: non-editable, never synced from inside a session.
tac := "uv run --frozen --no-sync --project .agents tac"

default:
    @just --list

# Install uv if absent and build both environments; safe to rerun.
setup:
    bash bootstrap.sh

# The full gate: lint, format, types, tests, private scan.
verify:
    uv run --frozen ruff check .
    uv run --frozen ruff format --check .
    uv run --frozen basedpyright
    uv run --frozen pytest -q
    bash scripts/private_scan.sh

# Named health checks from the deployed copy; non-zero until all pass.
doctor *args:
    {{ tac }} doctor {{ args }}

# Refuse private terms in tracked and new files.
private-scan:
    bash scripts/private_scan.sh

# Run one test file or node id while iterating.
test-one target:
    uv run --frozen pytest -q {{ target }}

# Host only: copy src/tac into .agents/lib/tac and rebuild the deployed venv.
stamp-lib:
    uv run --frozen python scripts/stamp_lib.py
    uv lock --project .agents
    uv sync --frozen --no-editable --project .agents

# Print what the next release section would say, from changelog.d/.
changelog:
    uv run --frozen towncrier build --draft --version Unreleased
