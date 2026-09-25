# ADR 0001: A task for an agent is a work order, and its acceptance is a command

Status: Accepted, 2026-09-24. Ported from [vibe-map ADR 0016](https://github.com/tpetedb/vibe-map/blob/main/docs/adr/0016-work-orders.md), where the format was proven over some fifty orders, and generalised for any project that adopts TAC.

## Context

Agents build in parallel, several at a time, each in its own worktree. What went wrong in vibe-map before orders existed was never the agents' ability to write code:

- The task lived in a prompt. Which files were the builder's and which checks had to pass was prose, and nothing held a builder to either. "Done" was whatever the report said.
- Collisions were found at merge time. Two builders touching the same file only met when their branches were combined.
- Defects hid between pull requests. Two branches that were each green alone were wrong together.
- The plan was not in the repository. When the orchestrating session ended, its lists ended with it.
- Nobody reviewed. The builder's own tests were the only judge of the builder's own work.

TAC's design (docs/DESIGN.md, sections 5 and 13) asks for the opposite: every stage proven by a command, review by another agent on the other provider, and teams that agree in files rather than in chat.

## Decision

**A task is a file.** `work/orders/<id>/order.toml` names the team, the branch, the builder and the builder's provider, the files the order owns (files or folders, never globs), the orders it needs first, and its acceptance criteria. A criterion is either a `check`, a shell command where exit 0 is pass, or a `judge`, a ruling a named role writes down. Every order has at least one check. The format carries `v = 1`, and an unknown version is refused.

**Teams are configuration, not code.** `.agents/config/teams.toml` lists the teams and the paths each answers for; a path belongs to the first team that covers it. An order owns files of its own team; another team's file goes in `cross` and needs that team's sign-off file. The knobs that were constants in vibe-map (the base branch, the shared and anyone paths, timeouts, how many agents an order remembers, the review provider rule, the repair turn) live in the same file, read through models that refuse an unknown key and a value of the wrong type.

**One tool decides, and everything calls it.** `tac work` checks that orders are readable, that no two active orders own the same file, that a branch changed nothing outside its order, that every check passes, that the review is by someone other than the builder, from the other provider, and rules on every criterion with evidence, and that every cross team signed. A review names the full commit it read and stays current only while neither the order nor the branch's own contribution to its files has changed since, compared as a verbatim patch id. The `just work-*` recipes, the pipeline gates, the hooks and CI are doors to the same checks. `work-repair` renders the failing criteria into the repair handoff for one bounded builder turn and checks again; `work-review` is the review stage's gate on its own.

**Hooks remind; the checks decide.** A pre-tool hook refuses an edit outside the orders on the current branch and names the owning team. It lets go on anything it cannot read, so a broken order stays repairable. A stop hook sends an agent back once while its order fails. A hook is a reminder for a cooperative agent: an edit made through a shell is seen by no hook. What decides whether work lands (`work-check`, `work-review`, `work-accept`, CI) fails closed.

**Status is derived, never stored.** An order is active while its branch is checked out in some worktree, and landed once its accepting review is on the base branch. `work-plan` turns a goal into launch groups within the host's agent budget and each team's `max_workers`. Landed orders are swept at a release.

**The issue is the conversation, the repository is the record.** An order may name an issue. The tool keeps one status comment there and prints the thread for an agent to read, showing only comments from people who can push: on a public repository anyone can comment.

## Consequences

- A review lapses when an owned file changes after it, including a fix made while combining branches. That is friction on purpose: such a fix is unreviewed code.
- Orders cost a few minutes to write. One-line fixes by a person need none; CI judges only pull requests that carry an order.
- The CI step runs after the tests and stays red until the review lands. A builder is done when everything else is green.
- `result.json` is a file the builder can write, so it only speeds up the builder's loop. `work-accept` measures again.
- Check commands run with a shell from a file in the repository: the same trust as the `justfile`. Whoever can merge an order can already run code here.
- On a fork's pull request the CI step reads only and runs no criterion; it guards collaborators against mistakes, not the repository against strangers. The ruleset and the owner's merge do that.
- A project with one provider, or one adopting TAC with orders written before orders named a provider, sets `review_provider = "any"` once.
