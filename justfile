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

# The labels of .github/labels.yml: --dry-run (default) prints the plan; --apply is the owner's step, on the host.
github-labels *args:
    {{ tac }} github labels {{ args }}

# The labels, issue forms and pull request template hold, judged offline.
github-lint:
    {{ tac }} github lint

# Render every harness file from .agents/ and templates/adapters/, and write the lock.
sync *args:
    {{ tac }} sync {{ args }}

# Judge the generated files against a fresh render and the lock: just check --staged
[positional-arguments]
check *args:
    {{ tac }} check "$@"

# Every pipeline under .agents/config/pipelines/ is sound; each refusal names its reason.
pipeline-check *names:
    {{ tac }} pipeline check {{ names }}

# The order a run takes a pipeline's stages in; nothing runs: just pipeline-plan order
pipeline-plan name:
    {{ tac }} pipeline plan {{ quote(name) }}

# Host or runner only: install the git hooks of hooks/git/prek.toml into this clone.
hooks-install:
    uv run --frozen --no-sync prek install --config hooks/git/prek.toml

# The owner's queue: just human render --check | ask | answer <id> | recap | verify <id>
[positional-arguments]
human *args:
    {{ tac }} human "$@"

# Owner only, host only: sign the decision on an approval item with the runner key.
[positional-arguments]
approve *args:
    {{ tac }} approve "$@"

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

# Run a pipeline for an order as the runner: just run order <id>, --resume <run>, --dry-run
run pipeline order *args:
    {{ tac }} run {{ quote(pipeline) }} --order {{ quote(order) }} {{ args }}

# Start a role's session the way the runner does: just launch claude --role chief --print
launch harness *args:
    {{ tac }} launch {{ quote(harness) }} {{ args }}

# Perform the effects the worker inbox asks for, re-checked: just inbox-watch --once
inbox-watch *args:
    {{ tac }} inbox watch {{ args }}

# Each provider's usage reading: ok, slow, stop or unavailable; exits 3 while spawning pauses.
usage *args:
    {{ tac }} usage {{ args }}

# Host only: move the file key into the login keychain and store the tac-bot token.
runner-keychain:
    {{ tac }} runner keychain import

# Acceptance 6: the launch and guard-failure tests with no skip, then both clients' commands.
adapter-proof:
    #!/usr/bin/env bash
    set -euo pipefail
    out="$(PY_COLORS=0 uv run --frozen pytest -q -rs tests/test_launch.py tests/test_guard_failure.py 2>&1)" || { echo "$out"; exit 1; }
    echo "$out"
    if echo "$out" | tail -n 1 | grep -Eq '[0-9]+ skipped'; then echo "adapter-proof: a test skipped" >&2; exit 1; fi
    uv run --frozen tac launch claude --role builder --print
    uv run --frozen tac launch codex --role builder --print

# ---- proof: acceptance 8, the offline part now, the live probes once the owner's steps are done (docs/DESIGN.md, 13)

# The ledger end to end, the bypass attempts, the mutation runs, then one SKIP line per live probe with its reason; --require-live turns a skip into a failure.
dev-proof *args:
    uv run --frozen tac proof dev {{ args }}

# Every guard, gate, hook, stage, handoff, memory, human and layer condition maps to a test that ran; a missing entry or a failed test exits non-zero.
coverage-proof *args:
    uv run --frozen tac proof coverage {{ args }}

# Every effective configuration value and the file it came from.
config-show:
    {{ tac }} config show

# One key's value, source file and line, when it applies, and its comment: just explain profile.active
explain key:
    {{ tac }} explain {{ quote(key) }}

# Load and cross-check the configuration; with a base, the floor may only tighten: just config-check origin/main
config-check base="":
    {{ tac }} config check {{ if base == "" { "" } else { "--base " + quote(base) } }}

# Start the chief as config/models.toml seats it (model, effort, ultracode), read through tac: just chief --resume
chief *args:
    {{ shell(tac + " config launch-command") }} {{ args }}

# ---- the gate pack for project.kind = "tool" (.agents/config/gates/tool.toml)

# The installed command prints its version and its help and exits 0.
gate-cli-smoke:
    uv run --frozen tac --version
    uv run --frozen tac --help > /dev/null

# The package builds into a wheel from a clean tree, in a throwaway folder.
gate-wheel:
    out="$(mktemp -d)"; uv build --wheel --out-dir "$out"; rm -rf "$out"

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

# ---- memory: the append-only bus (docs/DESIGN.md, 6)

# The memory bus: just memory add | event | search | index | promote | schema
[positional-arguments]
memory *args:
    {{ tac }} memory "$@"

# What a stage prompt receives from memory: just select --stage build --order <id>
[positional-arguments]
select *args:
    {{ tac }} select "$@"

# The session journal, run by the launcher: just session log | purge <id> --reason "..."
[positional-arguments]
session *args:
    {{ tac }} session "$@"

# Every record and event parses, the index is current, no secret-like line, no sequence gap.
memory-lint:
    {{ tac }} memory lint
