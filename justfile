# Every task a human or an agent runs. `just` lists them.

set shell := ["bash", "-euo", "pipefail", "-c"]

# The deployed checker: non-editable, never synced from inside a session.
tac := "uv run --frozen --no-sync --project .agents tac"

default:
    @just --list

# Install uv if absent and build both environments; safe to rerun.
setup:
    bash bootstrap.sh

# The full gate: lint, format, types, workflow and shell lint, tests, private scan.
verify: lint-ci
    uv run --frozen ruff check .
    uv run --frozen ruff format --check .
    uv run --frozen basedpyright
    uv run --frozen pytest -q
    bash scripts/private_scan.sh

# Lint the workflows and the shell scripts with the locked actionlint and shellcheck.
lint-ci:
    uv run --frozen actionlint
    uv run --frozen shellcheck bootstrap.sh scripts/*.sh

# Named health checks from the deployed copy; non-zero until all pass.
doctor *args:
    {{ tac }} doctor {{ args }}

# Owner only, host only, after Q14: check the bot, then create or update the ruleset.
github-apply *args:
    {{ tac }} github apply {{ args }}

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

# ---- the runner: host only, from the owner's terminal, never an agent session

# Create the controller store, the signing key and the runner's venv; --write-pub writes runner.pub.
runner-init *args:
    {{ tac }} runner init {{ args }}
    {{ tac }} runner install

# Rebuild the runner's own venv in the controller store from the stamped package.
runner-install:
    {{ tac }} runner install

# Serve the runner socket in the foreground from its own venv, until interrupted.
runner:
    "$({{ tac }} runner where)/venv/bin/tac" runner serve

# Is the runner up, and with which key.
runner-status:
    {{ tac }} runner status

# Have the runner probe a client and sign what it saw: just receipt-client claude version
receipt-client harness probe *args:
    {{ tac }} receipt client --harness {{ quote(harness) }} --probe {{ quote(probe) }} {{ args }}

# Print what the next release section would say, from changelog.d/.
changelog:
    uv run --frozen towncrier build --draft --version Unreleased

# ---- work orders: a task is data and its acceptance is a command (work/README.md)

# Scaffold a draft order on this branch: just work-new docs-intro docs "Intro page"
work-new id team title:
    {{ tac }} work new {{ quote(id) }} --team {{ quote(team) }} --title {{ quote(title) }}

# Every order here is readable, and no two active orders own the same file.
work-validate:
    {{ tac }} work validate

# Ownership, then every criterion's command; the result is cached for this exact tree.
work-check id:
    {{ tac }} work check {{ quote(id) }}

# The repair handoff, one bounded builder turn once [work.repair] argv is set, then work-check.
work-repair id:
    {{ tac }} work repair {{ quote(id) }}

# What a reviewer reads, and the commit to put in review.toml.
work-packet id:
    {{ tac }} work packet {{ quote(id) }}

# The review gate: current, by someone else, from the other provider, accepted.
work-review id:
    {{ tac }} work review {{ quote(id) }}

# Checks, review and sign-offs together: is it ready to land.
work-accept id:
    {{ tac }} work accept {{ quote(id) }}

# A goal as launch groups: needs first, disjoint files, the agent budget.
work-plan goal:
    {{ tac }} work plan {{ quote(goal) }}

# Every active order in every worktree.
work-board:
    {{ tac }} work board

# At a release: remove the orders that landed.
work-sweep:
    {{ tac }} work sweep

# Write or refresh the order's one status comment on its issue.
work-post id:
    {{ tac }} work post {{ quote(id) }}

# Read the order's issue: owner and collaborators only, status comments left out.
work-thread id:
    {{ tac }} work thread {{ quote(id) }}

# Say something on the order's issue in a role: just work-say docs-intro manager:docs "..."
work-say id role text:
    {{ tac }} work say {{ quote(id) }} --role {{ quote(role) }} {{ quote(text) }}
