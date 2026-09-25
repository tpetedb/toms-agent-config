# TODO.HUMAN.md: what only the owner can do

These are the decisions the board cannot take, and the one-time steps only the owner can run. Answer on the issue or tell the chief; the chief records the answer under the item with `tac human answer`. Each item carries the board's recommendation, what it blocks where that is known, and the milestone that waits on it ([docs/DESIGN.md](docs/DESIGN.md), sections 7 and 18). The owner-delegated answers of 2026-09-25 are recorded from [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).

A ticked box is a hint, never consent. An approval counts only as your own approving review on the pull request, or as a record you sign on the host with `tac approve <id>`; an item that runs out of time is still waiting, since a timeout is never consent.

Credential handover convention: every secret goes into `agents.env`, outside the repository (Q17), under the env var named in the item. Never put a secret in chat or in the repository. Agents never receive what a step does not need; the runtime injects it.

Rendered by `tac human render` from 30 items in `.human/approvals/` and `.human/todo.toml`, 11 open, as of 2026-09-25. Never edit this file by hand: `tac human render --check` fails on any difference.

## Agent identity (first: milestone M0 cannot pass without it)

- [ ] Q14 agent identity: create a machine account (for example `tac-bot`) with write access and no admin, or a GitHub App, for agent pushes. Then turn on "no bypass, including admins" for the default branch, and run `tac github apply`. The owner's own `gh` login stays the owner's and never enters an agent session.
      Recommendation: a machine account with a fine-grained token scoped to this repository. Waits: M0.

## Scope and providers

- [x] Q18 release 1 scope: Claude Code and Codex enforced, the standard profile only, with enterprise, yolo, containers and the four other harnesses in release 1.1?
      Recommendation: yes. The target is unchanged, and 1.1 starts the day vibe-map adopts release 1. Waits: M0.
      Answered 2026-09-25 by tpetedb, owner-via-chief: yes, Claude Code and Codex enforced with the standard profile; enterprise, yolo, containers, Copilot, opencode, pi and Gemini in 1.1, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q9 licence: the repository is public. Publish TAC under MIT?
      Recommendation: MIT. A public MIT repository also keeps unlicensed third-party skill bodies out. Waits: the first release.
      Answered 2026-09-25 by tpetedb, owner-via-chief: MIT, "Copyright (c) 2026 Tom Peters", in `LICENSE` at the root, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q10 providers on day one: Claude and OpenAI subscriptions are assumed. Is there a Google or Copilot seat as well?
      Recommendation: no seat is needed yet; Gemini and Copilot are instructions-only in release 1 either way. Waits: M7.
      Answered 2026-09-25 by tpetedb, owner-via-chief: Claude (Max) and OpenAI; no Google or Copilot seat is assumed; Gemini and Copilot are instructions-only in release 1, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q7 Copilot scope (governs 1.1): CLI only, or also the IDE, the cloud agent and code review?
      Recommendation: CLI only. Waits: M7.
      Answered 2026-09-25 by tpetedb, owner-via-chief: the CLI only, in 1.1, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).

## Governance

- [x] Q5 the board: Opus 5.5 sits as a director only through a separate fresh session, Fable compiles the tally, a majority of three decides, the dissent is recorded, and a safety veto goes to the owner?
      Recommendation: yes. Waits: M3.
      Answered 2026-09-25 by tpetedb, owner-via-chief: yes, Opus as a director only through a separate fresh session, Fable compiles the tally, a majority of three decides, the dissent is recorded, a safety veto goes to the owner, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q6 chief authority: the chief opens issues and pull requests and pushes branches as the machine account, through the runner; merges and releases are the owner's own action (one command or a GitHub review)?
      Recommendation: yes. Waits: M3.
      Answered 2026-09-25 by tpetedb, owner-via-chief: yes, issues, pull requests and branch pushes as the machine account through the runner; merges and releases are the owner's own action, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [ ] Q23 the Workflow ruling's M2 parts: the board's ruling on the Workflow tool puts the runner's single-use token (bound to run, stage and script hash), the signed run receipt and the classifier check in M2, but the runner that issues tokens is M3's `tac run`. As built, M2's guard refuses every Workflow call that carries no token, which is every call until M3, so a script registered early cannot run on the registry check alone (docs/DESIGN.md, section 5). Record a board amendment that moves the token, the receipt and the classifier check to M3 with the runner?
      Recommendation: yes; the token also binds the director's lead seat, which the hook cannot tell from the other seats. Blocks: registering the first Workflow script, which runs only once the ruling says where its token comes from. Waits: M3.

## Profiles, billing, secrets, telemetry

