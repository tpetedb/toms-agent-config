# TODO.HUMAN.md: what only the owner can do

These are the decisions the board cannot take, and the one-time steps only the owner can run. Tick a box or answer on the issue; the chief records the answer under the item with `tac human answer`. Each item carries the board's recommendation and the milestone that waits on it ([docs/DESIGN.md](docs/DESIGN.md), section 18). Until `tac human` exists (milestone M2), this file is kept by hand through pull requests. Compiled 2026-09-24 from docs/DESIGN.md.

Credential handover convention: every secret goes into `agents.env`, outside the repository (Q17), under the env var named in the item. Never put a secret in chat or in the repository. Agents never receive what a step does not need; the runtime injects it.

## Agent identity (first: milestone M0 cannot pass without it)

- [ ] Q14 agent identity: create a machine account (for example `tac-bot`) with write access and no admin, or a GitHub App, for agent pushes. Then turn on "no bypass, including admins" for the default branch, and run `tac github apply`. The owner's own `gh` login stays the owner's and never enters an agent session.
      Recommendation: a machine account with a fine-grained token scoped to this repository. Waits: M0.

## Scope and providers

- [ ] Q18 release 1 scope: Claude Code and Codex enforced, the standard profile only, with enterprise, yolo, containers and the four other harnesses in release 1.1?
      Recommendation: yes. The target is unchanged, and 1.1 starts the day vibe-map adopts release 1. Waits: M0.
- [ ] Q9 licence: the repository is public. Publish TAC under MIT?
      Recommendation: MIT. A public MIT repository also keeps unlicensed third-party skill bodies out. Waits: the first release.
- [ ] Q10 providers on day one: Claude and OpenAI subscriptions are assumed. Is there a Google or Copilot seat as well?
      Recommendation: no seat is needed yet; Gemini and Copilot are instructions-only in release 1 either way. Waits: M7.
- [ ] Q7 Copilot scope (governs 1.1): CLI only, or also the IDE, the cloud agent and code review?
      Recommendation: CLI only. Waits: M7.

## Governance

- [ ] Q5 the board: Opus 5.5 sits as a director only through a separate fresh session, Fable compiles the tally, a majority of three decides, the dissent is recorded, and a safety veto goes to the owner?
      Recommendation: yes. Waits: M3.
- [ ] Q6 chief authority: the chief opens issues and pull requests and pushes branches as the machine account, through the runner; merges and releases are the owner's own action (one command or a GitHub review)?
      Recommendation: yes. Waits: M3.

## Profiles, billing, secrets, telemetry

- [ ] Q3 yolo (governs 1.1): unattended inside a disposable container with an egress allowlist, or unrestricted host access?
      Recommendation: the container. Waits: M6.
- [ ] Q16 unattended Claude billing: subscription only and never `--bare`, or a capped API key for some headless runs?
      Recommendation: subscription only. Waits: M3.
- [ ] Q17 agents.env location: outside the repository, or at the root, gitignored?
      Recommendation: outside the repository, which is safer against `git add -f`. Waits: M0.
- [ ] Q15 telemetry on the owner's machine: the partial tier that keeps Remote Control and auto mode, or the full kill set?
      Recommendation: the partial tier on the host, the full set in containers and CI. Waits: M0 (`tac init --user`).

## Standards and memory

- [ ] Q4 commits: Conventional Commits, or "what and why, one line"?
      Recommendation: keep "what and why, one line"; add a type prefix only if release automation needs it. Waits: M1 (the registry default).
- [ ] Q8 retention: fold raw sessions and traces after 30 days, and commit the folded digests?
      Recommendation: yes; it ships as the default knob until the owner says otherwise. Waits: M8.
- [ ] Q13 Soda: an optional extra pinned to the Apache-2.0 v3 line (3.5.6, SodaCL), the v4 line under the Elastic License 2.0, or postponed to 1.1?
      Recommendation: postpone to 1.1, and pin v3 as an opt-in extra if data work needs it sooner. Waits: M8.

## Skills and third-party material

- [ ] Q1 ponytail: vendor DietrichGebert/ponytail (MIT), trimmed and opt-in, or is there a version of the owner's own that the board has not found?
      Recommendation: vendor it, trimmed. Waits: M4.
- [ ] Q2 user-level third-party skills: skills installed in `~/.agents/skills` load into every Codex session, and into every other harness that reads that folder, next to TAC's own. Move them aside while TAC runs?
      Recommendation: yes. The excluded caveman package stays excluded and its terse seed is dropped; the board does not reopen either. Waits: M4.
- [ ] Q19 patterns from a private repository: may patterns and code from a private repository (a TODO.HUMAN.md format, a gate runner for `just`, an em-dash check) be redistributed here? That needs permission from the owner of that repository, not only a licence line.
      Recommendation: yes, with an authorship line. Waits: M1 and M2.

## One-time steps on the owner's machine

- [ ] Trust the checkout in Codex once (the prompt at the next start, or `tac init --user`).
- [ ] Run `codex`, then `/hooks`, and approve the run.py entries once.
- [ ] Open `claude` once in the folder to accept workspace trust.
- [ ] After Q14: run `tac github apply`, then `tac doctor`.
- [ ] After bootstrap: run `mise trust` once, then `just doctor` until it is green.
- [ ] Once pi is enabled (1.1): trust the project in pi once.
