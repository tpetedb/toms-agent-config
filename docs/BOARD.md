# How the design was decided

The design in [DESIGN.md](DESIGN.md) was written and approved by a board of three AI directors on 2026-09-24. A chief of staff ran the process, and the owner set the requirements and keeps the final say. This page covers who sat on the board, how it worked and what it concluded. The board's working files stay private; this page is the public summary.

## The board

| Seat | Model | How it ran | What it did |
|---|---|---|---|
| lead director | Claude Fable 5.1 | a Claude Code session at the highest effort | wrote each version of the design and compiled the tally |
| director | Astra 6 (OpenAI), through Codex | a fresh `codex exec` per turn, so nothing carried over between turns | independent review and verdict |
| director | Claude Opus 5.5 | a separate, fresh Claude Code session | independent review and verdict |
| chief of staff | Claude Opus 5.5 | the owner's interactive session | ran the process, passed on the owner's steer, compiled the open decisions |

Two providers sat on the board on purpose: a design that one model family finds convincing can still fail on the other family's client, and TAC has to work on both.

## How it worked

**Research first.** Before any design existed, the board gathered:

- the vendor documentation for Claude Code and Codex, read first-hand;
- the documentation for the other harnesses: GitHub Copilot CLI, opencode, pi and Gemini CLI;
- the GitHub features the gate relies on: rulesets, CODEOWNERS, issue forms, Actions;
- the owner's existing repositories, for patterns already proven in use;
- orchestration frameworks, for pipeline files, memory records and human-in-the-loop approvals;
- open-source candidates, each with its licence and telemetry defaults.

The starting point was an earlier board decision on a cross-provider harness for [vibe-map](https://github.com/tpetedb/vibe-map/issues/193). Every claim in its fact base had already been checked against the vendor page, and those checks had corrected or refuted a handful. Everything went into one evidence pack. Astra wrote an independent opening position before the first draft, so the lead designer could not frame the question alone.

**The owner's steer.** During research the owner added four requirements with the same weight as the original brief: no fork of an existing project, with a `dev/` proving ground instead; enforcement that holds whichever harness does the work, with production-grade defaults; an agent toolchain environment inside `.agents/`; and data-quality testing for data work.

**Three rounds.**

1. Version 1 went to independent reviews by Astra and Opus. Every review item got a written disposition: accepted, changed, or declined with a reason.
2. Version 2 got two verdicts. Opus said go, with five changes. Astra said no-go, with two blockers. First, native subagent spawns were not actually blocked, because the `SubagentStart` hook event cannot block in either client. Second, it was unclear where authority lived at run time: which process holds the keys, and what stops an agent from editing the checker that judges it.
3. Version 3 closed both blockers. The handoff guard moved to `PreToolUse`, with single-use dispatch tokens issued by the runner. One host runner now holds the keys, agent sessions call only stamped, read-only hooks and a non-editable checker, and receipts are signed. All three directors returned "go with changes".

**The six conditions.** The final verdicts attached six bounded build conditions, three from each reviewing director. They change no architecture; they make the proofs honest:

- guard failure is tested explicitly;
- hook execution is isolated the way the design claims;
- CI takes the receipt key from the trusted base;
- new code is tested, but the deployed copy judges it;
- the bootstrap and adoption checks assert what they claim;
- the owner's token stays out of the runner.

Each condition is an acceptance item of the milestone that owns it ([DESIGN.md, section 18](DESIGN.md#18-releases-and-milestones)). The verdicts are permission to build, not evidence that the implementation passes.

## Rules the board kept

- Positions are independent first, then there is one critique round per version.
- Vendor claims are checked against first-party documentation, and a claim that could not be verified is marked as unverified in the design (the telemetry of some harnesses, for example).
- A majority of three decides and the dissent is recorded; a safety veto goes to the owner.
- Nothing leaves the target silently: the board proposes phasing, and the owner draws the cut line.
- Every text an agent writes is tagged with the model that wrote it. Plain words, no em dashes.

## What is left to the owner

[TODO.HUMAN.md](../TODO.HUMAN.md) lists the seventeen decisions the board could not take, each with the board's recommendation, and the one-time steps only the owner can run. The first is the agent identity (Q14), because the first milestone cannot pass without it.
