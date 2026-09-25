# The harness, for its operator

This page is for whoever runs TAC day to day: the owner, or an operator acting for them. It says where things are, how to change them, how to tell that they hold, and what to do when they do not. [QUICKSTART.md](../QUICKSTART.md) has the tables; [DESIGN.md](DESIGN.md) has the reasons.

## The short version

- **One authored tree, many harnesses.** Everything an agent obeys is written once under [`.agents/`](../.agents/) and rendered by `tac sync` into each client's own files (`CLAUDE.md`, `AGENTS.md`, `.claude/`, `.codex/`). Never edit a rendered file; edit `.agents/` and sync.
- **One judge.** `tac check` re-renders in memory and refuses any drift, hand edit or stale lock. It runs from the deployed, non-editable copy in `.agents/.venv`, so code an agent writes never judges itself. CI judges again with the base revision's copy.
- **Work is an order.** A task is a work order with the files it owns and acceptance criteria that are commands; a different agent on the other provider reviews it, and the owner lands it ([ADR 0001](adr/0001-work-orders.md)).
- **Only the owner decides what loosens.** Questions and approvals go to [TODO.HUMAN.md](../TODO.HUMAN.md); a ticked box is a hint, never consent. Merges, releases and policy changes are the owner's own action.

## Where everything is

```text
.agents/                 the authored tree, read-only to agents
  config.toml            the one knob file; points at config/
  config/                profiles, roles, models, teams, pipelines, hooks, policy, github
  skills/                one folder per skill, with provenance (ADR 0003)
  hooks/run.py           the stamped hook guard every client hook runs
  lib/tac/               the stamped copy of src/tac the judge is built from
  generated.lock         sha256 of every input and rendered output
  memory/                promoted memory records, events and the index
src/tac/                 the tac package: CLI, checker, runner, memory, work orders
templates/               adapter templates, handoff prompts, the question bank
contracts/               JSON Schemas: handoffs, config, receipts, memory
hooks/                   the source of the guard and the git hooks
docs/                    DESIGN.md, this page, adr/, diagrams/
.human/                  approvals and recaps; TODO.HUMAN.md is rendered from it
work/                    work orders, goals and templates
justfile                 every command a person, a hook or CI runs
mise.toml                the pinned CLIs: uv, just, mermaid-cli
```

The lifecycles are drawn in [docs/diagrams/](diagrams/README.md): [init](diagrams/lifecycle-init.mmd), [sync](diagrams/lifecycle-sync.mmd), [hook](diagrams/lifecycle-hook.mmd), [memory](diagrams/lifecycle-memory.mmd), [board](diagrams/lifecycle-board.mmd), [human loop](diagrams/lifecycle-human-loop.mmd) and [release](diagrams/lifecycle-release.mmd), and each pipeline has its own.

## How to change a knob

Use the "Where to tweak" table in [QUICKSTART.md](../QUICKSTART.md#where-to-tweak): it names the file, the key and the check for every knob, and it is the one list kept current. Three rules hold for every row:

- `just explain <key>` prints a key's value, its file and line, when it applies and its comment, before you change it.
- An unknown key is refused, not ignored. A new knob is added to the schema in `src/tac/config_schema.py` first, and CI judges configuration with the base revision's checker, so the key lands in a release before any file sets it.
- After the change: `just config-check`, then `just sync` and `just check`. A change to `.agents/` reaches main through a pull request the owner reviews (CODEOWNERS).

## How to check

| Recipe | Green looks like |
|---|---|
| `just verify` | ruff, formatting, types, workflow and shell lint, every test and the private scan pass; the last line of pytest says `passed` with no `failed` |
| `just check` | `generated files match the lock` |
| `just config-check` | `config holds: profile <name>, kind <kind>, <n> values` |
| `just pipeline-check` | `pipelines sound:` and the list of pipelines |
| `just human render --check` | `TODO.HUMAN.md matches its render` |
| `just skills-lint` | every skill passes the portable subset, provenance and the listing budget |
| `just diagrams-check` | `the diagrams match the inventory and render.lock` |
| `just diagrams-render` | one `rendered` line per diagram, then `wrote docs/diagrams/render.lock`; needs node 22.13 or later |
| `just doctor` | every named check `ok`; exits non-zero until all pass |
| `just work-check <id>` | the order's ownership and every criterion pass, printed as OK |

The full table, with what each check proves, is "Checks that tell you it worked" in [QUICKSTART.md](../QUICKSTART.md#checks-that-tell-you-it-worked).

## How to recover

**Drift: `just check` names a file.** A rendered file was edited by hand or an input changed without a sync. Run `just sync`, read the diff, and commit the inputs and the outputs together. If `just check` says a lock line was edited by hand, find who wrote it: only `tac sync` writes `generated.lock`.

**A diagram changed since the last render.** `just diagrams-check` names the source. Run `just diagrams-render` (it needs node) and commit the refreshed `docs/diagrams/render.lock` with the source.

**A stale review.** A review counts only while neither the order nor the branch's own contribution to its files has changed since the commit it names. `just work-review <id>` says why it does not count. Ask the reviewer for a delta review (the `review` pipeline, or `just work-packet <id>` for a person) and let them write a fresh `review.toml`; never edit the old one.

**A refused receipt.** CI and the runner refuse a receipt signed for another repository, revision, run or stage, a replayed one, or one checked against a key that is not the trusted `runner.pub`. Do not re-sign by hand: run the gate again through the runner at the current revision (`just run <pipeline> <order> --resume <run>`), so it signs what it observes now.

**The runner is down.** `just runner-status` says whether the socket answers and with which key. The owner starts it in their own terminal with `just runner`; agents never start or stop it. While it is down, dispatched sessions refuse every spawn by design (build condition C1), and nothing is lost: runs resume from their checkpoint.

**A missing trust root.** Without `.agents/config/runner.pub` every receipt is refused and `just doctor` fails its `runner-pub` check. The owner provisions it once (step S5 of [TODO.HUMAN.md](../TODO.HUMAN.md)) and lands the public key through a pull request. A client that does not load the rendered hooks usually lacks its own trust: Codex project trust and `/hooks`, Claude workspace trust (steps S1 to S3).

**Anything else.** `just doctor` names the failing check. Ask on the order's issue, or add an item with `just human ask` so the owner sees it in `TODO.HUMAN.md`.

## How to try it on another machine

1. Clone the repository and enter it.
2. `mise trust && mise install`: uv, just and mermaid-cli at the pinned versions. Rendering diagrams also needs node 22.13 or later on `PATH`.
3. `bash bootstrap.sh`: installs uv when absent and reports whether mise is there (it never installs mise; get it from [mise.jdx.dev](https://mise.jdx.dev)). It builds `.venv` for the tests and `.agents/.venv` from the stamped copy with `uv sync --frozen --no-editable --project .agents`, builds the runner venv with `tac runner install` when it runs outside an agent session, and runs `tac doctor`. `tac init` does not run yet: the script says it is skipped until it arrives with tac-core.
4. Work through the S-steps of [TODO.HUMAN.md](../TODO.HUMAN.md) that apply to this machine: client trust (S1 to S3), `mise trust` and `just doctor` (S6), the git hooks (S7). The runner key (S5) and the GitHub steps (S4, S9) are once per repository, not per machine.
5. `just verify`, then `just doctor`. Both green means the machine is ready.
