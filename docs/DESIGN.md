# Design: toms-agent-config

Status: approved by the design board on 2026-09-24. All three directors returned "go with changes"; the six build conditions from their final verdicts are folded into the milestones of section 18 as acceptance items, and no milestone is accepted while one of its conditions is open. Nothing described here is built yet. How the design was decided is in [BOARD.md](BOARD.md); the owner's decisions are in [TODO.HUMAN.md](../TODO.HUMAN.md) and are referred to below by their ids (Q1 to Q22); those taken on 2026-09-25 on the owner's delegation are recorded in [ADR 0002](adr/0002-owner-decisions-2026-09-25.md).

The short name is TAC; the command-line tool is `tac`.

## Contents

- [Diagram conventions](#diagram-conventions)
- [1. Overview](#1-overview)
- [2. The trees](#2-the-trees)
- [3. Configuration](#3-configuration)
- [4. Roles, identities, sandboxes](#4-roles-identities-sandboxes)
- [5. Pipelines and the handoff envelope](#5-pipelines-and-the-handoff-envelope)
- [6. The memory bus](#6-the-memory-bus)
- [7. The human loop](#7-the-human-loop)
- [8. The GitHub gate](#8-the-github-gate)
- [9. Enforcement at three layers](#9-enforcement-at-three-layers)
- [10. The trust boundary at run time](#10-the-trust-boundary-at-run-time)
- [11. Containers, secrets, the toolchain environment](#11-containers-secrets-the-toolchain-environment)
- [12. Skills and telemetry](#12-skills-and-telemetry)
- [13. CLI, init, validation, dev/](#13-cli-init-validation-dev)
- [14. Docs and diagrams](#14-docs-and-diagrams)
- [15. The toolchain](#15-the-toolchain)
- [16. Reuse list with licences](#16-reuse-list-with-licences)
- [17. Trade-offs resolved](#17-trade-offs-resolved)
- [18. Releases and milestones](#18-releases-and-milestones)
- [19. Risks and what is left out](#19-risks-and-what-is-left-out)
- [References](#references)

## Diagram conventions

Every diagram in this repository is a Mermaid flowchart that uses a house subset of the [ISO 5807:1985](https://www.iso.org/standard/11955.html) flowchart symbols, with one colour per symbol and a legend. Architecture views follow [ISO/IEC/IEEE 42010:2022](https://www.iso.org/standard/74393.html) (stakeholders, concerns, viewpoints, views), with C4 as the presentation form.

```mermaid
flowchart LR
    t(["terminator: start or end"])
    p["process: a step"]
    d{"decision: a branch"}
    io[/"input or output"/]
    st[("data store: file, config, database")]
    x[/"stop: blocked, refused, escalated"/]
    classDef term fill:#00A86B,stroke:#00D084,color:#000000
    classDef proc fill:#0067A5,stroke:#0088CC,color:#FFFFFF
    classDef dec fill:#FFBF00,stroke:#FFD500,color:#000000
    classDef io fill:#FF8C1A,stroke:#FFA94D,color:#000000
    classDef store fill:#9A2A2A,stroke:#F04923,color:#FFFFFF
    classDef stop fill:#D32F2F,stroke:#F04923,color:#FFFFFF
    class t term
    class p proc
    class d dec
    class io io
    class st store
    class x stop
```

| Shape | Mermaid | ISO 5807 meaning | Colour |
|---|---|---|---|
| Stadium | `([ ])` | terminator | green |
| Rectangle | `[ ]` | process | blue |
| Rhombus | `{ }` | decision | yellow |
| Parallelogram | `[/ /]` | input or output | orange |
| Cylinder | `[( )]` | data store | dim red |
| Parallelogram, red | `[/ /]` | stop or escalation | red |

A dotted edge is read-only access. Rendering with a pinned mermaid-cli proves the syntax; the reviewer of any change that touches a diagram checks the semantics.

## 1. Overview

**What TAC is.** One authored tree, `.agents/`, that every coding-agent harness is made to obey, with the harness's own source kept next to it in `src/tac/`, `hooks/`, `templates/` and `contracts/` so the product can edit itself. The owner edits one file, `.agents/config.toml`. It names the profile (enterprise, standard, yolo), the models, the teams and the standards, split into commented TOML that a schema checks. A generator renders what each harness natively reads and records every output in a lock, so a hand edit is a red check. Work runs through pipelines declared in TOML whose stages hand each other a validated envelope; a stage that skips its envelope is refused. Memory is an append-only log of attributed records with a rebuilt index and a deterministic selector.

What no agent can bypass is the repository boundary: agents act under a machine account with no admin rights, CI judges every change from the base revision, a ruleset with bypass off for everyone (the owner included) guards the main branch, and approvals count only when they come from the owner's own GitHub account or a host-side signed record. At run time the authority lives in one host process, the runner, with its keys in the macOS keychain. Agent sessions run in sandboxes that can write only their worktree and a worker store, and every hook and checker they call is a stamped, non-editable copy (section 10).

TAC is proven first in `dev/` by building a small real tool through it, with every hook, stage, memory write, handoff, human step and bypass attempt asserted by tests. Only then does [vibe-map](https://github.com/tpetedb/vibe-map), the first adopting project, take a pinned release. The full target is six harnesses, three profiles, containers and a secrets file injected per step; the build is phased, and release 1 is cut at Claude Code and Codex enforced with the standard profile (Q18, section 18).

**Who it is for.** One owner who runs several coding agents from more than one provider (Claude Code and Codex first) on one machine, wants the same rules, memory and quality gates whichever agent does the work, and keeps the final say over anything that leaves the machine.

**What the owner asked for**, in plain words:

- one source tree, `.agents/`, and one knob file inside it; pointers generated for every harness that does not read the tree natively;
- enforcement, not suggestion: whatever harness does the work (Claude Code, Codex, GitHub Copilot, opencode, pi, Gemini CLI or a plain script), the configuration is applied and checked;
- prompt pass-through by template: every agent-to-agent and human-to-agent handoff goes through a declared Jinja template and a schema, and a handoff that bypasses them fails;
- production-grade defaults in every profile: semantic versioning, a fragment-based changelog, ISO 8601 dates, tests and lint gates, licence and dependency hygiene, security baselines and release steps; yolo relaxes approvals, never gates;
- Mermaid diagrams everywhere, with ISO-grounded conventions, validated by rendering;
- a standards registry declared once and checked deterministically, which a project can only tighten;
- the ability to build any production-grade artefact: apps, tools, libraries, data pipelines, documents; nothing game-specific;
- a dedicated agent toolchain environment in `.agents/.venv`, locked by uv, and data-quality testing for data work;
- local quality hooks before code leaves the machine, identical to CI;
- a human loop with a root `TODO.HUMAN.md`, dated recaps and an approval queue;
- a board of three directors for decisions that loosen policy;
- no fork of an existing project; a `dev/` proving ground first.

**The tree, short form.**

```text
toms-agent-config/
  src/tac/  hooks/  templates/  contracts/     # the product: CLI and runner, hook guard, Jinja, JSON Schema
  .agents/                                     # the authored tree, a stamped dogfood copy here; read-only to agents
    config.toml  config/  skills/  memory/  context/  docs/  _index/  generated.lock  standards.floor.toml
    pyproject.toml  uv.lock  lib/tac/  .venv/ (gitignored)
  AGENTS.md  CLAUDE.md  .claude/  .codex/  .github/  .gemini/  .pi/  opencode.json   # generated surfaces
  TODO.HUMAN.md  .human/{recap,approvals}/  work/  dev/  tests/  docs/  justfile  LICENSE  THIRD_PARTY.md
  runtime, never committed: $(git rev-parse --path-format=absolute --git-common-dir)/agents/
```

**The knob file, short form** (full text in section 3.1):

```toml
schema_version = 1
[project]
name = "toms-agent-config"
kind = "tool"
[profile]
active = "standard"                      # enterprise | standard | yolo
[governance]
chief = "chief"
directors = ["fable", "astra", "opus"]
agent_identity = "tac-bot"               # a machine account, write access, no admin
[models]
policy = "default"
[teams]
table = "default"
max_local_agents = 4
[usage]
claude = { slow_at = 0.60, stop_at = 0.85 }
[standards]
registry = "default"                     # the floor beneath it can only be tightened
[harnesses]
enforced = ["claude", "codex"]
instructions_only = ["copilot", "opencode", "pi", "gemini"]
```

**The flow in one picture.**

```mermaid
flowchart LR
    T(["owner"]) -->|"chat, phone"| C["chief of staff<br/>Opus 5.5, bot identity"]
    C -->|order| P["pipeline run<br/>envelopes, gates"]
    P --> B["builder<br/>own worktree, sandbox, no token"]
    B -->|"proposal, diff"| RW["trusted runner<br/>host side, bot token"]
    RW -->|"commit, push, PR"| GH[("GitHub<br/>ruleset, CODEOWNERS")]
    GH --> CI{"CI from base revision<br/>tac check, tests"}
    CI -->|green| T2(["owner: one merge command<br/>or a GitHub review"])
    P -->|question| H[/"TODO.HUMAN.md<br/>approvals"/]
    H --> T
    RW --> M[("memory and runtime store")]
    classDef term fill:#00A86B,stroke:#00D084,color:#000000
    classDef proc fill:#0067A5,stroke:#0088CC,color:#FFFFFF
    classDef dec fill:#FFBF00,stroke:#FFD500,color:#000000
    classDef io fill:#FF8C1A,stroke:#FFA94D,color:#000000
    classDef store fill:#9A2A2A,stroke:#F04923,color:#FFFFFF
    class T,T2 term
    class C,P,B,RW proc
    class CI dec
    class H io
    class GH,M store
```

Legend: green terminator, blue process, yellow decision, orange input or output, dim red data store.

**Releases.** Release 1: Claude Code and Codex enforced, the standard profile, the human loop, the memory core, the GitHub gate, the `dev/ledger` proof and adoption by vibe-map. Release 1.1: the enterprise and yolo profiles with `.agentcontainers/`; Copilot, opencode, pi and Gemini enforced, each after its own receipt; automated board quorum; memory decay and fold; Soda as an extra. Nothing is dropped from the target (section 18).

**Words used below.**

| Word | Meaning |
|---|---|
| harness | a coding-agent client: Claude Code, Codex, Copilot CLI, opencode, pi, Gemini CLI |
| stamp | copy a pinned TAC release into a project's `.agents/`, with an uninstall manifest |
| deploy | host-side re-stamp of `.agents/` and rebuild of its venv after a merge |
| runner | `tac runner`, the one host process that holds keys, performs effects and signs receipts |
| worker store | the runtime folder agents may write: proposals, journals, envelopes, the effect inbox |
| controller store | the runtime folder only the runner may read or write: leases, approvals, receipts |
| envelope | the validated JSON record one pipeline stage hands the next |
| receipt | a runner-signed record of something it observed: a gate run, a dispatch, an effect, a probe |
| floor | the read-only minimum standards; a project may only tighten them |
| order | one unit of work with owned paths and acceptance commands, under `work/orders/` |

## 2. The trees

### 2.1 The product repository

```text
toms-agent-config/
  src/tac/                       # the CLI and runner: config, sync, check, doctor, explain, run, handoff, memory, human, work, launch, github
  hooks/                         # run.py: the offline guard, stdlib, Python 3.9 compatible; it calls the full checker from the locked venv
  templates/                     # Jinja2 sources: handoffs/, adapters/<harness>/, human/, init/; the first line names the output; output paths come from an allowlist
  contracts/                     # JSON Schema draft-07 for every payload, memory record, config file and receipt; model-facing contracts use the OpenAI strict subset
  .agents/                       # the dogfood copy, stamped by `tac stamp` from src/; read-only to agents
    config.toml                  # the knob file (section 3)
    config/                      # profiles/, models.toml, roles/, teams.toml, pipelines/, standards.toml, gates/<kind>.toml,
                                 # hooks.toml, policy.toml, mcp.toml, network.toml, runtime.toml, github.toml, probes.toml,
                                 # receipts.toml, capabilities.toml, jobs.toml, runner.pub
    standards.floor.toml         # read-only, lock-hashed; effective = max(floor, project); [[waivers]] with an expiry
    skills/<name>/SKILL.md       # Agent Skills standard; skills/_index/ generated
    memory/                      # README, records/ (promoted, committed), events/ (append-only supersession, review, decay), seed.jsonl, _index/
    context/                     # brief.md, the common brief AGENTS.md is rendered from; claude.md, codex.md, rules/<topic>.md: lazy per-harness extras
    docs/                        # ADRs of the harness itself
    _index/                      # generated map of the tree, one line per folder
    hooks/run.py                 # the stamped, non-editable guard every rendered hook calls; it imports only from .agents/.venv
    lib/tac/                     # the stamped package copy; the named source tac is installed from, non-editable
    generated.lock               # sha256 of inputs, outputs, hooks tree, policy and floor
    pyproject.toml  uv.lock      # the agent toolchain; .venv is gitignored, built only by bootstrap, the runner or CI, never by a worker
    agents.env.example           # names only, never values
  AGENTS.md  CLAUDE.md           # generated by tac sync (Q20): AGENTS.md from .agents/context/brief.md, CLAUDE.md as @AGENTS.md plus the Claude extras
  .claude/ .codex/ .github/ .gemini/ .pi/ opencode.json   # generated, committed, lock-listed
  .devcontainer/                 # for humans
  .agentcontainers/<profile>/    # release 1.1: its own clone inside, never the host .git
  .human/recap/<UTC basic datetime>-<slug>.md   .human/approvals/<id>.json
  TODO.HUMAN.md                  # rendered from approvals by the chief, reaches main through a pull request
  work/  dev/  tests/  docs/     # orders; the proving ground; tests; HARNESS.md, adr/, diagrams/
  justfile  .pre-commit-config.yaml  copier.yml  mise.toml  LICENSE  THIRD_PARTY.md  CHANGELOG.md  changelog.d/
```

Two runtime stores, never committed.

- The **worker store**, `<git common dir>/agents/`, holds what agents may write: `room.md`, `memory.jsonl` (the live journal), `proposals/` (config and record proposals), `sessions/<UTC date>/<id>.jsonl` with a daily `_index.json`, `runs/<run>/NN-<stage>.json` (envelopes), `traces/<order>/<stage>.md`, `inbox.jsonl` (effect requests to the runner) and `metrics.jsonl`. Because it sits in the git common directory, every worktree shares it. Codex writes it only when launched with `--add-dir` on the absolute path, since relative writable roots are not reliable; Claude writes it through Bash. Granting that root grants filesystem access to that store and nothing else.
- The **controller store**, `~/.local/state/tac/<repo>/`, belongs to the runner alone: leases (global, so the four-agent budget is counted once across projects), the effect journal, verified approvals, signed receipts, checkpoints and the runner's own state. It is never a writable root for an agent, it is on Claude's `denyRead` and `denyWrite` lists, and section 10 says how that holds after launch.

How the folders the owner asked for map onto this tree: `sessions`, `reasoning-traces` and `analysis` are runtime and gitignored, with their `_index/` summaries committed; `.git` in the list means `hooks/git/` sources installed into `.git/hooks`; `API` means `contracts/`; `utils` and `jobs` become `src/tac/` and `config/jobs.toml` (job definitions), with job receipts in the runtime store; `data`, `tools`, `assets`, `design` and `vault` are project folders that the gate pack for the project's kind knows about; `.obsidian` lives inside a vault.

### 2.2 An adopting project

```text
<project>/
  .agents/                       # stamped from a pinned TAC release; config.toml and config/ are the project's own
    config.toml  config/  skills/  memory/  context/  hooks/run.py  lib/tac/  _index/  generated.lock  standards.floor.toml
    pyproject.toml  uv.lock  .venv/ (gitignored)
  AGENTS.md  CLAUDE.md           # both generated by tac sync; the brief is .agents/context/brief.md
  .claude/settings.json  .claude/agents/*.md  .claude/skills/<n> -> ../../.agents/skills/<n>   # per-skill links made at setup, copy fallback, never committed
  .codex/config.toml  .codex/hooks.json  .codex/agents/*.toml
  .github/CODEOWNERS  .github/ISSUE_TEMPLATE/*.yml  .github/labels.yml  .github/workflows/{ci,labels}.yml
  .gemini/settings.json  .pi/settings.json  opencode.json   # instructions only in release 1
  .devcontainer/  agents.env.example
  TODO.HUMAN.md  .human/
  work/orders/                   # the project's orders
  <the project's own code, docs, data>
```

The canonical common prose is the project's brief, `.agents/context/brief.md` (Q20). `tac sync` renders the root `AGENTS.md` from it and lists it in the lock, so a hand edit to `AGENTS.md` is a red `tac check`, and the Codex chain budget is checked on the render. The rest of `.agents/context/` holds lazy per-harness extras, and `CLAUDE.md` is generated as `@AGENTS.md` plus the Claude extras. Adoption is reversible: `tac stamp` writes an uninstall manifest, and `just adoption-roundtrip` proves a clean install, update and rollback in a fixture before any real project adopts.

What each harness reads and what TAC generates for it:

| Harness | Reads | Generated | Hooks | Release |
|---|---|---|---|---|
| Claude Code | `CLAUDE.md` = `@AGENTS.md` plus context; nothing under `.agents/` is discovered automatically ([memory](https://code.claude.com/docs/en/memory)) | `.claude/settings.json`, `.claude/agents/*.md` (`model`, `effort: xhigh` for directors), per-skill relative links with a copy fallback | `settings.json` hooks; exit 2 blocks ([hooks](https://code.claude.com/docs/en/hooks)) | 1, enforced |
| Codex | the `AGENTS.md` chain (32 KiB cap; TAC checks at 24 KiB), `.agents/skills` (listing budget 2% of the window, 8,000 characters only when the window is unknown) ([AGENTS.md](https://developers.openai.com/codex/guides/agents-md), [skills](https://developers.openai.com/codex/skills)) | `.codex/config.toml` (trusted projects only, no `otel`, no profiles), `.codex/hooks.json`, `.codex/agents/*.toml` with `name`, `description`, `developer_instructions`, `model`, `model_reasoning_effort` | `hooks.json`, exit 2 denies; untrusted hooks warn at startup; hosted tools and `write_stdin` escape pre-tool coverage ([hooks](https://developers.openai.com/codex/hooks)) | 1, enforced |
| Copilot CLI | `AGENTS.md`, `.agents/skills` | `.github/agents/*.agent.md` with `include-custom-instructions: true`; no `handoffs`, since the CLI behaviour is not established ([custom agents](https://docs.github.com/en/copilot/reference/custom-agents-configuration)) | `.github/hooks/*.json`: sessionStart, preToolUse, postToolUse, agentStop; timeouts fail open; it also reads Claude settings, so duplicate firing is tested ([hooks](https://docs.github.com/en/copilot/reference/hooks-reference)) | 1.1 |
| opencode | `AGENTS.md`, `.agents/skills` | `opencode.json` validated against the pinned schema: `instructions`, `permission`, `agent`; `OPENCODE_DISABLE_CLAUDE_CODE` set ([config](https://opencode.ai/docs/config/)) | a registered plugin mapping `tool.execute.before` and `after` to the guard, exit codes translated ([plugins](https://opencode.ai/docs/plugins/)) | 1.1 |
| pi | `AGENTS.md` from cwd and parents without trust, `.agents/skills` | `.pi/settings.json`; `.pi/prompts/` are prompts, not role executors ([settings](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md)) | a TypeScript extension on `session_start`, `tool_call`, `tool_result` ([extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)) | 1.1 |
| Gemini CLI | `.gemini/settings.json` with `context.fileName = ["AGENTS.md"]`; the `.agents/skills` alias ([GEMINI.md](https://geminicli.com/docs/cli/gemini-md/), [skills](https://geminicli.com/docs/cli/skills/)) | `.gemini/agents/*.md` (YAML frontmatter), `.gemini/commands/*.toml` ([subagents](https://geminicli.com/docs/core/subagents/)) | `settings.json` `hooks`: BeforeTool, AfterTool, AfterAgent, millisecond timeouts ([hooks](https://geminicli.com/docs/hooks/reference/)) | 1.1 |

In release 1 the four deferred harnesses are labelled "instructions only, unenforced", are excluded from any pipeline stage that writes, and each switches to enforced only after its own contract receipt.

Symlinks are used only for Claude skills: never for `.claude` itself (Claude Code refuses to create a worktree when `.claude` is a symlink), never committed, and a skill folder that is itself a symlink is refused at stamp time. Links break with `core.symlinks=false`, on Windows without Developer Mode and in single-folder bind mounts, which is why they are regenerated at setup and fall back to copies.

### 2.3 Generation and the lock

`tac sync` renders every output named by a template's first line, serialises JSON and TOML with a writer (never Jinja for structured files), and writes `generated.lock` with the sha256 of every input and output plus the hooks tree, the guard, the policy and the floor. `tac check` re-renders into memory and exits 1 on any difference, on any missing generated path and on any unexpected output. A hand edit to a lock-listed output is refused by the hook guard, by the pre-commit hook and by CI.

`tac check` runs before `tac sync` in every acceptance, on staged content; sync idempotence is a separate test. Templates render with `StrictUndefined`; a template a project adds renders in a sandboxed environment with no loader; output paths come from an allowlist, and a template naming a path outside it fails.

## 3. Configuration

Rules for every configuration file: nested tables; commented in a house style (a banner per section, and above each key why it exists and what to change it to); unknown keys fail; profiles are complete files with no inheritance; a gitignored `config.local.toml` becomes launch arguments, validated against the floor and never rendered into tracked files.

Changing a knob: a human edits the file, or `tac set` writes a proposal into the worker store and the change reaches `.agents/config.toml` through a pull request, or through a host-side `tac apply` run by the trusted runner. No agent ever holds a writable root on `.agents/`, which makes the Codex read-only rule for `.agents` a feature: an agent cannot edit its own policy. The owner's own edits never convene the board.

### 3.1 The standard config, in full

```toml
# =============================================================================
# .agents/config.toml: the one file you edit. Everything else under .agents/config/
# is a table this file points at. Unknown keys fail loudly; `tac explain` prints
# every effective value, the file it came from, and when it applies.
# =============================================================================
schema_version = 1

[project]
name = "toms-agent-config"
# What this repository builds. The gate pack config/gates/<kind>.toml adds the
# artefact-specific checks: app | tool | library | data-pipeline | document | mixed.
kind = "tool"

[profile]
# A complete file under config/profiles/; switching profile is this one line.
# enterprise: prompts for every external effect, container isolation, deepest review.
# standard: local work without prompts; external effects go through the trusted runner.
# yolo: no prompts, only inside a disposable container; refused on the host.
active = "standard"

[governance]
# The owner's single entry point: reads the room and the recap, answers short,
# prints exact commands.
chief = "chief"                                   # config/roles/chief.toml, Opus 5.5
# The board convenes only on the triggers below, never on routine work.
directors = ["fable", "astra", "opus"]           # the Opus director seat is a separate fresh session
quorum = 3                                        # the release stage needs one director, not quorum
tally_by = "fable"                                # never the proposer; the chief never counts its own proposal
board_triggers = ["policy-loosening", "checker-change", "security", "cross-team-dispute", "unresolved-finding"]
# Who the agents are on GitHub: a machine account with write access and no admin,
# holding a fine-grained token scoped to this repository. The owner's login never
# enters a session.
agent_identity = "tac-bot"
# What the chief may do unattended under that identity. Merges and releases are the
# owner's own action.
chief_may = ["open-issue", "comment", "push-branch", "open-pr"]

[models]
# config/models.toml: model ids per provider, roles, effort per model. Requested and
# actual model and effort are both recorded in every receipt.
# config/models.toml is the one place to set each agent's model, effort and ultracode.
policy = "default"

[teams]
# config/teams.toml: partitions of paths with a budget and an expiry.
table = "default"
max_local_agents = 4                              # the host budget, counted globally by the runner

[usage]
# Share of each provider's usage window at which routing steps down to cheaper models
# (slow_at, on the seven-day window) and new spawns pause (stop_at, on whichever window
# is higher). Read from the client's statusline at no token cost.
claude = { slow_at = 0.60, stop_at = 0.85 }
openai = { slow_at = 0.50, stop_at = 0.70 }
unavailable = "pause"                             # no quota reading means no new spawn
max_age_s = 900                                   # an older sample counts as unavailable; so does a cold start before the first API response

[standards]
# config/standards.toml; the floor in standards.floor.toml wins where it is stricter.
registry = "default"

[pipelines]
# Each is config/pipelines/<name>.toml.
enabled = ["order", "board", "review", "release", "retro"]

[memory]
runtime_store = "git-common-dir"                  # or an absolute path; AGENTS_RUNTIME overrides it inside containers
select_cap_chars = 6000                           # the most memory text one stage prompt receives
retention_days = 30                               # folding raw sessions and traces is a release 1.1 job; nothing is deleted

[human]
todo = "TODO.HUMAN.md"                            # rendered from .human/approvals by the chief
recap_dir = ".human/recap"
approvals_dir = ".human/approvals"
timeout_is_consent = false                        # an elapsed timeout never approves anything
verify = ["github-review-by-owner", "host-signed"]   # what counts as a real answer (section 7)

[harnesses]
enforced = ["claude", "codex"]
# These read AGENTS.md and the skills; no writing stage until each one is enforced.
instructions_only = ["copilot", "opencode", "pi", "gemini"]
copilot_surfaces = ["cli"]                        # add "ide" or "cloud" only when their files are wanted (Q7)

[secrets]
# The real agents.env lives here, outside the repository, and is injected per step by
# the launcher; agents never receive what a step does not need.
env_file = "~/.config/toms-agent-config/toms-agent-config/agents.env"

[telemetry]
# partial keeps Remote Control and auto mode working on the owner's machine; full is
# used inside containers and CI (section 12, Q15).
host_tier = "partial"
```

### 3.2 The profiles, complete

A profile has no `gates` key: the gates come from the floor and the registry, so no profile can switch a quality gate off.

```toml
# config/profiles/enterprise.toml
schema_version = 1
name = "enterprise"
approvals = "external-effects"           # every push, PR, merge, release and egress goes through the runner after a verified approval
isolation = "container"                  # release 1.1: its own clone inside, results as a pushed branch
network = "allowlist"                    # Codex: features.network_proxy = true with a tested proxy, else command networking off
native_delegation = "off"                # spawn tools removed from the rendered config; dispatch only through tac run, so refusal survives a hook failure
review = { independent = 1, second = "deterministic", cross_provider = true }
effort = "models"                        # the per-role table; a profile may lower a role, never raise it
local_checks = "affected-plus-security"
loop = { blocks_no_progress = 2, blocks_max = 8, iterations = 25, wall_minutes = 60 }
context = { digest_max_chars = 6000, tool_output_max_chars = 12000, read_max_kb = 200, agents_md_chain_max_kb = 24 }
mcp_servers = "none"
claude = { failIfUnavailable = true, allowUnsandboxedCommands = false, defaultMode = "default" }
codex = { sandbox_mode = "workspace-write", approval_policy = "on-request" }
```

```toml
# config/profiles/standard.toml
schema_version = 1
name = "standard"
approvals = "external-effects"
isolation = "native-sandbox"
network = "allowlist"                    # Codex: features.network_proxy = true with a tested proxy, else command networking off; Claude: sandbox.enabled rendered explicitly
native_delegation = "guarded"            # native spawns pass the PreToolUse handoff guard with a single-use dispatch token (section 5)
review = { independent = 1, second = "deterministic", cross_provider = true }
effort = "models"
local_checks = "affected"
loop = { blocks_no_progress = 2, blocks_max = 8, iterations = 25, wall_minutes = 60 }
context = { digest_max_chars = 6000, tool_output_max_chars = 12000, read_max_kb = 200, agents_md_chain_max_kb = 24 }
mcp_servers = "from-conf"
claude = { failIfUnavailable = true, allowUnsandboxedCommands = false, defaultMode = "acceptEdits" }
codex = { sandbox_mode = "workspace-write", approval_policy = "on-request" }
```

```toml
# config/profiles/yolo.toml
schema_version = 1
name = "yolo"
approvals = "none"                       # no prompts inside the container; external effects still go through the runner
isolation = "disposable-container"       # required; tac refuses yolo on the host
network = "allowlist"
native_delegation = "guarded"
review = { independent = 1, second = "deterministic", cross_provider = true }
effort = "models"
local_checks = "nearest"
loop = { blocks_no_progress = 2, blocks_max = 8, iterations = 25, wall_minutes = 60 }
context = { digest_max_chars = 6000, tool_output_max_chars = 8000, read_max_kb = 200, agents_md_chain_max_kb = 24 }
mcp_servers = "from-conf"
claude = { failIfUnavailable = false, allowUnsandboxedCommands = true, defaultMode = "bypassPermissions" }   # container only; a check refuses these values in any host-rendered file
codex = { sandbox_mode = "danger-full-access", approval_policy = "never" }
```

### 3.3 The standards registry and its floor

```toml
# config/standards.toml: the project's registry. The effective value is
# max(floor, project); CI evaluates the floor from the base revision.
schema_version = 1
name = "default"

[versioning]
scheme = "semver-2.0.0"
source = "pyproject.toml"                # where the version lives; the release pipeline bumps it

[changelog]
format = "keepachangelog-1.1.0"
fragments = "changelog.d"                # a code change carries a fragment; a release refuses a tag with fragments and no bump
types = ["added", "changed", "deprecated", "removed", "fixed", "security"]
tool = "towncrier"

[commits]
convention = "what-and-why"              # or "conventional-1.0.0"; decided Q4, ADR 0002
max_subject = 72
trailers_required = ["Co-Authored-By"]   # when an agent commits

[dates]
format = "iso-8601"
timezone = "UTC"
basic_in_filenames = true                # 20260924T1425Z in file names, the extended form elsewhere

[diagrams]
tool = "mermaid"
flowcharts = "iso-5807:1985-house-subset"      # documented in docs/diagrams/README.md; a legend on every diagram
architecture = "iso-iec-ieee-42010:2022"       # stakeholders, concerns, viewpoints, views; C4 is the presentation, not the conformance
validate = "mermaid-cli-pinned"                # rendering proves syntax; a semantic checklist is part of review

[code]
python = { formatter = "ruff", linter = "ruff", types = "basedpyright", line_length = 88 }
shell = { linter = "shellcheck", dialect = "bash-3.2" }
sql = { linter = "sqlfluff" }
markdown = { linter = "rumdl", em_dashes = false }
yaml = { linter = "yamllint" }
toml = { formatter = "taplo" }
actions = { linter = "actionlint" }

[quality]
tests = "required"
lint = "required"
secrets = "gitleaks"
dependencies = "pip-audit"
licences = "allowlist"                   # a test over the resolved lock
data = "soda-v3-optional"                # only when project.kind includes data-pipeline; decided Q13, ADR 0002: 1.1 opt-in
```

`tac check` reads this registry and runs one checker per line: the version is bumped when the changelog has fragments, a fragment is present for a code change, the commit convention holds, dates in file names and frontmatter are ISO 8601, a rendered diagram exists per pipeline and lifecycle, ruff and basedpyright pass, gitleaks and pip-audit pass, the licence allowlist holds, and the Soda scan passes where data changes and the extra is enabled.

How `max(floor, project)` is computed, key by key: a boolean gate is on if either side has it on; an enum key carries an ordered strictness list in the floor schema and the stricter value wins; a set key is the union; a numeric key declares `min` or `max` in the floor schema and the stricter bound wins; a key the floor does not know is the project's alone.

```toml
# .agents/standards.floor.toml: read-only, hashed in generated.lock, evaluated from base in CI.
# A project may only tighten it.
schema_version = 1
floor = { tests = "required", lint = "required", secrets = "gitleaks", changelog = "keepachangelog-1.1.0", versioning = "semver-2.0.0", dates = "iso-8601", em_dashes = false }

[[waivers]]
gate = "types"
reason = "dev/ledger prototype notebooks are untyped until the dev proof order"
expires = "2026-10-15"
approved_by = "owner"                    # CI fails on an expired waiver or one the owner did not approve
```

### 3.4 Gate packs per artefact kind

`config/gates/{app,tool,library,data-pipeline,document}.toml` list the extra checks for what the project builds: an app gets a smoke run and a security baseline; a library gets an API surface diff and a wheel build; a data pipeline gets a schema drift check and, once Q13 enables the extra, the Soda scan; a document gets rumdl and the diagram render, with vale optional until 1.1. A pack never requires a deferred tool. A passing harness is not a certificate of production readiness: the pack is the minimum the project promises.

## 4. Roles, identities, sandboxes

| Actor | Identity on GitHub | Runs where | May | May not |
|---|---|---|---|---|
| owner | their own account, admin | anywhere | approve, merge, release, edit `.agents/` directly, loosen policy | bypass the ruleset (bypass is off for admins too) |
| chief of staff (Opus 5.5) | `tac-bot`, write, no admin | the owner's interactive Claude session on the host, sandboxed like any session, reachable by phone through Remote Control | triage, recap, `TODO.HUMAN.md` via `tac human`, comment, open issues, run a registered Workflow script under ultracode; every effect (push, PR, render) is a request queued in the worker inbox that the runner re-checks and performs | merge, release, sign approvals, write policy, perform an effect itself, count its own proposal |
| trusted runner | `tac-bot` token from the keychain via a credential helper, handed per step; never the owner's token | a separate host process (`tac runner`, a launchd job or a terminal the owner starts), never a child of an agent session; its own venv and controller store (section 10) | render, commit for every builder, push, open PRs, journal effects, sign receipts, promote memory, apply config proposals, install hooks, build `.agents/.venv`, run the watchdog | act without a verified approval where the profile asks for one; import code from any checkout an agent can write |
| builder | none | its own worktree under `~/.worktrees/<repo>/<name>`, native sandbox (release 1) | edit owned paths, run `tac check`, write proposals and journals to the worker store | push, commit on Codex, touch `.agents/`, approvals, receipts, another order's files, start subagents or workflows |
| reviewer | none | a fresh execution on the other provider | write `review.toml` bound to a commit sha | build, start subagents or workflows |
| manager | none | a headless session, sandboxed like a builder | split a goal into orders inside its team's paths, one builder per order (the runner starts each); sign off cross-team changes | build, start subagents or workflows |
| scout | none | a headless, read-only session | research and fact checks with citations | write outside the worker store, start subagents or workflows |
| director | none | Fable 5.1 and Opus 5.5 as separate fresh Claude sessions (`--effort ultracode` at launch, `effort: xhigh` in the agent file); Astra 6 as a fresh `codex exec` per turn | positions, critiques; the lead seat's `design` stage may run a registered Workflow script under ultracode | routine work |
| CI | `GITHUB_TOKEN`, read only | GitHub Actions on the base checkout | judge | write to the repository |

`config/models.toml` is the one place to set each agent's model, effort and, for a role launched as a Claude Code session of its own, ultracode; `tac explain roles.<role>` prints what a role runs on and the command that starts it.

Models per role, as the default `config/models.toml` states them: the chief is Opus 5.5 at effort `xhigh` with ultracode on (`claude --agent chief --effort ultracode`; ultracode is a Claude Code session setting, not an effort, so the agent file says `effort: xhigh`, and it is never written into the project's `.claude/settings.json`, where it would turn on for every session); the directors are Fable 5.1 (lead), Astra 6 (through Codex) and Opus 5.5 (a separate seat); builders are Opus 5.5 on the Claude team and the Codex team's build model on the other; a reviewer is always the other provider's build model; scouts use a small, fast model at low effort. Requested and actual model and effort are recorded in every receipt, and a board run never falls back to another model silently.

**Who starts agents natively** (the board's ruling on the Workflow tool, 2026-09-25). Ultracode has Claude plan a dynamic workflow for every substantive task, and a workflow starts its agents through the `Workflow` tool, which neither the handoff guard on `Agent|Task` nor `native_delegation = "off"` covered. Each charter now says `delegates = true | false`. The chief and the director delegate: the chief in ultracode, and the lead director's `design` stage launched with `--effort ultracode`, may run a Workflow script that a stage of that role registers (section 5). Builders, reviewers, managers and scouts never delegate, since the runner starts each of them: their rendered `.claude/agents/<role>.md` lists `Agent`, `Task` and `Workflow` under `disallowedTools`, which removes them from the tools the file grants ([sub-agents](https://code.claude.com/docs/en/sub-agents); an allowlist in `tools` would have to name every other tool), and the guard refuses the three to them anyway (section 9). `tac check` refuses `delegates = true` on those four, ultracode on a role that does not delegate, and an active profile with `native_delegation = "off"` while any role launches with ultracode (a seat with `ultracode = true`, or a director seat with `launch_effort = "ultracode"`); the shipped enterprise profile therefore needs ultracode taken off `config/models.toml` before it can be made active. Claude Code already strips `Workflow` from every subagent, so a workflow only ever starts from a role's own session. A session that names no role, or a name no charter has, keeps `Agent` and `Task` by decision, not by accident: the owner's own plain `claude` session starts without `--agent` and so is roleless, while every worker seat is launched with `--agent <role>`, so the guard sees its role, and its agent file drops the spawn tools besides. `Workflow` stays fail-closed for both, and `tests/test_workflow_guard.py` holds the four cases.

**Board rules.** Positions are independent and schema-validated; there is one critique round; Fable compiles the tally and is never the proposer; a majority of three decides and the dissent is recorded; any director may raise a safety veto, which sends the item to the owner; the owner's ruling supersedes everything and is recorded as a `decision` record with `basis = "owner"`. The `release` stage needs one director, not quorum. The board convenes only for agent-proposed changes that loosen policy or touch the checker, for security findings, for cross-team disputes and for unresolved findings.

**Teams** are spin-up partitions: `[teams.<name>]` with `owns` (explicit paths), `manager`, `max_workers`, `models`, `skills`, `budget_usd` and `expires` (ISO 8601). A worker never owns what another active order owns; `tac work validate` says so.

**Chief plumbing.** `just chief` runs the command `tac config launch-command` reads from `config/models.toml` (by default `claude --agent chief --effort ultracode`), so an edit there changes the launch; from M3 it becomes `tac launch claude --role chief --remote-control chief`, which also compares the generated surfaces with the lock; `remoteControlAtStartup` and the push settings are user scope, written by `tac init --user`. `tac inbox watch` and `tac usage` (ported from existing watch scripts) feed the chief and the runner. `tac usage` reads the statusline `rate_limits` at no token cost; a reading is valid for `usage.max_age_s`, is unavailable before the first API response and in unattended runs without a live statusline, and an unavailable reading pauses spawns. Every spawn checks usage first. When the owner answers in chat, the chief records `tac human answer <Qid> <option>` with `basis = "owner-via-chief"`, which is enough for design questions; anything with an external effect needs the owner's own action. The chief never switches its own session into a worktree, because that blocks every running subagent; worktrees are created by script.

**Role adapters.** One TOML charter per role under `config/roles/`, rendered to `.claude/agents/<role>.md`, `.codex/agents/<role>.toml` (with `name` and `description`), `.github/agents/<role>.agent.md` (`include-custom-instructions: true`), `.gemini/agents/<role>.md`, `opencode.json` `agent` entries and `.pi/prompts/<role>.md` (a prompt only). A pin test asserts that every adapter carries the charter's hash.

## 5. Pipelines and the handoff envelope

A pipeline is a TOML file of stages. A stage names a role, its skills, the contract it reads, the contract it writes, its prompt template, a budget, its gates and what happens on failure. `tac run <pipeline> --order <id>` keeps a persistent state machine per run.

**Execution semantics.** States are ready, running, blocked, waiting-human, failed, skipped, cancelled, verified, reviewed and complete. A conditional stage that does not apply is `skipped`, and a join treats skipped as satisfied. Gate stages have no contracts. Typed input bindings name the upstream stage. Repair is one pass per validation failure; `retry.max` bounds gate reruns after a repair; two rounds without improvement stop the run, whichever limit is hit first. External effects are journaled before dispatch with an idempotency key and reconciled after a crash, so a retry never publishes twice. Gates are argv arrays run without a shell; `order_id` has a pattern in its schema; `tac check` rejects `{{` inside any gate. Build checks (`local_checks`) are kept separate from checks that need a review.

```toml
# config/pipelines/order.toml: from an issue to landed.
schema_version = 1
name = "order"
entry_contracts = ["issue"]

[[stages]]
id = "intake"                            # issue text becomes an order-request; issues from non-collaborators are refused
role = "chief"
reads = ["issue"]
writes = "order-request"
template = "handoffs/intake.md.j2"

[[stages]]
id = "specify"
role = "chief"
depends_on = ["intake"]
skills = ["work-order", "research-first"]
reads = ["order-request"]
writes = "order-spec"
template = "handoffs/specify.md.j2"
budget = { max_items = 12, max_chars = 24000 }
on_fail = "repair-once-then-human"

[[stages]]
id = "build"
role = "builder"
depends_on = ["specify"]
skills = ["house-style", "justfile", "verification-before-completion"]
reads = ["order-spec"]
writes = "build-report"
template = "handoffs/build.md.j2"
max_turns = 50
gates = [{ argv = ["just", "work-check"], args_from = ["order_id"] }]
retry = { max = 2, on_failure = { argv = ["just", "work-repair"], args_from = ["order_id"] } }
on_fail = "human"

[[stages]]
id = "review"
role = "reviewer"
provider = "other"
depends_on = ["build"]
reads = ["build-report"]
writes = "review"
template = "handoffs/review.md.j2"
gates = [{ argv = ["just", "work-review"], args_from = ["order_id"] }]

[[stages]]
id = "signoff"
role = "manager"
depends_on = ["review"]
when = "cross_team"                      # skipped when the order lists no cross team; the join below treats skipped as satisfied
reads = ["review"]
writes = "signoff"
template = "handoffs/signoff.md.j2"

[[stages]]
id = "gate"
kind = "gate"                            # deterministic only, no model, no contracts
depends_on = ["review", "signoff"]
gates = [{ argv = ["just", "work-accept"], args_from = ["order_id"] }, { argv = ["just", "checks"], args_from = ["local_checks"], waits_on = "M2" }]

[[stages]]
id = "publish"
kind = "effect"                          # the trusted runner commits and pushes as tac-bot, journaled with an idempotency key
depends_on = ["gate"]
effects = ["commit", "push", "open-pr"]

[[stages]]
id = "recap"
role = "chief"
depends_on = ["publish"]
reads = ["build-report"]
writes = "recap"
template = "human/recap.md.j2"

[[stages]]
id = "land"
kind = "human"                           # the owner is asked; merging is the owner's own action, never an agent's
depends_on = ["recap"]
reads = ["recap"]
asks = "Merge the pull request with the one command the recap prints, or say what is missing."
owner_actions = ["merge"]
```

**The pipeline files as built.** The five pipelines ship under `.agents/config/pipelines/` (`order`, `board`, `review`, `release`, `retro`), read through `PipelineFile` in `src/tac/config_schema.py` and exported to `contracts/config/pipeline.schema.json`; the shipped files write gates as `[[stages.gates]]` tables, which is the same TOML as the inline form above. A stage has a `kind`: `agent` (the default: a role, the contracts it `reads` as a list, the one it `writes` and its `template`; that is its envelope, and an agent stage without one is refused), `gate` (deterministic checks, no model, no contracts), `effect` (what the trusted runner performs, each in `policy.toml` `runner_only`, never an `owner_only` action, and always with a gate stage upstream) or `human` (the owner is asked, with the `owner_only` actions only the owner takes there). `order` ends with the human stage `land`, where the owner merges. A director stage names `seats`, `lead` or `others`. An agent stage may register the Claude Code Workflow scripts a session of its role may run, `workflows = [".agents/workflows/<name>.js", ...]`: only for a role whose charter delegates, on a director stage only the lead seat's `design` stage (`seats = "lead"`), and each a plain file under `.agents/workflows/` (write-denied to every agent and under CODEOWNERS); `tac sync` records each script's sha256 in a `[workflows]` section of `generated.lock` and `tac check` fails when a script no longer matches it. No script is registered yet, so every Workflow call is refused until one is, and a registered script runs only with the runner's single-use token for its stage. A pipeline enters with `entry_contracts`, a list, since `review` and `retro` start from two payloads and `release` from none. Where two upstream stages write the contract a stage reads, `bind` names the one it comes from. A gate whose recipe a later milestone adds says so with `waits_on` (the board's tally waits on M8, the local checks on M2), and a run refuses the pipeline until it is live. Skills are the ones present under `.agents/skills/`: until M4 adds `research-first`, `house-style`, `justfile` and `verification-before-completion`, the shipped `specify` and `build` stages carry `work-order` only, and M4 adds the others to them. The runner's gate allowlist (`DEFAULT_GATE_RECIPES` in `src/tac/runner.py`) holds the recipes the M0 skeleton gates; `tac run` (M3) widens it to every live gate a pipeline names, `work-repair` and `changelog` among them, and a test holds its arities to the justfile's. `tac pipeline check` names the reason for every refusal and `tac check` runs it; `tac pipeline plan <name>` prints the stage order and runs nothing.

**Rules `tac check` enforces on every pipeline.** `depends_on` is acyclic; every `reads` equals an upstream `writes` or the entry contract; `argv[0]` is `just` or a script under `src/tac/`; a `just` gate names a recipe the justfile (and the files it imports) defines, and the values it passes, literal argv after the recipe plus one per `args_from`, are a count that recipe's parameters take, counting defaults and `*`/`+` variadics; `provider = "other"` appears only on review stages; every `effect` stage runs only in the trusted runner. Every model-facing contract (the handoff payloads a model must produce) is JSON Schema draft-07 in the OpenAI strict subset: all properties required, nullable written as `[type, "null"]`, `additionalProperties: false` everywhere, no `allOf`, `not`, `if`/`then`/`else` or `dependent*`, an object root, depth at most 10, and the documented size limits (at most 5000 object properties, 120,000 characters across property names, definition names, enum and const values, 1000 enum values in all, and 15,000 characters in one string enum of more than 250 values); otherwise the first cross-provider review fails at the Codex API. Config and receipt schemas are not model-facing and keep optional keys and defaults.

**The envelope.** Each stage's result is stored as an envelope with these fields: `schema, stage, run_id, order_id, topic[], provider, harness, model, effort, created, modified, inputs[{path, sha256}], template_sha256, schema_sha256, rendered_input_sha256, payload, payload_sha256, validation{checks, errors[{code, severity, message, subject, evidence, supportedFixes}], repair_rounds}, tokens{input, cache_read, cache_write, output}, cost_estimate_usd, confidence{level, basis}, status` (pass, degraded or escalated). Envelopes live in the worker store under `runs/<run>/NN-<stage>.json`; digests, never whole envelopes, enter memory.

Four more fields make the envelope self-describing: `envelope_version` (1; an unknown version is refused), `template` (the template's path under `templates/`, which `template_sha256` hashes), `role` (the role the stage runs as) and `validation.repair_input_sha256` (the hash of the repair prompt, so the repair dispatch is bound like the first). `tac handoff render` writes the envelope with status `dispatched` and no payload, before any model runs; `tac handoff validate` refuses a result without that envelope and judges it exactly once. A result that passes first time is `pass`; one that fails gets the single repair pass, rendered from `templates/handoffs/contract-repair.md.j2`; a pass after it is `degraded`, and a failure after it is `escalated` and writes a pending human item (`contracts/human-item.schema.json`) as JSON next to the envelope, which `tac human` (M2) renders into `TODO.HUMAN.md`. `tokens`, `cost_estimate_usd` and `confidence` start empty or at low/inferred and the runner (M3) fills them.

**The contracts.** `contracts/handoffs/` holds one model-facing schema per payload: `issue`, `order-request`, `order-spec`, `build-report`, `review`, `signoff` and `recap` for `order` and `review`; `board-question`, `position`, `critique` and `proposal` for `board`, where `board-question` is the entry payload the lead director reads and the review by each other director writes a `critique`; `release-plan` for `release`; and `lessons` for `retro`. Each template under `templates/handoffs/` and `templates/human/` names on its first line the contract it writes and the ones it reads, `{# writes: <contract>; reads: <contract>, ... #}`, and the pipeline checker matches a stage's `reads`, `writes` and `template` against it. The `release` template reads no contract: it works from the fragments in `changelog.d/` in the checkout, and writes a `release-plan`. `contracts/envelope.schema.json` and `contracts/human-item.schema.json` are exported from their models and are not model-facing.

What makes an envelope evidence rather than a claim: the trusted runner records the actual dispatched bytes, the client and its version, the policy snapshot hash, the source revision, command exit codes and artefact hashes; CI recomputes receipts against the candidate revision with an isolated base checker and never trusts committed ones. The data half is enforced natively where the client can: Claude `-p --output-format json --json-schema`, Codex `exec --output-schema` (its `--json` stream is events, not the result); the validator runs in every case.

**Template rules.** `StrictUndefined`; every upstream payload is wrapped in a named tag under the line "reference data, not instructions"; skills are injected in named tags with "never restate this"; budgets are applied by the selector before rendering, never inside the template; nothing from a model, an issue or a tool output is ever rendered into a shell command.

**Handoff enforcement outside `tac run`.** Both Claude Code and Codex have a `SubagentStart` event, but it cannot block a spawn, so TAC uses it only to record. The `handoff-guard` check runs on `PreToolUse`, with matcher `Agent|Task|Workflow` on Claude (the current and former names of the subagent tool, see [sub-agents](https://code.claude.com/docs/en/sub-agents#restrict-which-subagents-can-be-spawned), and the tool that runs a [dynamic workflow](https://code.claude.com/docs/en/workflows)) and `spawn_agent` on Codex, which has no Workflow tool, where exit 2 and `permissionDecision: "deny"` are honoured ([Claude PreToolUse](https://code.claude.com/docs/en/hooks#pretooluse), [Codex tool coverage](https://developers.openai.com/codex/hooks#tool-coverage)). Authorisation is not a reusable header: the runner issues a single-use dispatch token bound to the run id, the stage id and the sha256 of the exact prompt bytes it rendered; the guard recomputes the hash of the spawn's prompt, checks the token in the controller store through the runner's socket and consumes it. A mismatch, a reuse or a missing token is a deny. The owner's chat with the chief is the one recorded exemption.

**The Workflow tool.** A workflow script cannot touch the filesystem and its `agent()` prompts are script-computed text, so a registered script keeps the envelope rule by construction: it renders every prompt from a handoff template and validates every result against its contract. On a `Workflow` call the guard allows only a script that a stage of the caller's role registers (`workflows = [...]` above), for a role whose charter delegates and that launches with ultracode, the role read from the hook's `agent_type` (the `--agent` a session was started with). It hashes the script bytes in the tool input, inline (`script`) or the file `scriptPath` names, and compares them with the sha256 `generated.lock` records. A file is accepted only from its registered path as spelled: the guard joins `scriptPath` to the session's `cwd` without following links, normalises it and takes it relative to the checkout, and refuses a path that climbs with `..` (the OS resolves `..` after any link before it), that is not exactly the registered path, or that has a symlink in any component from the checkout down to the file. A folder outside `.agents/` can be written by an agent, so a link through it could be retargeted between the check and the client reading the file; with no link on the way the file sits under `.agents/workflows/`, which no agent may write. The client documents no input schema for Workflow, so these two field names are as observed in Claude Code 2.1.281, and a call naming neither or both is refused. The call must also carry the runner's single-use token bound to the run, the stage and the script hash, which the guard consumes; a missing or reused token is a deny. The runner issues those tokens (`tac run`): each stage it dispatches gets one, bound to the run, the stage and the sha256 of the rendered prompt or of the registered script, in a dispatch record the launcher writes into the worker store under the session id, and the guard spends it over the runner's socket after the registry and hash checks; in a session the runner dispatched, `Agent` and `Task` are refused as well without a token bound to the sha256 of the spawn's prompt. This implements the amendment Q23 asks for, pending the owner's record of it. From M3 the PostToolUse hook and the runner write the signed run receipt (script hash, token, agent count, result hashes), and a run proves the auto-mode classifier marks the workflow's prompts as script-computed; those parts of the ruling move to M3 with the token (Q23 in `TODO.HUMAN.md` asks for the board amendment that records it). An allowed call is recorded first, one JSON line in `journal/workflows.jsonl` in the worker store (session, role, pipeline, stage, script, sha256 and token); a call that cannot be recorded is refused, and every other Workflow call is denied. The guard cannot tell director seats apart: every director session reports `agent_type = "director"`, and the Opus seat launches with `--effort ultracode` like the lead. So the seat is bound by the token, which names the run and the stage, and not by the role the hook sees. Under `native_delegation = "off"` the guard refuses `Agent`, `Task` and `Workflow` alike, and the rendered `.claude/settings.json` denies all three. Codex's `spawn_agent` is judged as before. `dev/` proves a refused spawn on both clients with a receipt.

**When the guard itself fails.** A hook that fails is not a hook that refuses. A Claude command hook that times out lets the tool call continue ([timeouts](https://code.claude.com/docs/en/hooks#timeouts)), Codex documents tools its hooks do not cover, and an internal deadline in the guard does not make the client fail closed. So the guard's failure modes are tested, not assumed: a missing token, an altered token, a token reused concurrently by two spawns, the runner lost mid-check and a hook timeout. Where refusal must survive a hook failure, the profile sets `native_delegation = "off"`: the rendered config removes the spawn tools (`Agent`, `Task` and `Workflow` on Claude) and dispatch goes only through `tac run` (build condition C1, section 18); a role launched with ultracode needs `guarded`, and `tac check` refuses `off` next to it. Until M2 renders the Codex PreToolUse guard and proves it, `.codex/config.toml` renders `[agents] enabled = false` under every profile, `guarded` included, since a guard that is not installed guards nothing.

```mermaid
flowchart LR
    A(["stage N agent"]) -->|"JSON result"| V{"contract check<br/>jsonschema + rules"}
    V -->|fail| R["one repair pass<br/>diagnostics in prompt"]
    R --> V2{"check again"}
    V2 -->|fail| H[/"TODO.HUMAN.md item<br/>or demote"/]
    V -->|pass| E[("envelope<br/>worker store runs/RUN/NN-stage.json")]
    V2 -->|pass| E
    E --> S["tac select<br/>deterministic, 6000 chars"]
    S --> J["Jinja render<br/>StrictUndefined"]
    J -->|prompt| B(["stage N+1 agent"])
    E --> M[("memory bus<br/>digest record")]
    classDef term fill:#00A86B,stroke:#00D084,color:#000000
    classDef proc fill:#0067A5,stroke:#0088CC,color:#FFFFFF
    classDef dec fill:#FFBF00,stroke:#FFD500,color:#000000
    classDef store fill:#9A2A2A,stroke:#F04923,color:#FFFFFF
    classDef stop fill:#D32F2F,stroke:#F04923,color:#FFFFFF
    class A,B term
    class R,S,J proc
    class V,V2 dec
    class E,M store
    class H stop
```

Legend: green terminator, blue process, yellow decision, dim red data store, red escalation.

**The other shipped pipelines.** `board`, with the explicit stages `design` (the lead director), `review` (each other director, independently), `revise` (the lead director), `propose` (the chief writes the proposal and its `TODO.HUMAN.md` items), `rule` (the owner, a human stage) and a `decide` gate where Fable compiles the tally. `review`: a delta re-review bound to a sha, the cheap recipe for a second look. `release`: fragments assembled and the version bumped on a release PR, the tag cut only from the approved, merged revision on main, a GitHub release, and one merge command handed to the owner. `retro`: lessons extracted at the LANDED and DECISION events as `kind = "lesson"` records, reviewed before promotion.

## 6. The memory bus

Memory is append-only and never rewritten. `records/` holds the original record; `events/` holds supersession, review, decay and correction events; `status` and `confidence` are derived by `tac memory index` from the events, and history is never edited. Every record carries `repository`, `scope` (project, team, order, session), `paths[]`, `sequence` (durable ordering) and `schema_version`, with a migration script per version. A torn write is recovered by rule: a line that does not parse is quarantined, never dropped silently.

`basis` is recomputed by the trusted writer from evidence it checks itself (a CI run id and its result, a merged review, a verified approval); the value a record carries is ignored. A record, shaped for the strict subset (every property present, nulls explicit):

```json
{
  "schema_version": 1, "id": "mem:20260924T142500Z:9f2a", "sequence": 1042, "supersedes": null,
  "kind": "decision", "repository": "tpetedb/toms-agent-config", "scope": "project", "paths": [".agents/config.toml"],
  "topic": ["harness.config", "codex.sandbox"], "statement": "The knob file is .agents/config.toml; agents propose changes, the runner applies them.",
  "provider": "anthropic", "harness": "claude-code", "model": "claude-fable-5-1", "effort_requested": "ultracode", "effort_actual": "xhigh", "role": "director",
  "created": "2026-09-24T14:25:00Z", "valid_from": "2026-09-24T14:25:00Z", "valid_to": null,
  "source_refs": ["docs/DESIGN.md#3-configuration", "https://developers.openai.com/codex/sandbox"], "order_id": "tac-core", "run_id": "run:20260924T1400Z:01", "commit_sha": null,
  "sensitivity": "internal", "content_hash": "sha256:0000"
}
```

**Kinds and basis.** Kinds are observation, decision, lesson, question, contract, component, session-digest and trace-digest. `confidence.basis` is one of test-receipt, reviewed, single-source, inferred or owner; a test receipt outranks repetition. Provider, harness, model, effort and role are written by the tool from the launch record, never typed by the model.

**Auto-update and promotion.** The runner ingests every validated stage envelope as a digest record; LANDED and DECISION events trigger extraction of candidate lessons and decisions. Promotion into `.agents/memory/records/` happens only through `tac memory promote`, run by the trusted runner, and the promoted records reach main through a `tac-bot` pull request like any other change. The rules: the recomputed basis is test-receipt, reviewed or owner; the status is confirmed; the memory lint finds no secret-like line. Conflicts stay visible: a contradicting record is marked `conflict` until adjudicated, and supersession closes the old record with `valid_to` through an event, never by deletion.

**Index.** `tac memory index` rebuilds `memory/_index/{by-topic,by-order,by-status}.json` and `_index/README.md`; the index is plain JSON, never hand-edited, and its hash is in the lock.

**Selection.** `tac select --stage <id> --order <id>` is deterministic: filter by scope, repository, validity, topic and status; decisions first; rank by exact path or order match, evidence basis, recency and a stable id; cap at `select_cap_chars`; write the selected ids, their hashes and the selector version into the envelope; refuse an oversized mandatory policy rather than truncate it. Agentic search comes first; semantic search is considered only after a measured need above roughly a thousand files.

**Traces and sessions.** A stage stores the plan it produced, never raw provider reasoning, one file per stage per run, capped at a few thousand tokens. Sessions are journaled by `tac session log`, run by the launcher: one JSONL per session with `ts, session_id, provider, harness, model, effort, role, event, tool, topic, cost_usd, tokens{input, output, cache_read, cache_write}` (all four token fields), and a per-day `_index.json` that rolls each session into one line. Sessions and traces are scanned for secrets when written, with one audited purge path.

**Release 1.1.** Decay (lowering the confidence of unconfirmed records toward a floor by age and flagging them for a re-check) and fold (folding raw sessions and traces older than `retention_days` into one-paragraph digests appended to the order's record, archiving the raw files locally, deleting nothing) are 1.1 jobs. `tac memory select` tests are plain pytest in release 1 and property-based with hypothesis in 1.1.

```mermaid
flowchart TD
    S[/"stage envelope<br/>validated"/] --> I["tac memory ingest<br/>digest record, tool-written metadata"]
    E[/"LANDED, DECISION events"/] --> X["extract candidates<br/>lesson, decision"]
    I --> J[("worker store memory.jsonl<br/>git common dir")]
    X --> J
    J --> L{"memory lint<br/>schema, caps, secrets"}
    L -->|fail| H[/"TODO.HUMAN.md"/]
    L -->|pass| P{"promotion rules<br/>basis, status, review"}
    P -->|promote| R[("memory/records/*.json<br/>committed through a PR")]
    P -->|hold| J
    R --> N["tac memory index"]
    N --> K[("memory/_index/")]
    K --> T["tac select<br/>cap 6000 chars"]
    T --> A(["next stage prompt"])
    D["decay and fold<br/>release 1.1, nightly"] --> J
    classDef term fill:#00A86B,stroke:#00D084,color:#000000
    classDef proc fill:#0067A5,stroke:#0088CC,color:#FFFFFF
    classDef dec fill:#FFBF00,stroke:#FFD500,color:#000000
    classDef io fill:#FF8C1A,stroke:#FFA94D,color:#000000
    classDef store fill:#9A2A2A,stroke:#F04923,color:#FFFFFF
    class A term
    class I,X,N,T,D proc
    class L,P dec
    class S,E,H io
    class J,R,K store
```

Legend: green terminator, blue process, yellow decision, orange input or output, dim red data store.

**As built in M3.** `src/tac/memory.py`, `src/tac/session.py` and `src/tac/secrets_scan.py` hold the bus. The live journal is `<worker store>/memory/records.jsonl` and `events.jsonl`, with `quarantine.jsonl` beside them (the worker store is `memory.runtime_store`, the git common dir's `agents/` by default); every write appends one fsynced JSON line under an exclusive lock and takes the last `sequence` plus one, a torn line is copied to quarantine by the next writer, an unknown `schema_version` is refused (`MIGRATIONS` is the hook for later versions), and provider, harness, model, effort and role come only from the launch record the launcher writes. The promoted store is `.agents/memory/records/<id>.json` (the colons of the id written as underscores, so every filesystem can check it out), `.agents/memory/events/<UTC date>.jsonl` and `.agents/memory/_index/{by-topic,by-order,by-status}.json` with its `README.md`; `status` (confirmed, unconfirmed, conflict, superseded) and `confidence` are derived from the events at read time, as of a given moment, and never stored. In M3 `basis` recompute accepts three kinds of evidence: `receipt:<path>`, a gate or probe receipt that passed, verified with the runner key at `HEAD` and bound to the record's repository, run or order and a revision `HEAD` contains (test-receipt); `review:<order>`, the order's own record passing the gate `tac work review` runs (reviewed); and `approval:<id>`, a host-signed approval of an item whose arguments name the record (owner). Without one the basis is single-source or inferred and promotion is refused with the rule named; `tac memory promote` also refuses inside an agent session, a record marked secret, a status other than confirmed and a secret-like line in any field of the record or of the events it carries (the same scan runs over every field on add, on every event and in lint), writes files only, and rebuilds the index. The selector is version 1 (`SELECTOR_VERSION`), reads the promoted store only, keeps the records of this repository (the origin remote's, or `tac select --repository`; with neither it refuses rather than select unfiltered), and treats a record whose topics include `policy` as mandatory. `contracts/memory-record.schema.json` is exported from the model in the strict subset and `contracts/session-event.schema.json` as a config-style schema, both by `tac memory schema --write`. Left for later: the index hash in `generated.lock`, a `sync.py` change that can land only after the checker that reads it is stamped (until then `tac memory lint` fails on an index that differs from a fresh build); the template wiring of the `Selection` into `tac handoff render`, so its ids, hashes and selector version enter the envelope; the ingestion of stage envelopes as digest records and the extraction of lessons and decisions at LANDED and DECISION events, which need `tac run`; the record contract's place in `tac check`'s contract lint, which reads `contracts/handoffs/` only (`tests/test_memory.py` holds it to the strict subset meanwhile); and decay and fold (release 1.1).

## 7. The human loop

`.human/approvals/*.json` is the single source of truth. `TODO.HUMAN.md` is rendered from it with `tac human render` and reaches main through a `tac-bot` pull request that the owner approves, like every other change, because the owner is the sole code owner and bypass is off for the owner too.

**The format of `TODO.HUMAN.md`**, taken from a private project where it has run in production: a title line, an opening paragraph saying what the file is for, the credential handover convention, then one `##` heading per topic with a checkbox list. Each item carries a stable id (`Q7`), the exact action, the env var name where a secret is involved, the options, the board's recommendation, the orders that wait on it, and the answer history below the item once answered. A provenance line says when and from what the file was compiled. Agents never read the secrets file; the runtime injects it.

**Approvals.** `.human/approvals/<id>.json` mirrors the approval shape of the [OpenAI Agents SDK run state](https://openai.github.io/openai-agents-python/human_in_the_loop/) (`pipeline, stage, agent, tool, arguments, question, status, asked_at, decided_at, decided_by`), so a paused run resumes exactly where it stopped; only the dependent stage blocks, and `timeout_is_consent = false`. An approval binds the action, its arguments, the revision and an expiry, and a replay is rejected.

**What counts as an answer.** The runner believes an approval only when it can verify it: a GitHub review or comment by the owner's own account, fetched through the API, or a record signed by the host-only `tac approve`, whose key sits in the keychain. A ticked box is a hint. Only the owner's account counts as a human answer; anything written by `tac-bot` is agent output. Design questions may be answered through the chief (`basis = "owner-via-chief"`); merges, releases and policy loosening need the owner's own action.

**How M2 builds it.** `tac human ask` writes an item with its recommendation, what it blocks and the milestone that waits (an approval also binds a tool, its arguments and a revision, and may name the pull request); `tac human answer` appends the owner's answer under it and closes a question or step, never an approval; `tac human render` writes `TODO.HUMAN.md` from the items and the fixed text and section order in `.human/todo.toml`, byte for byte, and `--check` fails on a hand edit. `tac approve <id>` runs only on the host, refuses a Seatbelt sandbox and an agent session, and signs the decision with the runner key under its own signature domain, binding the item, the action and arguments digest, the revision, the repository and an expiry. `tac human verify <id>` judges a signed record against a trusted `runner.pub` (the base revision's, or a provisioned copy) and an approval on GitHub as the latest decisive review by the repository's owning account on the pull request's current head; it exits 0 approved, 3 waiting, 4 rejected and 1 refused. The owner is the owning account of a personal repository; for an organisation repository no review counts until the owner's login is configured. Single use is enforced by the caller that acts: `verify_signed` refuses an approval id already in the `consumed` set it is given, and `tac human verify` only reports, so the same record reads as approved each time it is shown for the same item and head until it expires; the runner records each approval id it acts on in the controller store in M3 (`tac-runner`), which makes a replay a refusal there. The item contract is `contracts/human-approval.schema.json`.

**Recaps.** `.human/recap/<UTC basic datetime>-<slug>.md`, written by `tac recap` from the session journal and the room: shipped artefacts first, then what is in flight, then decisions, then what the owner must do, in under 400 words. The chief polls one inbox (the room, issue comments from collaborators, approvals), never idle agents.

```mermaid
flowchart LR
    G{"stage needs<br/>human input?"} -->|no| C(["continue"])
    G -->|yes| Q[/"approvals/&lt;id&gt;.json<br/>pending"/]
    Q --> T[/"TODO.HUMAN.md item<br/>id, options, recommendation"/]
    T --> R[/"recap<br/>.human/recap/&lt;datetime&gt;-&lt;slug&gt;.md"/]
    R --> H["owner answers: GitHub review or comment<br/>by their own account, or host-signed tac approve"]
    H --> VF{"verify: identity,<br/>action, revision, expiry"}
    VF -->|fails| W2[/"ignored: a ticked box is a hint"/]
    VF -->|passes| A{"approved?"}
    A -->|yes| C2(["resume the paused stage"])
    A -->|no| B["loop back to the named stage"]
    A -->|no answer| W[/"wait: timeout is not consent"/]
    classDef term fill:#00A86B,stroke:#00D084,color:#000000
    classDef proc fill:#0067A5,stroke:#0088CC,color:#FFFFFF
    classDef dec fill:#FFBF00,stroke:#FFD500,color:#000000
    classDef io fill:#FF8C1A,stroke:#FFA94D,color:#000000
    classDef stop fill:#D32F2F,stroke:#F04923,color:#FFFFFF
    class C,C2 term
    class H,B proc
    class G,VF,A dec
    class Q,T,R io
    class W,W2 stop
```

Legend: green terminator, blue process, yellow decision, orange input or output, red stop.

## 8. The GitHub gate

**Identity and ruleset.** `tac github apply` is run by the owner once, after Q14 is answered (which is why Q14 comes first in `TODO.HUMAN.md`). It creates or checks the machine account or GitHub App identity and sets a ruleset on the default branch through `gh api`: a `pull_request` rule with required code-owner review, `required_status_checks`, `required_linear_history`, `non_fast_forward` and `deletion`, and an empty `bypass_actors` list, so bypass is off for everyone including admins.

**How `tac doctor` checks it** (build condition C5). The list endpoint omits `bypass_actors`, and the single-ruleset endpoint shows it only to an admin token, so a count over the list proves nothing. `tac doctor` runs on the host with the owner's token, fetches each active branch ruleset by id, and fails when there is none, when `bypass_actors` is missing or non-empty, or when code-owner review or required checks are absent. A tag ruleset does not count. The equivalent shell, for reading:

```sh
# Run on the host with the owner's token: the list endpoint omits bypass_actors,
# and the single-ruleset endpoint returns it only to an admin.
repo="tpetedb/toms-agent-config"
ids=$(gh api "repos/$repo/rulesets" \
  --jq '.[] | select(.enforcement == "active" and .target == "branch") | .id')
test -n "$ids" || { echo "no active branch ruleset"; exit 1; }
for id in $ids; do
  gh api "repos/$repo/rulesets/$id" --jq '
    (has("bypass_actors") and (.bypass_actors | length == 0))
    and ([.rules[] | select(.type == "pull_request")
          | .parameters.require_code_owner_review] | any)
    and ([.rules[] | .type] | index("required_status_checks") != null)' \
    | grep -qx true || { echo "ruleset $id does not hold"; exit 1; }
done
```

`tac doctor` also fails when `gh auth status` inside an agent session shows an admin identity.

**CODEOWNERS.** `.github/CODEOWNERS` names the owner as the only owner of `.github/**`, `justfile`, `.pre-commit-config.yaml`, `pyproject.toml`, `conftest.py`, `.agents/**`, `src/tac/**`, `hooks/**`, `templates/**`, `contracts/**` and `scripts/**`, and names on their own lines what CI runs from: `.github/workflows/`, `.github/CODEOWNERS`, every `scripts/ci_*.sh`, `scripts/private_scan.sh`, `scripts/private_scan.py`, the term list `scripts/private_terms.txt` and every `.gitattributes` at any depth, since attributes decide what git treats as binary and how it checks files out. `tac doctor`'s `codeowners-ci` check reads CODEOWNERS as GitHub does (first of `.github/`, the root and `docs/`; the last matching rule wins; a rule with no owner unassigns) and fails naming each gap among every workflow, every `scripts/ci_*.sh`, every script a workflow names, the `justfile` whose recipes CI mirrors, CODEOWNERS itself, the root `.gitattributes` and a stand-in for one in a folder, and a stand-in for a new workflow and a new gate script, since GitHub reads CODEOWNERS from the base and a rule naming only today's files leaves tomorrow's open. A path owned only by the agent identity is a gap too.

**CI.** Workflows run on `pull_request` with a read-only `GITHUB_TOKEN`, never on `pull_request_target` with a head checkout. On `pull_request` the workflow file itself is candidate-controlled: GitHub runs the version in the pull request's merge commit, so a pull request can rewrite any step. CODEOWNERS on `.github/` plus the ruleset's required owner review is what protects it, the one layer no agent can bypass. Within that, `ci.yml` has two jobs, and the ruleset requires both (`REQUIRED_CHECKS` in `tac.github`). `verify` runs the candidate's own code: its environment, the workflow and shell lint, ruff, basedpyright, the tests and the deployed checker's `tac check` and `tac pipeline check`. `gates` runs none of it: no `uv sync` of the candidate's project, no test, no script from the checkout, no custom shell, no cache (setup-uv takes the uv version `mise.toml` pins and `enable-cache: false`, so no file of the candidate's picks the uv and no cache the `verify` job saved is restored). It takes the base and the head from the event payload by commit id (`github.event.pull_request.base.sha` and `head.sha`), never from a ref in the clone, since a ref can be moved; it takes every gate script from that base commit (`git show "$BASE_SHA:scripts/<name>"` into `$RUNNER_TEMP/gates/`) and runs only those: the private scan over the range, the work orders, the floor, the generated files and the receipts, each gate script building the base's own checker outside the checkout and reading the candidate tree as data. Each job has its own runner, so nothing a candidate test does reaches the gates. `tests/test_ci_gates.py` holds every step of the `gates` job to the programs it may start (git, and bash only on a script under `$RUNNER_TEMP/gates/`), to the event's commit ids with no ref name anywhere in the job, and to a pinned uv with no cache; it runs the job's own step text against a candidate that rewrote every gate script, once more after moving every ref named after the base to the candidate's commit, and proves the base's copy ran, told the base by its commit id; and it runs actionlint on the workflow.

**The bootstrap exception.** A base that predates a gate script runs the candidate's copy with a warning, and a gate script whose base has no stamped checker builds the candidate's `.agents/` instead. That fallback is candidate code, so the `gates` job cannot claim base-revision enforcement for a pull request whose base predates the gates. The bootstrap pull request is exactly that: `bootstrap/2026-09-25` into `main`, whose base holds only the initial README, runs every gate script from the candidate, and needs the owner's direct review of its gate scripts and workflow before it merges; its pull request notes say so. It replaces the stacked pull requests #1 (`docs/design` into `main`), #2 (`tac/m0-bootstrap` into `docs/design`) and #4 (`tac/m1-core` into `tac/m0-bootstrap`), which close as superseded (Q21, below). From the first pull request whose base carries this workflow and these scripts, the gates are the base's.

**The private scan.** The base's copy still reads the candidate's content, so the scan (`scripts/private_scan.sh`, a wrapper around `scripts/private_scan.py`, standard library only) reads what GitHub publishes: committed blobs, straight from the object store with `git cat-file --batch`, never the checkout and never through gitattributes, so no `-diff`, `binary`, `working-tree-encoding`, filter or smudge the candidate carries decides what is read. Every blob, text or binary, is read as UTF-8 with replacement and as UTF-16 and UTF-32 in both byte orders at every alignment, with or without a byte order mark. No blob is skipped as binary: any such guess can be forced (a NUL byte and a code unit invalid in every UTF-16 reading hide a whole file from it), while the raw download still serves the bytes, and UTF-8 with replacement keeps every ASCII byte in place, so an ASCII term stored as plain bytes inside a binary format, such as a PNG text chunk, is found. A hit prints a window of its line around the term. A Git LFS pointer is refused, since the content it stands for is not in the repository and git-lfs uploads it on push; the scan finds a pointer as git-lfs does, in the first 1024 bytes with surrounding whitespace trimmed, by a version line naming any spec git-lfs accepts (`git-lfs.github.com/spec/v1`, `hawser.github.com/spec/v1`, `git-media.io/v/2`) or by a version line followed by an `oid` line. Each path name is scanned as well. In CI the scan runs with `--range <base sha>..<head sha> --branch <head ref>`, both commit ids from the event payload, and covers every blob each commit of the pull request adds or changes against its first parent (a merge's own resolution included), the whole tree at the head, every commit object whole (the author and committer identities, every other header and the message, each hit named by its header) and the branch name, since a term added in one commit and removed in the next stays public in the history. Locally, with no arguments, it covers the index, new and changed files in the working tree (read as they stand, not yet blobs), the author and committer identity git would record (`git var`), so a commit under a work identity is caught before it is made, and the current branch name; the commit-msg hook adds `--message <file>`, which reads git's message file as the hook gets it, comment lines included since `git commit -m` keeps them, up to the scissors line, below which git drops everything. The terms live in `scripts/private_terms.txt`, one per line as a kind (`text`, matched without regard to case, `regex`, matched with case, or `identity`, below), one space and the term's bytes in lowercase hex, so the public list is not searchable text and an older scan that greps every file does not flag it; it is not a secret. Every other line of the list is blank: it takes no comments, since a free-text line there could carry a term past the scan, and its explanation lives in the scan's docstring. A list with any other line fails the scan (exit 2), and in any tree the scan reads the list at `scripts/private_terms.txt` like any other file less its entry lines, and reports each line there that is neither blank nor an entry, so a candidate's comment in the list is found and refused though CI loads the base's list. CI copies the scan and the list together from the base revision, so a candidate can neither empty the list nor change the scan it is judged by. The scan and its wrapper carry no terms and are scanned like any other file; the only content the scan does not read is the entry lines of `scripts/private_terms.txt`, at that exact path, and CODEOWNERS names it on its own line so a change to it needs the owner's review. The owner's personal address is a term, and it is also the identity the owner commits under, which every commit publishes; an `identity` line names that exact `Name <email>` pair, compared without regard to case, and exempts it in an author or committer header only, so the same words in a file or a message, and any other identity, are still refused (Q22: this repository now commits under the GitHub noreply address, set in its local git config, and the `identity` line goes once no open branch holds a commit under the old address). Not covered: the pull request's title, description and comments, which live in GitHub and not in git; tag names; a non-ASCII term stored in a legacy single-byte encoding; and a term spelled so that no reading matches it (split across lines, broken by an invalid byte or a zero-width character, compressed, base64 or any other transform), which the owner's review covers. Until this scan reaches a pull request's base, CI runs the base's older scan, which reads only the final tree; the step says so in a warning, and the owner's review covers the history in that window. The earlier scans carried their term list in plain text, so the history of `tac/m0-bootstrap` and `tac/m1-core` publishes that list, and a range scan over that history reports it. That history is re-cut, not excepted (Q21): `main` takes the design, M0 and M1 as one squashed commit on `bootstrap/2026-09-25`, which holds no plain-text list; pull requests #1, #2 and #4 close as superseded, and their branches are deleted once no open pull request depends on them. GitHub keeps a closed pull request's commits readable, so the old list stays visible there, which the owner accepts as low sensitivity. The scan carries no known-history exception, and every commit that reaches `main` is held to the whole list. `tests/test_private_scan.py` proves each property above with fixture repositories, and each test fails when the property it names is removed.

**The merge.** The command handed to the owner is `gh pr review <N> --approve && gh pr merge <N> --squash --match-head-commit <sha>` (squash, because history on main is linear; `--match-head-commit`, so the merge fails if the branch moved after review) (build condition C6). A pull request's author cannot approve it, so the owner's own edits also travel as `tac-bot` pull requests.

**Issue forms and labels.** Issue forms under `.github/ISSUE_TEMPLATE/` ([syntax](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms)): `bug.yml`, `feature.yml`, `work-order.yml` (id, team, owned paths, acceptance commands), `decision.yml` (question, options, recommendation) and `human-question.yml` (mirrors a `TODO.HUMAN.md` item); `config.yml` turns blank issues off. `.github/labels.yml` holds four families, `type/*`, `team/*`, `state/*` (queued, building, review, blocked, landed) and `priority/*`, in the format [EndBug/label-sync](https://github.com/EndBug/label-sync) reads, with no external App. As built in M2 no workflow writes labels: `tac github labels --dry-run` reads the public list with no credential and prints the plan, and `tac github labels --apply` is the owner's step on the host (S9), which creates and updates, reads back and never deletes. `tac github lint` (in CI and `just github-lint`) holds the forms to GitHub's form schema, every form label to the labels file, `team/*` to `teams.toml`, `state/*` to `[labels] states`, and `.github/pull_request_template.md` to its tag line `order: <id>`, its acceptance section and the one merge command. Automation uses `actions/github-script`: validate the form, apply labels, post one status comment per order, and mirror DECISION and LANDED room entries, deduplicated by entry id.

**Rules for automation.** Issue text is untrusted data and is never executed; only comments by users who can push are ingested; the automation's token is read-only except for labels and comments; the agent identity cannot bypass the ruleset. `release-drafter` is deferred to 1.1. The shipped `gh` runs with `GH_TELEMETRY=false` ([gh telemetry](https://docs.github.com/en/github-cli/github-cli/github-cli-telemetry)).

## 9. Enforcement at three layers

One checker, `tac check`, runs everywhere with the same registry. It validates config and contracts against their schemas, re-renders the outputs against the lock, and applies the standards registry, the memory lint, the pipeline rules, the `AGENTS.md` chain budget and the em-dash and secret scans. The layers, strongest last:

| Layer | Who acts | Identity | Sandbox | What it stops | Bypassable by an agent? |
|---|---|---|---|---|---|
| 1 rendered entry points | every harness | none | native | the wrong brief, missing skills, wrong wiring | yes, by a harness without discovery; hence layers 2 and 3 |
| 2 harness hooks | the harness, calling the stamped guard | none | native session sandbox; Claude runs hooks outside its Bash sandbox | edits outside owned paths, secret reads, generated-path edits, native spawns (`Agent`, `Task`, `Workflow`) the guard cannot vouch for, runaway loops | yes: timeouts fail open, hosted tools and `write_stdin` escape, shell edits are unseen |
| 3a git hooks | prek on the host | the committing human or the runner | host | bad commits and pushes before they leave the machine | yes, `--no-verify` |
| 3b repository boundary | GitHub ruleset, CODEOWNERS, CI from base, a bot without admin | `tac-bot` (write, no admin); the owner (admin, bypass off) | GitHub | anything not judged green from the base revision; any change to the checker or `.agents/` without the owner's review; any push by an identity that could bypass | no |

**Layer 2, the guard.** The rendered hooks call `.agents/hooks/run.py`, the stamped copy, never `hooks/run.py` in the source tree. It is the offline guard: stdlib only, Python 3.9 compatible (macOS ships 3.9 as `/usr/bin/python3`, and a hook must not depend on the venv existing), with its own deadline under the harness timeout. It invokes the full checker from `.agents/.venv`, where `tac` is installed non-editable from the stamped copy (section 11). When the venv is missing, deny-class checks exit 2 with "run tac init" and reminder-class checks exit 0; any launch failure maps to exit 2 for deny-class checks. The rendered command holds to the same rule one step earlier: when it cannot find the stamped guard (a Codex session outside a git work tree, where it asks git for the root, or a checkout never stamped) it refuses a tool call and lets every other event pass, so a stop is never sent back for a guard that is not there. Two checks are not wired on Codex in release 1: a secret read, because Codex reads files through its shell and no read tool names a path, so there it rests on the secrets living outside the repository (section 11) and on the Codex sandbox; and the order check at a subagent's stop, because native Codex subagents are rendered off until the dispatch tokens of M3, which add Codex's subagent stop to the guard's event table. The `secret-read` and `generated-paths` checks read the same deny lists the rendered Claude rules are made from (`denied_reads` and `denied_writes` in `src/tac/adapters.py`): the policy's paths, the credential folders, the folder of the secrets env file, the generated outputs and the controller store, so a read or an edit the client's rules refuse, the guard refuses too. Only the guard is stdlib; `tac check` itself uses the locked venv.

**The handoff guard, as built in M2.** The `handoff-guard` check answers `Agent`, `Task` and `Workflow` on Claude. It refuses all three under `native_delegation = "off"` and to any role whose charter says `delegates = false` (the builder, reviewer, manager and scout), whatever their agent files already leave out. A `Workflow` call is allowed only for a role that delegates and launches with ultracode, running a script registered for that role with its hash in the lock, read from its registered path with no symlink on the way, carrying the runner's single-use token, and only after the call is recorded in the worker store (section 5); a session started without `--agent` names no role and is refused. No token exists before M3, so in M2 every `Workflow` call is refused. `Agent` and `Task` from a delegating role still pass until the runner's dispatch tokens (M3). Because a timed-out hook lets a call continue, the `off` profile, the `disallowedTools` in the worker agent files and Claude Code's own removal of `Workflow` from every subagent are what hold when the guard does not answer.

**The handoff guard from M3.** A session the runner dispatched is known twice: by the dispatch record the launcher writes into the worker store under the session id (run, stage, role, token and kind), and by a marker the runner writes into the controller store, which no agent can write or remove. In such a session, `Agent` and `Task` from a delegating role pass only when the guard, through the checker, spends the record's token over the runner's socket for the sha256 of the spawn's prompt, and `Workflow` only when it spends one for the registered script's hash; the runner consumes a token by an atomic rename, so of two concurrent spawns one gets it, and a token presented with the wrong run, stage, hash or kind is burned with the refusal. The token is also bound to the session the run loop started it for: a token copied from another session's dispatch record is burned, and a record that names a different run or stage than the runner's marker for that session is refused before the runner is asked. A missing record, a record in any other shape, a missing or altered token, a runner that does not answer within five seconds and a connection closed before the reply are each a deny. A session with no dispatch record, the owner's own chat with the chief, keeps `Agent` and `Task`, and a call that names no session id is refused. The checker reaches the store through `HOME`, the one location variable the guard passes, so no guard change was needed. The guard records a subagent start into `journal/spawns.jsonl` (session, agent id and agent type) through `hooks.checks.spawn-record`, which renders a `SubagentStart` hook on Claude and on Codex, both of which fire the event and neither of which lets it block, and a Workflow result into `journal/workflow-results.jsonl` (script hash, token hash, result hash) through `hooks.checks.workflow-record`, a `PostToolUse` hook on Claude's `Workflow` tool, which Codex does not have; the runner copies the Workflow results of a session into its dispatch receipt. That journal sits in the worker store, which an agent can write, so the copied lines are evidence an agent could have written, not something the runner attests; the runner does not yet check them against the tokens it granted (issue #3). A record check never changes the answer: an event in any other shape is not recorded, a record that fails is dropped, and a deny check on the same event still decides. `tests/test_guard_failure.py` runs the stamped guard against a real runner for each failure mode.

**Hook execution matches the isolation it claims** (build condition C2). Claude Code runs hooks outside its Bash sandbox ([protected paths](https://code.claude.com/docs/en/sandboxing#protected-paths)), so the sandbox does not protect what a hook loads; the hook command line has to. Every rendered hook command therefore uses absolute paths to trusted executables (the pinned interpreter and the stamped guard), starts from a controlled environment (a fixed `PATH` of trusted directories, no inherited `PYTHON*` or `UV_*` variables) and runs Python in isolated mode (`-I`: neither the script's directory nor the user site-packages on `sys.path`, `PYTHON*` variables ignored). The guard reaches the checker through the absolute path of the `.agents/.venv` interpreter, never through a `PATH` lookup or project configuration discovery. Tests prove that a writable module planted in the worktree, a shim earlier on `PATH`, and a project-level `uv.toml` or `pyproject.toml` change nothing about what the guard loads or runs. Checking only where `tac` imports from is too narrow.

**Cross-harness protection.** Claude gets `sandbox.filesystem.denyWrite` on `.agents/**` plus the exact paths listed in `generated.lock`, with a matching Edit deny rule for each (never Read, or the skill links break). No Write rule is rendered: Claude Code checks file paths against Edit and Read rules only, and a Write rule with a path is never consulted and warns at startup ([permissions](https://code.claude.com/docs/en/permissions)). `.github/workflows/`, `justfile` and the other hand-maintained files stay editable in a builder's worktree, because CODEOWNERS guards them at the boundary. Steps that render or install (`tac sync`, `prek install`, `uv sync`) run in the runner, never in a builder sandbox. Codex cannot protect `.claude/`, so `tac launch` compares every generated surface and hook source with the lock from `origin/main` before starting a harness in a worktree, and refuses unattended starts on a difference. Codex trust for project hooks is by hash of the hook definition, not of the called script, so the lock pins the hooks tree and `tac doctor` reports project trust and hook trust separately.

**Layer 3a.** `hooks/git/prek.toml` is installed by [prek](https://github.com/j178/prek), pinned in the dev group of `uv.lock`, with `just hooks-install` on the host or by the runner, as pre-commit (ruff format and check, `tac check --staged`, the private-term scan; gitleaks, taplo and rumdl join once their pins land), commit-msg (`tac check --commit-msg`, the `[commits]` registry: one line of what and why with only trailers below it, the subject bound, no em dash, and the required trailers on a commit from an agent session; then the private-term scan of the message, `scripts/private_scan.sh --message`) and pre-push (the fast tests, `-m 'not integration and not slow'`, and `tac check`). Every hook is a local system command, `[git]` in `hooks.toml` names them in order, `tac check` refuses any difference, and the lock hashes the file with the guard. Local hooks are bypassable; CI plus the ruleset is the real gate.

**Layer 3b.** CI checks out the base sha into its own directory and runs the base's `tac check --base`, `justfile`, lock and workflow logic against the head tree, plus the full suite, the diagram render, pip-audit and the licence check. As built in M2, the `work` job takes four gate scripts from the base revision (see **CI** above) and each builds the base's stamped checker outside the checkout: `scripts/ci_work_from_base.sh` (the orders), `scripts/ci_config_from_base.sh` (`tac config check --base`, so the floor only tightens), `scripts/ci_check_from_base.sh` (`tac check` on the head tree, so every generated file matches the base's render and the lock) and `scripts/ci_verify_receipts.sh` (receipts, with the base's `runner.pub`). A pull request that changes `.agents/lib/` can have its generated files judged only by its own new renderer, so there `ci_check_from_base.sh` prints the base's verdict as a warning and CODEOWNERS is the gate. A separate `macos.yml` installs `just` pinned by version and SHA256 from the just project's release and runs the Seatbelt tests on a macOS runner, failing on any skip, since without `sandbox-exec` or `just` the confinement tests skip and prove nothing; it is not a required check, and the host run stays the acceptance evidence. A test deleted or skipped compared with base fails unless the order names it. Because `.agents/` and the checker are under CODEOWNERS, a pull request cannot quietly make the check return nothing.

```mermaid
flowchart TD
    W(["any harness or a plain script"]) --> E1["layer 1: rendered entry point<br/>brief, skills, roles, wiring"]
    E1 --> E2{"layer 2: harness hook<br/>stamped run.py, isolated"}
    E2 -->|"exit 2"| X1[/"blocked in session"/]
    E2 -->|"pass or no hooks"| G{"layer 3a: git hooks<br/>prek: ruff, tac check, gitleaks"}
    G -->|fail| X2[/"refused at commit or push"/]
    G -->|pass| CI{"layer 3b: repository boundary<br/>ruleset, CODEOWNERS, CI from base, bot without admin"}
    CI -->|fail| X3[/"cannot merge"/]
    CI -->|pass| M(["merge by the owner:<br/>one command or a review"])
    S[("standards.toml, contracts, generated.lock")] --> E2
    S --> G
    S --> CI
    classDef term fill:#00A86B,stroke:#00D084,color:#000000
    classDef proc fill:#0067A5,stroke:#0088CC,color:#FFFFFF
    classDef dec fill:#FFBF00,stroke:#FFD500,color:#000000
    classDef store fill:#9A2A2A,stroke:#F04923,color:#FFFFFF
    classDef stop fill:#D32F2F,stroke:#F04923,color:#FFFFFF
    class W,M term
    class E1 proc
    class E2,G,CI dec
    class X1,X2,X3 stop
    class S store
```

Legend: green terminator, blue process, yellow decision, dim red data store, red stop.

**What `dev/` proves about the layers, including each bypass attempt:** a PR that edits `src/tac/check.py` without owner review is refused by the ruleset; a push without the required checks is refused; a forged `approvals/*.json` is ignored by the runner; an altered receipt is recomputed and rejected by CI; a policy tamper in `.agents/` from a worker is denied by the sandbox and, if written through a shell, refused at commit by prek and at the boundary by CODEOWNERS; removing each required hook in a mutation run makes the suite fail; a native subagent spawn without a valid dispatch token is denied; a Workflow call with an unregistered script, without a token, with a reused token, or from a builder is denied; a registered run has every prompt hash matching a handoff template and every result valid against its contract, with the signed receipt; and the auto-mode classifier marks the workflow's prompts as script-computed.

## 10. The trust boundary at run time

**Where the authority lives.** In one host process, `tac runner`, started by launchd or by the owner in a terminal, never as a child of an agent session. It runs from its own venv at `~/.local/state/tac/<repo>/venv`, built by bootstrap from the pinned TAC release, so it never imports code from a checkout an agent can write. It owns the controller store at `~/.local/state/tac/<repo>/` (leases, the effect journal, verified approvals, signed receipts, checkpoints) and listens on a Unix socket there.

**What the runner holds** (build condition C6). Exactly two secrets, both in the macOS login keychain, read at start outside any sandbox: its ed25519 signing key and the `tac-bot` token. It never holds the owner's token: holding it, the runner could approve as the owner, which is the one approval that counts. The owner's token is used only by the owner, on the host, for `tac github apply`, `tac doctor`'s ruleset check and the merge command.

**Receipts** (build condition C3). Every receipt that counts (a gate run, a dispatch, an effect, a client probe) is written by the runner and signed; a copy is committed under `work/orders/<id>/receipts/`. The signature binds the repository, the revision, the run, the stage and the policy hash. The runner signs only what it observed or verified itself, never a worker's assertion. CI verifies each signature against `runner.pub` taken from the trusted base revision or from separately provisioned configuration (a repository setting the owner controls), never from the candidate being judged; a receipt replayed from another repository, revision, run or stage is rejected, and so is a candidate that supplies its own key. CI also recomputes what it can, so historical provenance rests on the signed receipt, not on files a worker could rewrite.

**Agent sessions** (the chief, builders, reviewers, managers, scouts, directors) run inside their harness sandbox with exactly two writable places: their worktree under `~/.worktrees/<repo>/<name>` and the worker store. They cannot write `.agents/`, `.agents/.venv`, the controller store or the keychain, and they cannot read the controller store or the secrets file. What runs when a hook fires is the stamped `.agents/hooks/run.py`, which imports only from `.agents/.venv` site-packages; both paths are read-only under Codex and denied under Claude (section 9), so a builder editing `src/tac/` changes nothing about the checker that judges its own session. A candidate `src/tac/` runs its tests in the builder's sandbox, but it becomes the deployed checker only after merge and a host-side `tac deploy` by the runner, which re-stamps `.agents/` and rebuilds the venvs at the new lock hash.

**Gate children.** A gate runs code a builder wrote: the root justfile's recipe bodies, an order's criteria as shell strings, the tests `just verify` starts. The runner never runs that code with access to its store, and never lets it write anywhere the owner's own processes later load code from, since a gate child that planted a Claude hook, a LaunchAgent, a git hook or a module in the judge's venv would run later as the owner outside any sandbox and read the key. Every child that touches the candidate tree, the gate itself and the runner's own `git status` (a repository's config can make it start an fsmonitor or a clean filter), starts under a macOS Seatbelt profile from `/usr/bin/sandbox-exec`, named by absolute path. Writes are an allowlist, not a denylist: the profile denies every write, then allows the checkout and a private scratch folder per child, then denies again, inside the checkout, `.agents/` (the stamped toolchain and the judge's `.agents/.venv`), `.git`, `.claude/` and `.codex/`, and the git common dir, which holds the hooks and the config every worktree shares. So the gate writes exactly what the builder's own sandbox writes, its worktree less the protected paths, plus scratch; the owner's `~/.claude/`, `~/Library/LaunchAgents/`, uv cache and every other path stay out of reach. The runner's `git status` gets no write to the checkout at all. Connects are an allowlist as well: the profile denies every outbound Unix socket, then allows only sockets under the child's scratch folder (where a test binds its own, under its `TMPDIR`) and `/private/var/run/mDNSResponder` for name resolution, and it denies outbound IPv4 to loopback (`ip4 "localhost:*"`) and all outbound IPv6, TCP and UDP alike. IPv6 goes whole because a v4-mapped address such as `::ffff:127.0.0.1` reaches an IPv4 loopback service past a localhost rule and Seatbelt cannot name the mapped range alone; the IPv4 rule says `ip4`, since next to an IPv6 rule `ip "localhost:*"` stops matching 127.0.0.1. `uv`, `git` and name resolution work over IPv4; a host reachable only over IPv6 is not. A socket or port of a process the owner runs unsandboxed would otherwise act for the child: `tmux -S` on the owner's tmux socket sends keys to a live shell, and `docker run -v $HOME` over the Docker socket reads the key, so either would undo every file rule in two steps. The builder's own harness sandbox allows no Unix socket by default, and the gate gives no more reach than that. The profile also denies reading and writing the controller store subtree, the signing key by name, connecting to the runner socket (listed last, so no allow above reopens it), and writing any folder above the store, since Seatbelt matches paths at access time and a renamed parent would otherwise expose the key under a new path; and it denies Apple Events, LaunchServices lookups and preference writes, each of which asks a process outside the sandbox to act. The child gets a fixed environment: the runner's own PATH, `USER`, `LOGNAME`, `LANG`, `LC_ALL`, `LC_CTYPE`, and `HOME`, `TMPDIR`, `XDG_CACHE_HOME` and `UV_CACHE_DIR` inside its scratch folder, which the runner removes after the child; nothing else, so no token, no `GIT_*`, no `TAC_STATE_HOME` and not the owner's home. It runs in the checkout at the committed revision the receipt names; a gate that leaves HEAD moved or the tree changed gets no receipt. Where no sandbox exists (not macOS, `sandbox-exec` missing, or the runner itself already inside a Seatbelt profile, since profiles do not nest) the runner refuses to gate or probe and says why; it never falls back to running unsandboxed. For the same reason, when a gate runs this repository's own `just verify`, the tests that need Seatbelt skip inside it, as they do on CI's Linux runner, and prove the boundary only when run on the host. What stays open until M3: reads, process starts and Mach IPC are allowed by default, so a system service that acts for its caller and is not on the deny list is not covered; outbound IPv4 to anything but loopback is allowed, since `uv` and `git` fetch over the network, so a service the owner runs listening on this Mac's own LAN address, rather than on loopback or a Unix socket, is reachable; and the same user can still read the key file from outside a sandbox, for instance from a Claude hook the owner's own session runs. The keychain entry of M3 (C6), or a dedicated user, closes the key file gap; the two service gaps stay open until M3 chooses between a dedicated user and a narrower profile.

**Effects are requests.** The chief or a pipeline writes an entry into the worker inbox; the runner re-checks the request against policy, the approval and the lease, performs it under the `tac-bot` credential helper, journals it with its idempotency key and signs the receipt.

**The live proof.** `dev/` proves this boundary live, not by fixture: inside a sandboxed Claude session and a sandboxed Codex session, `gh auth token` fails; `security find-generic-password` fails for both keychain entries (the `tac-bot` token and the runner signing key); a write to the controller store and to `.agents/.venv` is denied; an import of `tac` resolves to site-packages; and a forged receipt without a valid signature is rejected by CI. If the keychain proof fails on a client, the fallback is to run the runner under a dedicated macOS user with its own keychain, and `tac doctor` reports the boundary as unknown until then.

**As built in M3.** The runner reads its secrets from the macOS login keychain through `/usr/bin/security`, named by absolute path, and names exactly two entries, `tac-runner-signing-key` and `tac-bot-token`, each namespaced by the store's slug; a third name is refused before any process starts. `just runner-keychain` (host only) moves the key file in and stores the token, and a runner that still finds only the file warns on every start and names that step. The token reaches only the runner's own git, through `tac runner credential` passed with `-c credential.helper=`, and its own `gh`, as `GH_TOKEN` in that one child's environment; that git reads no global or system config and runs no hook or fsmonitor. The controller store now holds the token store (`tokens/`, each token kept by its hash and moved to `tokens/used/` when spent), the effect journal (`effects/`, one entry per idempotency key written before the effect runs, reconciled on `--resume` and on `tac inbox watch` start), the approval ids already acted on (`approvals/consumed.jsonl`), the checkpoints (`runs/<run>/state.json`, written whole and renamed after every transition) and the dispatched-session markers; the leases live in the user state folder (`leases/`), since `teams.max_local_agents` is a host budget. What stays open: the two service gaps above, and the choice between a dedicated macOS user and a narrower profile for them; the `open-issue`, `comment` and `render` effects, journaled and refused until M4 performs them; the memory selection, not yet wired into the dispatch prompts; the gates observe committed trees only, so a Codex builder's work waits for a commit before its build gate; the human items a run writes land in the owner's checkout and must be committed before a gate runs there again; Codex has no documented statusline shape, so its usage reading stays unavailable and a Codex stage waits; the classifier proof and the live probes wait for M4.

```mermaid
flowchart TB
    subgraph host ["host, outside every sandbox"]
        KC[("macOS keychain<br/>tac-bot token, runner signing key")]
        RUN["tac runner<br/>own venv from the pinned release"]
        CS[("controller store<br/>leases, effect journal, approvals, signed receipts")]
        KC --> RUN
        RUN <--> CS
    end
    subgraph hooks ["hooks, outside the Claude Bash sandbox"]
        HX["stamped run.py<br/>absolute executables, fixed env, python -I"]
    end
    subgraph sessions ["sandboxed agent sessions"]
        CH["chief"]
        BU["builder, reviewer, director"]
        WT[("worktree<br/>~/.worktrees/repo/name")]
        WS[("worker store<br/>proposals, journals, envelopes, inbox")]
        AG[(".agents/ stamped, .agents/.venv<br/>read-only: hooks/run.py, tac site-packages")]
        CH --> WS
        BU --> WT
        BU --> WS
        CH -.reads.-> AG
        BU -.reads.-> AG
    end
    CH -->|"tool call fires hook"| HX
    BU -->|"tool call fires hook"| HX
    HX -.loads.-> AG
    HX -->|"dispatch token check, socket"| RUN
    WS -->|"effect requests, proposals"| RUN
    RUN -->|"dispatch tokens, verified approvals"| WS
    RUN -->|"commit, push, PR as tac-bot"| GH[("GitHub<br/>ruleset, CODEOWNERS")]
    GH --> CI{"CI on pull_request<br/>base checkout, read-only token,<br/>runner.pub from base, recomputes"}
    CI --> TM(["owner approves, then merges<br/>squash, pinned to the head commit"])
    TM --> GH
    classDef term fill:#00A86B,stroke:#00D084,color:#000000
    classDef proc fill:#0067A5,stroke:#0088CC,color:#FFFFFF
    classDef dec fill:#FFBF00,stroke:#FFD500,color:#000000
    classDef store fill:#9A2A2A,stroke:#F04923,color:#FFFFFF
    class TM term
    class RUN,CH,BU,HX proc
    class CI dec
    class KC,CS,WT,WS,AG,GH store
```

Legend: green terminator, blue process, yellow decision, dim red data store; dotted edges are read-only. The trust boundary is the edge of the host subgraph: nothing inside the sessions subgraph can write anything inside it. The hooks subgraph is drawn apart because a Claude hook runs unsandboxed with the user's rights; that is why its executables, environment and imports are pinned (section 9).

## 11. Containers, secrets, the toolchain environment

**`.devcontainer/`** is for humans: python with uv, node (for mermaid-cli), gh and just; `postCreateCommand: tac doctor`; the Claude Code feature pinned by npm plus `DISABLE_AUTOUPDATER=1`, and Codex pinned by npm ([Claude devcontainer](https://code.claude.com/docs/en/devcontainer)); login stores in named volumes, never the host `~/.claude` or `~/.codex`.

**`.agentcontainers/<profile>/`** (release 1.1) is the strict runner for the enterprise and yolo profiles. The container gets its own clone, never the host `.git`; `.agents/`, `contracts/`, `templates/`, the native wiring, `hooks/` and the lock are mounted read-only or baked into the image; results come back as a pushed branch, and pushes happen host-side by the runner after a verified approval. The compose file puts the agent service on an `internal: true` network whose one peer is a proxy sidecar holding the allowlist rendered from `network.toml`; `cap_drop: [ALL]`, `no-new-privileges`, `read_only` with tmpfs, `user 1000:1000`, pids and memory limits, no Docker socket, no home mounts. Inside it, Claude runs with its sandbox off and `--dangerously-skip-permissions` as non-root, and Codex with `danger-full-access` and `approval_policy = "never"`; those values never appear in a host-rendered file, and the check that refuses them there ships in release 1. Six live checks are the delivery gate before any unattended use and run nightly afterwards: an allowed command runs; direct egress is denied; a disallowed proxy target is denied; an allowed proxy target succeeds; embedded DNS does not resolve external names; mounts are as declared, with no host credential inside.

**Secrets.** Agents never receive secrets they do not need. The launcher sets `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`; Claude gets `sandbox.credentials` or `denyRead` on the secrets directory, `~/.ssh`, `~/.aws` and `~/.config/gh`; Codex gets `shell_environment_policy.inherit = "core"` with `ignore_default_excludes = false`; the bot token reaches only the runner's `gh`, through a credential helper; `tac doctor` proves each deny with a live read.

**`agents.env`.** A common and sensible deny rule blocks `*.env` for every agent, and TAC keeps it. So the real `agents.env` does not live in the repository by default: its path is `[secrets] env_file`, the launcher, compose or CI secrets inject it per step, and agents never read it (Q17 decided outside the repository, ADR 0002). The repository ships `agents.env.example` with names and comments only, and every non-secret runtime setting lives in `config/runtime.toml`. Nothing is renamed to dodge the deny.

**`.agents/.venv`** (build condition C4). The agent toolchain has its own project, `.agents/pyproject.toml` and `.agents/uv.lock`, never mixed with the project's own environment. The venv is built only by bootstrap, the runner or CI, with `uv sync --frozen --no-editable --project .agents`. `tac` comes from a named path source declared in `[tool.uv.sources]`: `.agents/lib/tac/`, the package copy that `tac stamp` writes from the pinned release (in this repository, from `src/`). `--no-editable` matters: by default `uv sync` installs the project, and any workspace member, in editable mode, which would let the checker import straight from a source tree instead of site-packages. `tac doctor` verifies that the imported `tac` resolves to site-packages and not to `src/tac/`. Every in-session call is `uv run --frozen --no-sync --project .agents tac ...`; workers never sync. Python, the external tools, image digests and the lock are frozen in `mise.toml`, `uv.lock` and `generated.lock`. Under Codex the venv is read-only because `.agents` is read-only inside writable roots ([sandbox](https://developers.openai.com/codex/sandbox)), and under Claude by the `denyWrite` rule, so a worker-writable environment never supplies the checker's interpreter.

**Test the candidate, judge with the deployed copy** (build condition C4). `.agents/.venv` serves only the checker. A builder's tests of new code run from the worktree's own root project, where `src/` is installed editable in an environment the runner builds. So the candidate is what gets tested, and the deployed copy is what judges.

## 12. Skills and telemetry

**Skills** ship under `.agents/skills/`, each with provenance in its frontmatter `metadata` and a `THIRD_PARTY.md` line where it is not the owner's: `adr`, `changelog` (Keep a Changelog discipline merged in), `semver`, `justfile`, `mermaid-diagrams` (the ISO 5807 house subset, the palette, the legend), `readme-quickstart`, `verification-before-completion`, `webapp-testing`, `work-order`, `research-first`, `house-style`, `hooks-authoring` (made generic), `gitlab-ci`, `data-platform` (optional), `shared-memory` (rewritten for `tac memory`) and `ponytail` (opt-in, builders only, trimmed under its MIT notice, never injected into reviewers, no hooks; pending Q1). `skills/_index/` is generated. Skills specific to one product stay in that product (for vibe-map: `obsidian-notes`, `camp-progress`, `council`, `develop-camp`, `install-camp`, `duckdb-sql`, `python-data`), and skills specific to one private tool stay with that tool. No caveman package, skill or seed is shipped, and that question is closed.

Every skill passes the portable subset lint of the [Agent Skills specification](https://agentskills.io/specification): `name` equals its folder and is at most 64 characters, `description` is at most 1,024 characters, the body is under 500 lines, and only specification fields are used; the sum of descriptions fits the smaller listing budget (1% of the Claude window, or the Codex skills budget).

**Telemetry**, written at user scope by `tac init --user` and verified by `tac doctor`. The receipt records the exact opt-outs, the observed connection attempts and the remaining uncertainty, since an egress trace cannot prove absence on allowed inference endpoints.

| Harness | Off switch | Note |
|---|---|---|
| Claude Code, host | the partial tier: `DISABLE_ERROR_REPORTING=1`, `DISABLE_FEEDBACK_COMMAND=1`, surveys and pinned updates off | the full set (`DISABLE_TELEMETRY`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`) also cuts Remote Control, auto mode and artefacts, so it is used only in containers and CI; Q15 |
| Codex | user config: `[analytics] enabled = false`, `[feedback] enabled = false`, `[otel] exporter = "none"`, `trace_exporter = "none"`, `metrics_exporter = "none"`, `log_user_prompt = false` | `otel` is ignored at project scope ([config reference](https://developers.openai.com/codex/config-reference)); other keys are not claimed to be ignored |
| Copilot CLI | the account-level opt-out, unverified; enterprise `telemetry.enabled = false` disables configured OTel export only | marked unverified; `GH_TELEMETRY=false` for `gh` separately |
| opencode | `share = "disabled"` disables publishing conversations only | general telemetry absence is unverified |
| pi | `enableInstallTelemetry = false` and `enableAnalytics = false` | the former does not stop update checks |
| Gemini CLI | `privacy.usageStatisticsEnabled = false` and `telemetry.enabled = false` | plus controlled environment overrides |

**As built in M4.** Seventeen skills ship under `.agents/skills/`, each with `metadata.source`, `metadata.author`, `metadata.licence` and `metadata.changes` in its frontmatter, and `THIRD_PARTY.md` carries the notice of the three that are not the owner's (ponytail, verification-before-completion, webapp-testing). `tests/test_skills.py` is the lint: name equals folder and is at most 64 characters, description at most 1,024, body under 500 lines, only specification fields, provenance present, the sum of descriptions under the listing budget the test derives and cites, and no em dash. `ponytail` carries `metadata.opt_in: true`: `tac sync` links every other skill into `.claude/skills/` and skips an opt-in one, and the lint refuses an opt-in skill in any team's `skills` list or in a review stage. The `build` stage of `order.toml` names it after `work-order`, so it reaches the builder's prompt and no other. The teams' `skills` lists give each team the skills its paths call for: core gets house-style, hooks-authoring, research-first, changelog, adr, shared-memory and verification-before-completion; harness gets house-style, justfile, hooks-authoring, research-first, changelog, semver, shared-memory and verification-before-completion; docs gets house-style, adr, readme-quickstart, mermaid-diagrams, owner-report and shared-memory. The skills for another kind of project (data-platform, gitlab-ci, webapp-testing) stay on no team until a project of that kind adopts them. `skills/_index/` is not generated yet; the lint computes the budget from the folders, and the index waits for a release where the base checker knows the output. No caveman skill, package or seed ships.

## 13. CLI, init, validation, dev/

**Languages.** Python 3.12 for `tac` (click for the CLI, pydantic models with `extra = "forbid"` for every config file, jinja2, jsonschema); bash 3.2 for `bootstrap.sh`; TOML for everything authored; JSON Schema for contracts; YAML only where a tool demands it (GitHub, compose, SodaCL, pre-commit). The hook guard is stdlib and Python 3.9 compatible (section 9).

**Commands.**

- `tac init [--user] [--answers file.toml]`: runs the question bank; detects installed clients and versions; chooses profile and harnesses; previews the diff; never overwrites unknown config; never grants hook trust; with `--user`, writes the user-scope telemetry and Remote Control keys.
- `tac sync`, `tac check [--staged | --commit-msg | --base <sha>]`, `tac doctor [--json]`, `tac explain [key]`, `tac config launch-command [role]` (the command that starts a host-session role, which `just chief` runs), `tac set <key> <value>` (writes a proposal), `tac apply` (host-side, applies a proposal), `tac migrate` (config `schema_version` migrations).
- `tac run <pipeline> --order <id>`, `tac handoff render|validate|receipt`, `tac launch <harness> [--role r] [--order id]` (adds `--add-dir` on the absolute worker store for Codex and `--settings` overlays for Claude, and compares the generated surfaces with the lock first).
- `tac memory add|search|index|promote|lint|select`, `tac session log`, `tac room read|say|slot`.
- `tac human ask|answer|render|verify`, `tac approve` (host only, keychain key), `tac recap`.
- `tac work new|validate|check|review|accept|plan|ci` (ported from vibe-map `tools/work.py`).
- `tac github apply`, `tac inbox watch`, `tac usage`, `tac trust-status`.
- `tac runner` (the host process of section 10), `tac deploy` (host-side re-stamp and venv rebuild after a merge), `tac stamp <dir>` (with an uninstall manifest), `tac receipt client`.

`just` recipes wrap each command, one short line per recipe. Every in-session call goes through `uv run --frozen --no-sync --project .agents`.

**Defined by inputs and outputs.** `tac receipt client --harness <h> --probe <name>` takes a probe name from `config/probes.toml` (discovery from the root, a subdirectory and a worktree; trust state; hook firing; a refused spawn; a denied write; a keychain read), runs it on the installed client, and writes a signed receipt `{harness, client_version, probe, expected, observed, effective_settings, requested_model, actual_model, requested_effort, actual_effort, billing_route, exit, created}` into the controller store and `work/orders/<id>/receipts/`; it exits non-zero when observed differs from expected or a required field is missing. `just work-repair <id>` takes an order whose `work-check` failed, reads `result.json`, renders the failing criteria into the work-order `repair` handoff for the builder role (`src/tac/templates/handoffs/repair.md.j2`, which answers a failed check; a result that fails its contract is repaired from `contract-repair.md.j2`, section 5), runs one bounded builder turn and re-runs `work-check`, exiting with that check's status. `just work-review <id>` runs the review stage's checks: a `review.toml` bound to the current sha, written by a name other than the builder's, from the other provider.

**Headless runs on a subscription.** Claude: `claude -p --settings <rendered overlay> --setting-sources project --strict-mcp-config --mcp-config <rendered mcp file> --output-format json --json-schema <contract> --max-turns <n> --max-budget-usd <x>` ([headless](https://code.claude.com/docs/en/headless)). Never `--bare`, which would skip the subscription login and bill an API key outside `[usage]`; without the other flags, a stage would load the owner's user hooks, MCP servers and plugins. The receipt records the billing route, and a run that would use an API key is refused unless Q16 allows it. Codex: `codex exec --output-schema <contract> -o <file> --add-dir <worker store>` with the project trust enrolled ([non-interactive](https://developers.openai.com/codex/noninteractive)).

**Compatibility policy.** TAC releases follow semver; a `schema_version` bump ships with a `tac migrate` step; adapters are pinned to client versions in `config/capabilities.toml`, and a client beyond the matrix sets `tac doctor` to unknown for that harness until a receipt updates the matrix.

**Bootstrap.** `bootstrap.sh` (bash 3.2) installs uv and mise if absent, runs `uv sync --frozen --no-editable --project .agents`, then runs `tac init`. The question bank, each question with its default and the key it writes:

```toml
# templates/init/questions.toml: an agent answers from a file with --answers; a human answers in the terminal.
schema_version = 1

[[questions]]
id = "kind"
text = "What does this repository build?"
options = ["app", "tool", "library", "data-pipeline", "document", "mixed"]
default = "tool"
writes = "project.kind"

[[questions]]
id = "profile"
text = "Which profile? standard is right unless you need container isolation."
options = ["standard", "enterprise", "yolo"]
default = "standard"
writes = "profile.active"

[[questions]]
id = "harnesses"
text = "Which harnesses are installed here? Detected ones are preselected."
default = ["claude", "codex"]
writes = "harnesses.enforced"

[[questions]]
id = "identity"
text = "The GitHub machine account the agents act as (no admin)."
default = "tac-bot"
writes = "governance.agent_identity"

[[questions]]
id = "secrets"
text = "Where the real agents.env lives, outside the repository."
default = "~/.config/toms-agent-config/<project>/agents.env"
writes = "secrets.env_file"

[[questions]]
id = "telemetry"
text = "Telemetry tier on this machine: partial keeps Remote Control; full is for containers."
options = ["partial", "full"]
default = "partial"
writes = "telemetry.host_tier"

[[questions]]
id = "usage"
text = "Usage thresholds per provider: the share of the usage window at which to slow down and to stop spawning."
default = { claude = { slow_at = 0.60, stop_at = 0.85 }, openai = { slow_at = 0.50, stop_at = 0.70 } }
writes = "usage"

[[questions]]
id = "runtime"
text = "Runtime store: the git common dir, or an absolute path."
default = "git-common-dir"
writes = "memory.runtime_store"
```

**Validation and tests.** pytest for the generator, schemas, envelope, selector, memory and work modules; drift tests; hook tests with piped stdin fixtures for both providers plus degraded modes; a test that every fenced TOML block in the docs parses; the diagram render in CI; a nightly adapter contract run on every installed client with version-bound receipts. `tac check` runs before `tac sync`, on staged content, and sync idempotence is a separate test. A Python 3.9 runtime, the minor macOS ships as `/usr/bin/python3`, resolved through uv, runs the guard's tests. Live adapter tests run on each enabled client with no skipped cases. Provider schema subsets, refusals and truncated results are tested against both native structured-output systems. The acceptance commands `just verify`, `just adapter-proof`, `just container-proof` (1.1), `just dev-proof`, `just coverage-proof` and `just adoption-roundtrip` each exit non-zero on missing evidence and bind their results to the tested revision.

**The `dev/` proving ground.** `dev/ledger` is a small real tool: a CLI that ingests a CSV of expenses, validates it through the data gate (a schema drift check, and the Soda scan once Q13 enables the extra), stores it in DuckDB and prints a monthly report. It needs a data-quality gate, a semver bump, a changelog fragment, an ADR and one Mermaid diagram, so every standard is exercised. It is built from an issue through the `order` pipeline: the chief specifies, a builder builds, gates verify, the other provider reviews, a manager signs off, and the recap writes `TODO.HUMAN.md`.

A coverage manifest is generated from the configuration: every hook, script, pipeline stage, handoff, memory operation, human transition and enforcement layer has a test id and an observed receipt, and a missing entry fails `just coverage-proof`. The proof exercises all five pipelines, both signoff branches, rejection, timeout without consent, malformed output, a stale review, a forged approval, an altered receipt, a policy tamper, denied ownership, crash and restart, a repeated external effect, one mutation run per required hook, and the guard failure modes of section 5. Native-client probes run for every enabled client across the root, a subdirectory and a worktree; fresh, resumed and headless sessions; trust states and profiles; recording actual model and effort and the effective settings. Synthetic answers prove the queue mechanics, and one real workspace trust, one hook trust and one human approval are witnessed separately. A Python fixture calling `run.py` is never evidence that a client fired it. vibe-map adopts only after `dev/` is green on both providers.

**As built in M4, the offline part.** `dev/ledger` is the small real tool: `ledger ingest <csv> --db <file>` validates the header and types against `dev/ledger/schema.json` (the schema drift gate, exit 3 with the differing columns named), stores the rows in DuckDB and `ledger report --month YYYY-MM` prints the monthly totals; it has its own `pyproject.toml` at 0.1.0, `uv.lock`, tests, `CHANGELOG.md`, ADR 0001 and `docs/diagrams/ingest.mmd`. `tac proof coverage` derives the condition ids from the configuration (every hook check per wired client, every pipeline stage and gate, every handoff template and contract, every memory command, every human kind and transition, the four enforcement layers, every doctor check, C1 to C6 and the section 9 bypass list), reads `dev/coverage/map.toml`, refuses a condition without an entry and a test id that does not collect, runs the mapped tests, and writes `dev/out/coverage.json` bound to `git rev-parse HEAD` and a clean tree. `tac proof dev` runs the ledger's verify and drift gate, `tests/test_bypass.py` and `tests/test_mutation.py` (one run per hook check per client with that check removed from the render, asserting the guard then lets the call through that it refused before), then evaluates every live probe's `requires` against `tac doctor --json` and the answered items in `.human/approvals/` and prints `SKIP live: <probe>: <reason>` for each one it cannot run; it never marks a live probe passed without a runner receipt. The live probes, C1 and C6 on Claude and Codex across the root, a subdirectory and a linked worktree and across fresh, resumed and headless sessions, are defined in `src/tac/proof.py` with the probe kinds added to the schema, each with a slug the runner's probe request accepts (lowercase, hyphenated, at most 32 characters) that keys it in `probes.toml`, wait for S5, Q24, S3 and the owner resuming Codex, and move into `config/probes.toml` as follow-up config. The pipeline run of the ledger from an issue to the recap waits for the same and for Q14. A policy tamper written through a shell is refused at commit where the policy schema holds the value (dropping `.agents/**` from `deny_write` is refused by `tac check --staged` and by `tac sync`); a loosening of `deny_read` is refused at commit only while the lock is stale, and once synced it is a reviewable configuration change caught at layer 3b by CODEOWNERS on `.agents/`, with a floor pin for `deny_read` listed as follow-up config.

## 14. Docs and diagrams

`docs/HARNESS.md` is the operator's page: the short version, where everything is, how to change a knob, how to check, how to recover, how to try it on another machine. `docs/adr/` holds the decision records. `docs/diagrams/README.md` documents the house symbol subset of ISO 5807:1985 and the review checklist from ISO/IEC/IEEE 42010:2022 (stakeholders, concerns, viewpoints, views); C4 context, container and component views are the presentation. ISO 8601 is used for every date in text, frontmatter and file names (the basic form in file names).

The diagram inventory is exactly the seven lifecycles (init, sync, hook, memory, board, human loop, release) plus one per pipeline, listed in `config/github.toml` and checked by `tac check`. Rendering is a pinned loop, one output per diagram, with the version from `mise.toml`:

```sh
for f in docs/diagrams/*.mmd; do
  npx --yes @mermaid-js/mermaid-cli@<pinned> -i "$f" -o "/tmp/$(basename "$f" .mmd).svg"
done
```

Rendering proves syntax only; the reviewer of any order that changes a diagram checks its semantics against the checklist.

The architecture in one view:

```mermaid
flowchart TB
    subgraph authored [".agents/ authored, read-only to agents"]
        CFG[("config.toml + config/")]
        CON[("contracts/ schemas")]
        TPL[("templates/ Jinja")]
        SK[("skills/")]
        HK[("hooks/ run.py")]
        MEMR[("memory/records + _index")]
    end
    SRC[("src/tac, hooks, templates, contracts<br/>the product source")] --> GEN["tac sync + generated.lock<br/>run by the trusted runner"]
    GEN --> CL[("CLAUDE.md, .claude/")]
    GEN --> CX[(".codex/")]
    GEN --> OT[(".github/agents, opencode.json, .pi/, .gemini/")]
    CFG --> GEN
    CON --> GEN
    TPL --> GEN
    SK --> GEN
    CL --> H1["Claude Code"]
    CX --> H2["Codex"]
    OT --> H3["Copilot, opencode, pi, Gemini"]
    H1 & H2 & H3 --> HK
    HK --> CHK{"tac check<br/>one checker"}
    H1 & H2 & H3 --> RUN["tac run: pipelines,<br/>envelopes, gates"]
    RUN --> RT[("runtime stores<br/>worker and controller")]
    RT --> PROM["trusted runner:<br/>tac memory promote"]
    PROM --> MEMR
    RUN --> HUM[/"TODO.HUMAN.md, recaps, approvals"/]
    CHK --> GIT{"prek git hooks"}
    GIT --> CI{"protected CI"}
    classDef proc fill:#0067A5,stroke:#0088CC,color:#FFFFFF
    classDef dec fill:#FFBF00,stroke:#FFD500,color:#000000
    classDef io fill:#FF8C1A,stroke:#FFA94D,color:#000000
    classDef store fill:#9A2A2A,stroke:#F04923,color:#FFFFFF
    class GEN,H1,H2,H3,RUN,PROM proc
    class CHK,GIT,CI dec
    class HUM io
    class CFG,CON,TPL,SK,HK,MEMR,RT,CL,CX,OT,SRC store
```

Legend: blue process, yellow decision, orange input or output, dim red data store.

**As built in M4.** `docs/diagrams/` holds twelve sources, `lifecycle-<name>.mmd` for the seven lifecycles and `pipeline-<name>.mmd` for the five enabled pipelines, plus the README with the house subset and the 42010 review checklist. `tac diagrams render` runs `npx --yes @mermaid-js/mermaid-cli@<pinned>` with the version `mise.toml` pins under `[tools]`, renders every source and every fenced mermaid block under `docs/` into a scratch folder, exits non-zero on any failure, and writes `docs/diagrams/render.lock` (the mermaid-cli version and the sha256 of every source and block). No SVG is committed: renders are not byte-stable across versions, so the lock binds the sources to the last successful render, `tac diagrams check` and `tests/test_diagrams.py` judge it without node, and `tac check` refuses an inventory that does not match `github.toml`. CI checks the lock on every pull request; the render runs locally until the owner decides how CI runs headless Chromium on the ubuntu runner.

## 15. The toolchain

Pinned in `.agents/pyproject.toml` and `mise.toml` (the CLIs); licence and telemetry per item. "None documented; egress-tested" means the tool documents no telemetry and the `dev/` egress test sees no unexpected host. A licence allowlist test runs over the resolved lock.

| Tool | Role | Licence | Telemetry | Release |
|---|---|---|---|---|
| [uv](https://docs.astral.sh/uv/) | environments and locks | MIT or Apache-2.0 | none documented; egress-tested | 1 |
| ruff | lint and format, pre-commit and CI | MIT | none documented; egress-tested | 1 |
| basedpyright | types, basic mode | MIT | none documented; egress-tested | 1 |
| pytest, pytest-xdist | tests, parallel shards | MIT | none documented; egress-tested | 1 |
| pydantic | config and envelope models, `extra = forbid` | MIT | none documented; egress-tested | 1 |
| jinja2 | handoff and adapter templates | BSD-3-Clause | none documented; egress-tested | 1 |
| jsonschema, check-jsonschema | contracts, config and workflow schemas | MIT, Apache-2.0 | none documented; egress-tested | 1 |
| click | the `tac` CLI | BSD-3-Clause | none documented; egress-tested | 1 |
| rich | tables in `explain` and `doctor` | MIT | none documented; egress-tested | 1 |
| polars | data helpers in `dev/` and data pipelines | MIT | none documented; egress-tested | 1 |
| duckdb | storage in `dev/ledger`; memory analytics deferred | MIT | none documented; egress-tested | 1 (analytics 1.1) |
| taplo | TOML format and schema check, keeps comments | MIT | none documented; egress-tested | 1 |
| [mermaid-cli](https://github.com/mermaid-js/mermaid-cli) | render diagrams in CI | MIT | none documented; it drives a headless browser | 1 |
| [prek](https://github.com/j178/prek) | runs the `.pre-commit-config.yaml` hooks; single binary, no Python, uv-aware | MIT | none documented; egress-tested | 1 |
| towncrier | assembles changelog fragments at release | MIT | none documented; egress-tested | 1 |
| [gitleaks](https://github.com/gitleaks/gitleaks) | secrets at pre-commit and CI | MIT | none documented; egress-tested | 1 |
| pip-audit | dependency vulnerabilities in CI | Apache-2.0 | queries the OSV and PyPI advisory APIs, which is the check itself | 1 |
| just | recipes | CC0-1.0 | none documented; egress-tested | 1 |
| actionlint, shellcheck | workflow and shell lint | MIT, GPL-3.0 (used as a tool, not linked) | none documented; egress-tested | 1 |
| rumdl | Markdown lint, including the em-dash rule | MIT | none documented; egress-tested | 1 |
| sqlfluff | SQL lint where a project has SQL | MIT | none documented; egress-tested | 1 |
| [mise](https://mise.jdx.dev/) | pins the CLIs (needs `mise trust` once) | MIT | none documented; egress-tested | 1 |
| [copier](https://copier.readthedocs.io/en/stable/) | stamping and updates (`--trust` for tasks) | MIT | none documented; egress-tested | 1 |
| commitizen | not used: Q4 chose what-and-why, ADR 0002 | MIT | none documented; egress-tested | no |
| hypothesis | property tests for the selector and the config merger | MPL-2.0 | none documented; egress-tested | 1.1 |
| ipykernel, nbstripout | notebooks where a project has them; outputs stripped at commit | BSD-3-Clause, MIT | none documented; egress-tested | 1.1 |
| vale | prose style, optional | MIT | none documented; egress-tested | 1.1 |
| soda-core v3 | data-quality gate, SodaCL YAML, local DuckDB connector, no Soda Cloud | Apache-2.0 ([v3.5.6 licence](https://github.com/sodadata/soda-core/blob/v3.5.6/LICENSE)) | `send_anonymous_usage_stats: false` in the selected configuration, which is searched in the home directory before the project; proven by an offline passing and failing scan without Cloud credentials | optional extra `tac[data]`, 1.1 opt-in per Q13, ADR 0002 |
| great_expectations | reference only, not shipped | Apache-2.0 | usage statistics on by default | no |

Soda: the supported line is soda-core v3 (3.5.6). The current v4 line is under the Elastic License 2.0 with telemetry reported on by default (its opt-out variable is not independently verified) and is not used. Soda is an optional extra, enabled only when `project.kind` includes data-pipeline, so the MIT core never depends on it. `gh` ships with `GH_TELEMETRY=false`.

Why prek over pre-commit and lefthook: it reads the standard `.pre-commit-config.yaml`, needs no Python and integrates with uv. Why towncrier over git-cliff: fragments are the changelog model in use, and towncrier is the fragment tool; git-cliff derives the changelog from commits, and Q4 decided the commit convention as what and why, not Conventional Commits (ADR 0002).

## 16. Reuse list with licences

| Component | Source | Licence | Use |
|---|---|---|---|
| an earlier cross-provider harness design: one `.agents/` source, generated per-harness files, a lock, files-plus-index memory, a strict container profile, one hook dispatcher | [vibe-map issue #193](https://github.com/tpetedb/vibe-map/issues/193) | the owner's own work | taken, generalised |
| `tools/work.py`, `work/`, the work-order ADR | [vibe-map](https://github.com/tpetedb/vibe-map) main | MIT (owner) | ported as `tac work` |
| `tools/board.py`, the memory schema, their tests | [vibe-map PR #192](https://github.com/tpetedb/vibe-map/pull/192) | MIT (owner) | ported as `tac room` and `tac memory` |
| `harness.py`, templates, profiles, the lock | [vibe-map PR #197](https://github.com/tpetedb/vibe-map/pull/197) | MIT (owner) | ported as `tac sync`, `check`, `doctor`, `explain` |
| strict and sandboxed Jinja rendering, a staleness hook, a per-task model router, a status log, comment-keeping TOML edits | the owner's documentation pipeline that renders Jinja scaffolds and validates them (private) | the owner's; code moves in under MIT with an authorship line | taken |
| the `TODO.HUMAN.md` format, one charter with thin adapters pinned by a test, a gate runner for `just`, an em-dash check, tighten-only overrides | a private platform repository | nothing is redistributed: no code is copied from it (Q19) | re-implemented from scratch here as patterns, each with an authorship line, per Q19, ADR 0002 |
| commented TOML style, symlink sync, the hook `.test.sh` pattern, several skills | the owner's private dotfiles toolbox | the owner's | taken |
| a versioned JSONL bus and an IDE registry | the owner's private workspace orchestrator | the owner's | adapted |
| session-bus design | [earendil-works/pi](https://github.com/earendil-works/pi) | MIT | credited, re-implemented |
| diagnostic record and handoff receipt shapes | archify | MIT | shapes adopted, code not vendored |
| ponytail, trimmed | [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) | MIT | vendored, trimmed, as an opt-in skill for builders, its notice in `THIRD_PARTY.md` when it lands; decided per Q1, ADR 0002 |
| `AGENTS.md` format | [agents.md](https://agents.md/) | MIT | followed |
| Agent Skills specification | [agentskills.io](https://agentskills.io/specification) | specification | followed; skill bodies not vendored |
| issue forms, label-sync, github-script, release-drafter | GitHub, [EndBug/label-sync](https://github.com/EndBug/label-sync), actions/github-script, [release-drafter](https://github.com/release-drafter/release-drafter) | first-party, MIT, MIT, ISC | taken |
| devcontainers specification, features, CLI | [devcontainers](https://github.com/devcontainers/spec) | CC-BY-4.0, MIT | taken |
| recipe fields (`response`, `retry.checks`, `sub_recipes`) | block/goose | Apache-2.0 | field shapes borrowed |
| run-state approval shape | [openai-agents-python](https://openai.github.io/openai-agents-python/human_in_the_loop/) | MIT | shape mirrored |
| ruler, rulesync | [intellectronica/ruler](https://github.com/intellectronica/ruler), [dyoshikawa/rulesync](https://github.com/dyoshikawa/rulesync) | MIT | references only |
| caveman, caveman-micro | public repositories | MIT plus BSL-1.1 | not used |
| basic-memory, MCP reference servers | [basic-memory](https://github.com/basicmachines-co/basic-memory), [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) | AGPL-3.0, NOASSERTION | not used as code; MCP servers optional and pinned |

`THIRD_PARTY.md` lists every line above that is not the owner's, with the notice text. TAC itself carries MIT (Q9).

## 17. Trade-offs resolved

1. **Codex treats `.agents` as read-only, yet memory was wanted under `.agents/`.** `.agents/` holds schemas, seed, promoted records and generated indexes; live journals, sessions, traces, envelopes and receipts live in the runtime stores; the runner promotes records through a pull request. No agent ever holds a writable root on `.agents/`; workers write proposals.
2. **Where `config.toml` lives.** In `.agents/config.toml`, changed by humans directly, or by agents through proposals and a pull request or a host-side `tac apply`.
3. **`agents.env` against the `*.env` deny.** The deny stays; the secrets file lives outside the repository by default and is injected, never read (Q17 for the root option).
4. **Profiles.** Enterprise, standard and yolo, each a complete file with no `gates` key; the floor and waivers carry the gates, so yolo relaxes approvals, never gates.
5. **Symlinks against generated files.** Generated files are the baseline; per-skill relative links for Claude only, made at setup and never committed; a skill folder that is itself a symlink is refused.
6. **Adapters.** Native discovery first; only what a harness cannot discover (agents, settings, hooks) is rendered. Copilot has hooks and gets them in 1.1; Gemini has agents and hooks in `settings.json`; opencode gets a registered plugin; pi an extension.
7. **Commit convention.** The registry carries either form; the default stays "what and why, one line" until Q4.
8. **Codex project trust.** A user-level trust entry, written idempotently by `tac init --user` or by the client's prompt; `tac doctor` reports project trust and hook trust separately, and untrusted hooks warn at startup.
9. **The board.** Quorum of three, Fable tallies, the Opus director is a separate session from the Opus chief, and the release stage needs one director.
10. **A terse skill derived from caveman.** Dropped; caveman itself stays closed.
11. **Envelopes and receipts.** Envelopes stay in the worker store; digests go to memory; receipts are runner-signed, committed under the order, and recomputed by CI rather than trusted from the tree.
12. **Confidence.** The level is set by the assessing role; the basis is recomputed by the trusted writer from checked evidence; the model never writes the basis.
13. **Worktrees.** Created by script outside the repository, `~/.worktrees/<repo>/<name>`, so a Codex session in the main checkout cannot write a nested agent worktree.
14. **Codex reads project hooks from the main checkout in linked worktrees.** Hook changes take effect once main has them; an instruction digest is a fallback for context, never a replacement for a missing enforcement hook.
15. **Product source against the dogfood copy.** The source is `src/tac/`; `.agents/` is a stamped copy, so the order that builds the hooks does not lock what later orders edit.
16. **Scope.** The full target stays; the build is phased, and the cut line is release 1 as section 18 names it (Q18).

## 18. Releases and milestones

**How every order is built and accepted.** The Claude team builds on Opus 5.5 and the other provider reviews. Every order carries a version-bound client receipt. `tac check` runs before `tac sync` in every acceptance. Two command prefixes appear below, and the difference is build condition C4, "test the new code, judge with the deployed copy":

```sh
# The judge: the deployed, non-editable copy in .agents/.venv, read-only to agents.
uv run --frozen --no-sync --project .agents tac check
# The candidate: the worktree's own root project, src/ installed editable, in an
# environment the runner builds. Tests of new code always run here.
uv run --frozen pytest -q tests/
```

**The six build conditions.** The board's final verdicts attached six conditions. Each is an acceptance item of the milestone that owns it, and no order is accepted while its condition is open.

| Id | Raised by | Condition, in one line | Owned by |
|---|---|---|---|
| C1 | Astra 6 | guard failure is explicit: missing, altered and reused tokens, runner loss and hook timeout are tested; where refusal must survive hook failure, the profile disables native delegation | M3 `tac-runner`, proven in M4 `tac-dev-proof` |
| C2 | Astra 6 | hook execution matches the claimed isolation: absolute trusted executables, a controlled environment, isolated imports, proven against writable modules, `PATH` and project config | M2 `tac-hooks` |
| C3 | Astra 6 | receipt trust is pinned: CI takes `runner.pub` from the trusted base, signatures bind repository, revision, run, stage and policy, replay and a candidate key are rejected, the runner signs only what it observed | M0 `tac-bootstrap`, M2 `tac-human-github` |
| C4 | Opus 5.5 | test the new code, judge with the deployed copy: candidate tests from the worktree project, `.agents/.venv` non-editable from a named source, the Python 3.9 test runs the source guard, order 0 accepted on the host; the strict-subset lint covers model-facing contracts only | M0 `tac-bootstrap`, M1 `tac-core` |
| C5 | Opus 5.5 | M0 and M5 check what they claim: rulesets fetched by id with the owner's token, vibe-map's tools kept as thin wrappers or ported test ids named, the prek fixture offline | M0 `tac-bootstrap`, M5 `tac-adopt-vibe-map`; the prek clause lands in M2, where the fixture lives |
| C6 | Opus 5.5 | the owner's token never reaches the runner: it holds only its signing key and the `tac-bot` token, the live proof checks both keychain entries, the merge command is squash with `--match-head-commit` | M3 `tac-runner`, M4 `tac-dev-proof` |

### Release 1: Claude Code and Codex enforced, the standard profile

The scope, as the owner decided on 2026-09-25 (Q18, [ADR 0002](adr/0002-owner-decisions-2026-09-25.md)): Claude Code and Codex enforced, the standard profile only. The enterprise and yolo profiles, containers, and the Copilot, opencode, pi and Gemini harnesses come in release 1.1; until then Gemini and Copilot, like opencode and pi, are instructions only (Q10), and Copilot is the CLI alone (Q7).

**M0, order 0 `tac-bootstrap`.** Builds: `tac work` ported from vibe-map `tools/work.py` with the same `just work-*` recipes plus `work-repair` and `work-review` (section 13); the runner skeleton (`tac runner` with the controller store, the socket, signed receipts and `tac receipt client`), so the runner exists before its consumers; a skeleton `ci.yml` on `pull_request`; `.github/CODEOWNERS`; `tac github apply` and the ruleset; `bootstrap.sh`; `.agents/pyproject.toml` and `uv.lock`; client discovery, minimal launch and trust probes.

Build conditions owned here:

- C3: the skeleton's receipts bind repository, revision, run, stage and policy hash; `ci.yml` reads `runner.pub` from the base revision or a separately provisioned repository setting, never from the candidate; a replayed receipt, a receipt for another revision and a candidate-supplied key are each rejected; the runner signs only its own observations. The gate child is isolated (section 10, gate children): it runs under a Seatbelt profile whose writes are an allowlist (the checkout minus `.agents/`, `.git`, `.claude/`, `.codex/` and the git common dir, plus a private scratch folder that is its HOME, TMPDIR and caches), that allows connects only to Unix sockets in its scratch folder and to the DNS resolver's socket and denies IPv4 loopback and all outbound IPv6, that denies reading the controller store and the key, the runner socket, renaming any folder above the store, Apple Events, LaunchServices and preference writes, with a scrubbed environment, and the runner refuses to gate where no sandbox exists; `test_runner_skeleton.py` proves a criterion that reads or writes the key, moves the store, reaches the runner socket or reads a token fails; that a criterion connecting to a Unix socket the test binds outside the scratch folder, or to a loopback port on 127.0.0.1, ::1 or the v4-mapped ::ffff:127.0.0.1, fails and the server sees no connection, while one that binds and reaches a socket under its own TMPDIR passes; that a criterion writing the owner's `~/.claude/`, `~/Library/LaunchAgents/` or uv cache, the judge's `.agents/.venv`, the stamped toolchain, `.claude/`, the shared git hooks or the git config fails and leaves nothing, in a main clone and in a linked worktree, by absolute path and through a planted link; that `git status` runs sandboxed, with a filter that marks it ran and cannot copy the key, and cannot write the tree; and that the unsupported platform refuses. What holds at M0: code a builder wrote, run by the runner, cannot read or change the key or the store, and cannot write any place the builder's own sandbox cannot, and cannot connect to a Unix socket outside its scratch folder or to a loopback port, over IPv4 or IPv6; so it cannot plant code where the owner's processes later load it (a Claude hook, a LaunchAgent, a git hook, the judge's venv), cannot ask a process the owner runs unsandboxed over a socket (tmux, Docker, a local dev server) to run a command for it, and cannot change the judge (C4). What waits for M3 (C6): the key is still a file the owner's user can read from any unsandboxed process, such as a Claude hook or another terminal; a system service outside the sandbox that acts for its caller over Mach IPC and is not on the profile's deny list, and a service the owner runs listening on this Mac's non-loopback address, are not covered; and the runner's own memory is protected only by macOS's debugger rules. The keychain entry, or a dedicated macOS user, closes the key file and the memory gaps; the two service gaps stay named here until M3 chooses between a dedicated user and a narrower profile, and the live proof of M4 checks all of them.
- C4: `.agents/.venv` is built with `uv sync --frozen --no-editable --project .agents` from the named source; order 0 is accepted by a run on the host, since no runner exists yet to judge it.
- C5: `tac doctor` fetches each active branch ruleset by id with the owner's token on the host, fails when `bypass_actors` is missing or non-empty, and requires code-owner review and required checks (section 8).

Acceptance, run on the host by the owner:

```sh
just verify \
  && uv run --frozen --no-sync --project .agents tac doctor --json > /tmp/doctor.json \
  && uv run --frozen pytest -q tests/test_doctor_exit.py tests/test_doctor_ruleset.py tests/test_receipt_trust.py
```

`test_doctor_exit.py` proves `tac doctor` exits non-zero on a seeded failure; `test_doctor_ruleset.py` proves it fails on a missing or non-empty `bypass_actors`, a tag-only ruleset, and a ruleset without code-owner review or required checks; `test_receipt_trust.py` covers the C3 rejections. M0 cannot pass before the owner answers Q14 and runs `tac github apply`.

**M1, orders 1 `tac-core` and 2 `tac-contracts`.** `tac-core`: config, schemas, sync, check, explain, the lock, the floor, waivers, and the Claude and Codex adapters. `tac-contracts`: strict-subset contracts, templates, `tac handoff`, the pipeline checker, and the five pipeline files with their intake and effect stages.

Build condition owned here: C4, candidate tests from the worktree project and the judgement from the deployed copy; the strict-subset lint applies to model-facing contracts only, and config schemas keep optional keys and defaults.

Acceptance 1:

```sh
uv run --frozen tac check --staged \
  && uv run --frozen tac sync \
  && uv run --frozen tac check \
  && git diff --exit-code -- CLAUDE.md .claude .codex \
  && uv run --frozen pytest -q tests/test_core.py tests/test_toml_docs.py tests/test_sync_idempotent.py \
  && uv run --frozen --no-sync --project .agents tac check
```

Acceptance 2:

```sh
uv run --frozen pytest -q tests/test_handoff.py tests/test_pipelines.py tests/test_contracts.py
```

**M2, orders 3 `tac-hooks` and 4 `tac-human-github`.** `tac-hooks`: the guard, its checks, the wiring for Claude and Codex, `hooks/git/`, and prek installed for pre-commit, commit-msg and pre-push, exercised with an invalid message and a blocked push. `tac-human-github`: `tac human`, `tac approve`, approval verification through the GitHub API, `TODO.HUMAN.md` rendering, issue forms, labels, and `ci.yml` with the base-revision check.

Build conditions owned here:

- C2 (`tac-hooks`): rendered hook commands use absolute trusted executables, a controlled environment and `python -I`; `test_hook_isolation.py` proves that a writable module in the worktree, a shim on `PATH`, and a project `uv.toml` or `pyproject.toml` change nothing about what the guard loads or runs; the trust diagram marks that Claude hooks run outside its Bash sandbox (section 10).
- C4 (`tac-hooks`): `test_guard_py39.py` runs the source `hooks/run.py`, the candidate, as a subprocess under Python 3.9 with piped fixtures, not the deployed `.agents/hooks/run.py`, which the builder cannot write and which changes only after `tac deploy`. The pin is the 3.9 language level that uv resolves, `/usr/bin/python3` on a Mac and `uv python install 3.9` in CI before the tests, not a patch release: the guard's contract is what every macOS 3.9 runs, and the test itself never reaches the network.
- C5, prek clause (`tac-hooks`): `test_git_hooks_fixture.py` creates a throwaway repository under a temporary directory, sets `PREK_HOME` inside that directory, uses system hooks so it needs no network, runs `prek install` for the three hook types there (never in the shared `.git/hooks`), commits an invalid message and pushes to a local bare remote, and asserts both are refused.
- C3 (`tac-human-github`): `ci.yml` verifies receipt signatures with the base-side key and rejects replay and a candidate key; `test_receipt_ci.py` runs those cases against the workflow's verifier.

Acceptance 3:

```sh
uv run --frozen pytest -q tests/test_hooks.py tests/test_git_hooks_fixture.py tests/test_guard_py39.py tests/test_hook_isolation.py
```

Acceptance 4:

```sh
uv run --frozen pytest -q tests/test_human.py tests/test_approvals.py tests/test_github_forms.py tests/test_receipt_ci.py && uv run --frozen actionlint
```

**M3, orders 5 `tac-memory` and 6 `tac-runner`.** `tac-memory`: add, search, index, promote, lint and select; events; the session log; the secrets scan. `tac-runner`: `tac run`, states, journaled effects, leases, checkpoints, `tac launch` for Claude and Codex with the lock comparison, the handoff guard with single-use dispatch tokens, `tac inbox watch`, `tac usage`, and subscription headless runs without `--bare`.

Build conditions owned here:

- C1 (`tac-runner`): `test_guard_failure.py` covers a missing token, an altered token, a token reused by two concurrent spawns, the runner lost mid-check and a hook timeout; the enterprise profile renders with `native_delegation = "off"` and the spawn tools absent. `tests/test_guard_failure.py` proves each case through the stamped guard and a real runner on a short socket, and `test_the_enterprise_profile_takes_the_spawn_tools_away` the render.
- C6 (`tac-runner`): the runner reads exactly two keychain entries, its signing key and the `tac-bot` token, and `test_runner.py` asserts it can name no other; the merge command it hands the owner is `gh pr review <N> --approve && gh pr merge <N> --squash --match-head-commit <sha>`. `test_the_runner_names_exactly_two_keychain_entries` and `test_a_third_keychain_name_is_refused_before_any_process_starts` in `tests/test_runner.py` prove the first half against a fake `security`, and `test_the_merge_command_is_squash_pinned_to_the_head_commit` with `test_an_order_runs_to_the_owner_s_approval_then_to_the_merge_command` the second.

Acceptance 5:

```sh
uv run --frozen pytest -q tests/test_memory.py tests/test_select.py
```

Acceptance 6:

```sh
uv run --frozen pytest -q tests/test_runner.py tests/test_launch.py tests/test_guard_failure.py && just adapter-proof
```

**M4, orders 7 `tac-skills-docs` and 8 `tac-dev-proof`.** `tac-skills-docs`: the skills of section 12 with provenance, the skill lint, `docs/HARNESS.md`, ADRs, and the diagrams with the pinned render loop. `tac-dev-proof`: `dev/ledger`, the coverage manifest, the bypass attempts, the mutation runs, and the probes across root, subdirectory, worktree and session kinds for the two enforced clients.

Build conditions owned here:

- C1, proven live (`tac-dev-proof`): on both clients, a spawn with a missing, an altered and a reused token is refused with a receipt; under the delegation-off profile the spawn tool is absent; runner loss and a hook timeout are exercised, and the receipt records what each client actually did (the documented behaviour is that a timed-out Claude hook lets the call continue, which is why the delegation-off profile exists).
- C6, proven live (`tac-dev-proof`): inside sandboxed Claude and Codex sessions, `security find-generic-password` fails for both the `tac-bot` token entry and the runner signing key entry.

Acceptance 7:

```sh
uv run --frozen --no-sync --project .agents tac check \
  && just diagrams-render \
  && uv run --frozen pytest -q tests/test_skills.py
```

Acceptance 8:

```sh
just dev-proof && just coverage-proof
```

**M5, order 9 `tac-adopt-vibe-map`.** In TAC: `copier.yml`, `tac stamp` with the uninstall manifest, and `just adoption-roundtrip` in a clean fixture (install, update, rollback). Then one order in vibe-map: first bring `tools/board.py` and `tests/test_board.py` over from the open [PR #192](https://github.com/tpetedb/vibe-map/pull/192), where they live (they are not on vibe-map main); stamp the pinned release; move [PR #197](https://github.com/tpetedb/vibe-map/pull/197)'s root `config.toml` into `.agents/config.toml`; put `tac room|memory` behind `tools/board.py`, keeping its 25 tests as the contract, and `tac work` behind `tools/work.py`, keeping the same `just work-*` recipes; close #192 and #197 with a comment naming what carried over. The landing step on the main checkout is done with the owner present.

Build condition owned here: C5. `tests/test_work.py` imports `tools.work` and asserts hooks wired to `tools/work.py`, and `tests/test_board.py` imports `tools.board`, so replacing the tools outright would fail at import. `tools/work.py` and `tools/board.py` therefore stay as thin wrappers over `tac`; if either is removed instead, the order names the ported test ids that must pass in its place.

Acceptance, `just adoption-roundtrip` in TAC, then in vibe-map:

```sh
uv run --frozen --no-sync --project .agents tac doctor \
  && just verify \
  && uv run pytest -q tests/test_board.py tests/test_work.py
```

### Release 1.1: the rest of the target

Each milestone has the same acceptance form: named test files run from the candidate, the judgement from the deployed copy, and a `just` proof that exits non-zero on missing evidence.

- **M6 `tac-containers`**: `.agentcontainers/` with the own-clone design, the six live checks, the enterprise and yolo profiles activated, `just container-proof`.
- **M7 `tac-harnesses`**: Copilot hooks and agents, the opencode plugin, the pi extension, Gemini agents and hooks, each enforced only after its receipt; `copilot-instructions.md` and a Zed `.rules` file if Q7 extends Copilot beyond the CLI.
- **M8 `tac-board-memory`**: automated quorum and tally, memory decay and fold, hypothesis tests, the Soda extra per Q13, vault support, release-drafter.

## 19. Risks and what is left out

**Risks and how they are met.**

- False portability: a release-pinned capability matrix and nightly client receipts; `tac doctor` reports unknown for a client beyond the matrix.
- Policy self-modification: the read-only `.agents/`, CODEOWNERS, the base-revision check, and a checker that changes only through a reviewed merge and a host-side deploy.
- Memory poisoning: provenance, derived status, a basis recomputed from checked evidence, and the promotion rules.
- Coordination overhead: narrow triggers for the board and one review per order.
- Hidden data movement: deny rules, the egress allowlist, and journaled side effects with idempotency keys.
- A client change that breaks discovery or trust again: `tac doctor` reports unknown and unattended runs refuse to start.
- The agent identity depends on the owner creating the account or App (Q14) and turning bypass off for admins; until then the boundary is CODEOWNERS and CI alone, and `tac doctor` says so.
- The keychain boundary may not hold on every client; the fallback is a dedicated macOS user for the runner, and `tac doctor` reports the boundary as unknown until proven.

**Left out of release 1:** the enterprise and yolo profiles, containers, the four harnesses beyond instructions, automated quorum, memory decay and fold, Soda, DuckDB memory analytics, hypothesis, notebooks, vale, vault support, release-drafter.

**Left out of the target:** semantic or vector memory, an OpenTelemetry collector, MCP memory servers, a nested inner sandbox inside the container, caveman, great_expectations, and rulesync as an engine.

## References

Vendor documentation the design relies on:

- Claude Code: [hooks](https://code.claude.com/docs/en/hooks), [sandboxing](https://code.claude.com/docs/en/sandboxing), [sub-agents](https://code.claude.com/docs/en/sub-agents), [memory](https://code.claude.com/docs/en/memory), [skills](https://code.claude.com/docs/en/skills), [headless](https://code.claude.com/docs/en/headless), [statusline](https://code.claude.com/docs/en/statusline), [devcontainer](https://code.claude.com/docs/en/devcontainer).
- Codex: [hooks](https://developers.openai.com/codex/hooks), [sandbox](https://developers.openai.com/codex/sandbox), [AGENTS.md](https://developers.openai.com/codex/guides/agents-md), [skills](https://developers.openai.com/codex/skills), [subagents](https://developers.openai.com/codex/subagents), [config reference](https://developers.openai.com/codex/config-reference), [advanced config](https://developers.openai.com/codex/config-advanced), [non-interactive](https://developers.openai.com/codex/noninteractive).
- GitHub Copilot CLI: [custom agents](https://docs.github.com/en/copilot/reference/custom-agents-configuration), [hooks](https://docs.github.com/en/copilot/reference/hooks-reference), [agent skills](https://docs.github.com/en/copilot/concepts/agents/about-agent-skills).
- opencode: [config](https://opencode.ai/docs/config/), [rules](https://opencode.ai/docs/rules/), [skills](https://opencode.ai/docs/skills/), [plugins](https://opencode.ai/docs/plugins/), [agents](https://opencode.ai/docs/agents/).
- pi: [settings](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md), [skills](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md), [extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md).
- Gemini CLI: [configuration](https://geminicli.com/docs/reference/configuration/), [GEMINI.md and context files](https://geminicli.com/docs/cli/gemini-md/), [skills](https://geminicli.com/docs/cli/skills/), [subagents](https://geminicli.com/docs/core/subagents/), [hooks](https://geminicli.com/docs/hooks/reference/).
- Formats and standards: [AGENTS.md](https://agents.md/), [Agent Skills](https://agentskills.io/specification), [Semantic Versioning](https://semver.org/), [ISO 5807:1985](https://www.iso.org/standard/11955.html), [ISO/IEC/IEEE 42010:2022](https://www.iso.org/standard/74393.html).
- GitHub: [issue forms](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms), [gh telemetry](https://docs.github.com/en/github-cli/github-cli/github-cli-telemetry).