- [x] Q3 yolo (governs 1.1): unattended inside a disposable container with an egress allowlist, or unrestricted host access?
      Recommendation: the container. Waits: M6.
      Answered 2026-09-25 by tpetedb, owner-via-chief: unattended only inside a disposable container with an egress allowlist, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q16 unattended Claude billing: subscription only and never `--bare`, or a capped API key for some headless runs?
      Recommendation: subscription only. Waits: M3.
      Answered 2026-09-25 by tpetedb, owner-via-chief: the subscription only, never `--bare`, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q17 agents.env location: outside the repository, or at the root, gitignored?
      Recommendation: outside the repository, which is safer against `git add -f`. Waits: M0.
      Answered 2026-09-25 by tpetedb, owner-via-chief: outside the repository, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q15 telemetry on the owner's machine: the partial tier that keeps Remote Control and auto mode, or the full kill set?
      Recommendation: the partial tier on the host, the full set in containers and CI. Waits: M0 (`tac init --user`).
      Answered 2026-09-25 by tpetedb, owner-via-chief: the partial tier on the owner's Mac, which keeps Remote Control and auto mode; the full kill set in containers and CI, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).

## Standards and memory

- [x] Q4 commits: Conventional Commits, or "what and why, one line"?
      Recommendation: keep "what and why, one line"; add a type prefix only if release automation needs it. Waits: M1 (the registry default).
      Subject length, part of the same answer: the commit-msg hook (M2) refuses a subject over 72 characters, the `[commits] max_subject` in `.agents/config/standards.toml`, and the floor caps it at 72 (`commit_max_subject`). The hook judges only new commits, so no earlier subject is rewritten; raising the limit means raising both numbers together in one pull request the owner reviews. Waits: `just hooks-install` on this checkout.
      Answered 2026-09-25 by tpetedb, owner-via-chief: "what and why, one line", with a subject of at most 72 characters, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q20 the root `AGENTS.md`: generated by `tac sync` from `.agents/context/brief.md`, as M1 builds it, or hand-written at the root, as DESIGN 2.1 and 2.2 say?
      Recommendation (from the M1 build, not yet put to the board): generated. The brief then sits in the lock-guarded `.agents/` tree, a hand edit to `AGENTS.md` is a red `tac check`, and the Codex chain budget is checked on the render. DESIGN 2.1 and 2.2 change to match once answered. Waits: M1.
      Answered 2026-09-25 by tpetedb, owner-via-chief: generated by `tac sync` from `.agents/context/brief.md`; DESIGN 2.1 and 2.2 now say so, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q8 retention: fold raw sessions and traces after 30 days, and commit the folded digests?
      Recommendation: yes; it ships as the default knob until the owner says otherwise. Waits: M8.
      Answered 2026-09-25 by tpetedb, owner-via-chief: yes, fold raw sessions and traces after 30 days and commit the folded digests, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q13 Soda: an optional extra pinned to the Apache-2.0 v3 line (3.5.6, SodaCL), the v4 line under the Elastic License 2.0, or postponed to 1.1?
      Recommendation: postpone to 1.1, and pin v3 as an opt-in extra if data work needs it sooner. Waits: M8.
      Answered 2026-09-25 by tpetedb, owner-via-chief: postponed to 1.1; the Apache-2.0 v3 line comes in as an opt-in extra if data work needs it sooner, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).

## The public repository's history and identity

