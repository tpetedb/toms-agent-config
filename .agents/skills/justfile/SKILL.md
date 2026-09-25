---
name: justfile
description: Adds and names recipes in the justfile so a task is one reviewable command for a person, a hook, a pipeline gate, CI and an agent. Use for "add a just recipe", "put this in the justfile", "make this a task", or when the same multi-step shell has been composed twice.
allowed-tools: Read Edit Bash(just --list) Bash(just --summary) Bash(just --fmt --check) Bash(just --dump --dump-format json)
metadata:
  source: "https://github.com/tpetedb/vibe-map/blob/main/.agents/skills/justfile/SKILL.md"
  author: "the owner"
  licence: "MIT, the owner's own work"
  changes: "the examples became TAC's own recipes (verify, check, test-one, the deployed tac variable); argument quoting and the pipeline gate contract were added"
---
# Writing a justfile

A justfile holds the commands of a project as named recipes you run with `just RECIPE`. Manual: https://just.systems/man/en/

## When a recipe earns its place

- You have composed the same multi-step shell twice. The third time is a recipe. Why: the steps stop drifting between your terminal, the hook, the pipeline gate and CI.
- A task needs an exact order or exact flags. Put them in the recipe, not in a README sentence, so there is one spelling of the command.
- A task is dangerous. A recipe can carry `[confirm]`, which asks in the terminal before it runs (https://just.systems/man/en/requiring-confirmation-for-recipes.html).
- Do not add a recipe that only wraps one short command you already type by hand; `just` is not an alias file.

## Naming

- One word, lowercase, a verb or the thing produced: `verify`, `check`, `sync`, `changelog`. A hyphen when a second word is needed: `test-one`, `work-check`, `pipeline-plan`.
- Related recipes share a prefix (`work-*`, `runner-*`, `diagrams-*`) and sit under one `# ---- <topic>` comment line in the file.
- A helper that exists only as a dependency starts with `_`, or carries `[private]`, and is left out of `just --list` (https://just.systems/man/en/private-recipes.html).
- The first recipe in the file, or the one with `[default]`, runs when `just` is called with no arguments. Here it is `default`, which prints the list.

## Doc comments are the interface

The comment on the line directly above a recipe is what `just --list` prints next to it (https://just.systems/man/en/documentation-comments.html). Every public recipe gets one sentence saying what it does and not how, and an example when it takes arguments:

```just
# The full gate: lint, format, types, workflow and shell lint, tests, private scan.
verify: lint-ci
    uv run --frozen ruff check .
    uv run --frozen pytest -q

# Run one test file or node id while iterating.
test-one target:
    uv run --frozen pytest -q {{ target }}
```

Parameters may have defaults (`config-check base="":`) and the last one may be variadic with `*` or `+` (https://just.systems/man/en/recipe-parameters.html). A value a person or an issue chose reaches the shell through `quote()` or `[positional-arguments]` with `"$@"`, never spliced into script text. A recipe named after the colon is a dependency and runs first, once per invocation.

## The deployed judge

Recipes that judge (`check`, `pipeline-check`, `work-*`, `human`) run the deployed, non-editable copy through the `tac` variable at the top of the file, `uv run --frozen --no-sync --project .agents tac`. Recipes that test new code run the candidate, `uv run --frozen ...`. Keep that split when you add a recipe: a builder's code never judges itself.

## Never put a secret in a justfile

A justfile is committed and read by everyone and every agent. Keys and tokens go in the env file outside the repository, which the runtime injects; read them as `$NAME` in the recipe body, never as a literal and never as a default parameter value.

## The contract with an agent

- `AGENTS.md` and the skills name recipes, not shell incantations. An agent that reads "run `just verify`" cannot get the flags wrong.
- A pipeline gate's `argv` is `["just", "<recipe>"]` with values appended from `args_from`, run without a shell (`.agents/config/pipelines/`). A recipe a gate names must exist, or `just pipeline-check` refuses the pipeline.
- The hook, the gate, CI and the person call the same recipe, so green locally means green in CI.
- When you add a recipe people run, add its row to `QUICKSTART.md`.

## Before you commit

- `just --list` reads the way you want the project explained.
- `just --fmt --check` exits 0.
- `just -n RECIPE` prints what it would do without doing it.