- [x] Q21 the private-term list is public in the history: the earlier private scans carried their term list in plain text, and those commits are pushed on `origin/tac/m0-bootstrap` and `tac/m1-core`, so anyone can read the list there today. The range scan reports those commits, so the pull request that brings `tac/m0-bootstrap` into main fails its private scan until this is settled ([docs/DESIGN.md](docs/DESIGN.md), section 8).
      The same list reached `tac/m2-hooks-human` through its merge `39fab53`, which took `tac/m1-core` in. Under the re-cut, M2 lands on `main` as one commit on top of the bootstrap, so the range scan of its pull request reads none of that history.
      Options: (a) rewrite or squash both branches so no commit carries the plain list and force-push them (the owner's own action) before main takes them; (b) accept the exposure, and add a known-history exception to the scan naming exactly those commits and that one file.
      Recommendation: (b). GitHub keeps every pull request's commits readable under its pull request after a branch is rewritten or deleted (only GitHub support can purge them), so a rewrite hides little of what is already public, and it needs a force-push, which agents never do. The exception names only the commits that carry the plain list (those on `tac/m0-bootstrap` that add or change the older `scripts/private_scan.sh`, and `50bc1ca` and `8f5f2cc` on `tac/m1-core`) and that one path. Waits: the pull request from `tac/m0-bootstrap` to main.
      Answered 2026-09-25 by tpetedb, owner-via-chief: re-cut instead of an exception: `main` starts from the single squashed commit on `bootstrap/2026-09-25`, which holds no plain-text term list; pull requests #1, #2 and #4 close as superseded, and their branches are deleted once no open pull request depends on them; GitHub keeps closed pull requests' commits readable, which the owner accepts as low sensitivity; no scan exception is added, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q22 the commit identity: the owner commits as `tpetedb` with a personal address that is itself on the private-term list, so every commit publishes it. The scan exempts that exact pair in author and committer headers (an `identity` line in `scripts/private_terms.txt`). Keep the exemption, or commit under the GitHub noreply address (`<id>+tpetedb@users.noreply.github.com`, with "Keep my email addresses private" on) and drop the exemption?
      Recommendation: the noreply address for this repository, set in its local git config; the exemption line goes once no open branch holds a commit under the old address, since the range scan reads only the commits a pull request brings. Blocks: dropping the `identity` line from `scripts/private_terms.txt`. Waits: M2.
      Answered 2026-09-25 by tpetedb, owner-via-chief: this repository commits under the GitHub noreply address (`177582730+tpetedb@users.noreply.github.com`), set in its local git config; the scan's `identity` line stays until no open branch holds a commit under the old address, then goes, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).

## Skills and third-party material

- [x] Q1 ponytail: vendor DietrichGebert/ponytail (MIT), trimmed and opt-in, or is there a version of the owner's own that the board has not found?
      Recommendation: vendor it, trimmed. Waits: M4.
      Answered 2026-09-25 by tpetedb, owner-via-chief: vendor DietrichGebert/ponytail (MIT), trimmed, opt-in for builders, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q2 user-level third-party skills: skills installed in `~/.agents/skills` load into every Codex session, and into every other harness that reads that folder, next to TAC's own. Move them aside while TAC runs?
      Recommendation: yes. The excluded caveman package stays excluded and its terse seed is dropped; the board does not reopen either. Waits: M4.
      Answered 2026-09-25 by tpetedb, owner-via-chief: yes, moved aside on the owner's machine to `~/.agents/skills-disabled`; the caveman package stays excluded, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).
- [x] Q19 patterns from a private repository: may patterns and code from a private repository (a TODO.HUMAN.md format, a gate runner for `just`, an em-dash check) be redistributed here? That needs permission from the owner of that repository, not only a licence line.
      Recommendation: yes, with an authorship line. Waits: M1 and M2.
      Answered 2026-09-25 by tpetedb, owner-via-chief: allowed only as patterns re-implemented from scratch in this repository (the `TODO.HUMAN.md` format, a gate runner for `just`, an em-dash check), each with an authorship line; no code is copied from the private repository, see [docs/adr/0002-owner-decisions-2026-09-25.md](docs/adr/0002-owner-decisions-2026-09-25.md).

## One-time steps on the owner's machine

- [ ] S1 Trust the checkout in Codex once (the prompt at the next start, or `tac init --user`).
- [ ] S2 Run `codex`, then `/hooks`, and approve the run.py entries once.
- [ ] S3 Open `claude` once in the folder to accept workspace trust.
- [ ] S4 After Q14: run `tac github apply`, then `tac doctor`.
      In your own terminal on the host, logged in to `gh` as yourself (never in an agent session), from the main checkout, with the machine account named in Q14 in place of `tac-bot`:
      `uv run --frozen --no-sync --project .agents tac github apply --dry-run` prints the ruleset without calling GitHub;
      `uv run --frozen --no-sync --project .agents tac github apply --bot tac-bot` checks the account has write and no admin, then creates or updates the ruleset and reads it back by id;
      `uv run --frozen --no-sync --project .agents tac doctor` must then show `github-ruleset` as PASS.
- [ ] S5 Provision the runner key: in your own terminal, never an agent session, run `just runner-init --write-pub` and land `.agents/config/runner.pub` through a pull request. Until then every receipt is refused and `tac doctor` fails its `runner-pub` check. The same recipe builds the runner's own venv in the controller store; start it with `just runner`. The key sits in the controller store, mode 0600, until M3 moves it to the keychain and it is rotated.
- [ ] S6 After bootstrap: run `mise trust` once, then `just doctor` until it is green.
- [ ] S7 Install the git hooks once, in your own terminal in the main checkout: `just hooks-install`. Agents never write the shared `.git/hooks`. From then on every commit there is held to Q4's subject length.
- [ ] S8 Once pi is enabled (1.1): trust the project in pi once.
- [ ] S9 Apply the labels of `.github/labels.yml` once, then again whenever the file changes.
      In your own terminal on the host, logged in to `gh` as yourself (never in an agent session), from the main checkout:
      `just github-labels --dry-run` reads the public label list with no credential and prints what would be created or updated;
      `just github-labels --apply` creates and updates them with your own `gh`, reads them back, and never deletes a label the file does not declare.
      Waits: M2 (the issue forms name these labels; GitHub drops a label that does not exist).
