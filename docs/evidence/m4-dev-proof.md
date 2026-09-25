# M4 dev proof: acceptance 8, the offline part

Acceptance 8 is `just dev-proof && just coverage-proof` ([docs/DESIGN.md](../DESIGN.md), sections 13 and 18). This page records the offline run, from an agent session on the host, on branch `tac/m4-devproof`. Everything that needs the runner's key, the keychain, a live client session or GitHub is a live probe: defined in `src/tac/proof.py`, evaluated on every run, and skipped with the step it waits on. No live probe is reported passed without a signed runner receipt. This page is rewritten from the live run of S10 when it happens.

## Where it ran

- Host: macOS 26.6.2 (build 25G83), arm64, Apple M4.
- Tree: branch `tac/m4-devproof`, the commit before the one that adds this file; both proofs refuse a dirty tree, and each JSON names the revision it tested.
- Session: an agent session. The runner was not serving, no client session was started, the keychain and the controller store were not read, and Codex was not called (paused by the owner).
- The doctor checks the requirements read were run as `tac doctor` runs them: `runner-pub`, `client-claude` and `trust-claude`. Codex's checks were left out while it is paused, so the `codex` binary was never started.

## The commands

```sh
just dev-proof
just coverage-proof
just --justfile dev/ledger/justfile --working-directory dev/ledger verify
just dev-proof --require-live
```

## The results

| command | result |
|---|---|
| `just --justfile dev/ledger/justfile --working-directory dev/ledger verify` | ruff clean, `17 passed` |
| `just --justfile dev/ledger/justfile --working-directory dev/ledger gate-schema-drift` | the good fixture loads 12 rows; the drifted one exits 3 naming `category` and `kind` |
| `just dev-proof` | exit 0: ledger sync, verify and drift gate exit 0; bypass `7 passed`; mutation `26 passed`; `SKIP render: recipe lands with order 7`; live `0 passed, 0 failed, 147 skipped` |
| `just coverage-proof` | exit 0: `129 of 130 conditions proved by a test that ran, 0 failed, 5 live probes waiting`; the mapped run is 307 test cases, none failed or skipped |
| `just dev-proof --require-live` | exit 1, as it must while any live probe is skipped |
| `uv run --frozen tac proof coverage --list` | 130 conditions: 11 hook, 32 pipeline, 14 handoff, 13 contract, 7 memory, 10 human, 4 layer, 18 doctor, 6 build condition, 15 bypass |

The one condition proved by no offline test is `bypass.classifier`: only a live auto-mode session shows what the classifier marks.

What the mutation runs found and the map records: two wired checks refuse nothing a mutation could take away. The Codex `handoff-guard` wiring judges `spawn_agent` allow until the Codex dispatch tokens; what holds on Codex is native subagents rendered off in every profile. `spawn-record` is a record check and renders no hook on either client until the follow-up config wires it. `tests/test_mutation.py` names both with the reason and holds the list to what the guard does. The coverage run also found three doctor checks no test held (`uv-on-path`, `agents-lib`, `agents-venv`); `tests/test_proof.py` now covers them.

## The SKIP lines, verbatim

From `just dev-proof`:

```text
SKIP live: C1.missing-token.claude.root.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.missing-token.claude.root.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.missing-token.claude.root.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.missing-token.claude.subdir.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.missing-token.claude.subdir.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.missing-token.claude.subdir.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.missing-token.claude.worktree.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.missing-token.claude.worktree.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.missing-token.claude.worktree.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.missing-token.codex.root.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.missing-token.codex.root.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.missing-token.codex.root.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.missing-token.codex.subdir.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.missing-token.codex.subdir.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.missing-token.codex.subdir.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.missing-token.codex.worktree.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.missing-token.codex.worktree.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.missing-token.codex.worktree.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.altered-token.claude.root.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.altered-token.claude.root.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.altered-token.claude.root.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.altered-token.claude.subdir.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.altered-token.claude.subdir.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.altered-token.claude.subdir.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.altered-token.claude.worktree.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.altered-token.claude.worktree.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.altered-token.claude.worktree.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.altered-token.codex.root.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.altered-token.codex.root.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.altered-token.codex.root.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.altered-token.codex.subdir.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.altered-token.codex.subdir.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.altered-token.codex.subdir.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.altered-token.codex.worktree.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.altered-token.codex.worktree.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.altered-token.codex.worktree.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.reused-token.claude.root.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.reused-token.claude.root.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.reused-token.claude.root.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.reused-token.claude.subdir.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.reused-token.claude.subdir.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.reused-token.claude.subdir.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.reused-token.claude.worktree.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.reused-token.claude.worktree.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.reused-token.claude.worktree.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.reused-token.codex.root.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.reused-token.codex.root.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.reused-token.codex.root.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.reused-token.codex.subdir.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.reused-token.codex.subdir.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.reused-token.codex.subdir.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.reused-token.codex.worktree.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.reused-token.codex.worktree.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.reused-token.codex.worktree.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.delegation-off.claude.root.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.delegation-off.claude.root.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.delegation-off.claude.root.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.delegation-off.claude.subdir.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.delegation-off.claude.subdir.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.delegation-off.claude.subdir.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.delegation-off.claude.worktree.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.delegation-off.claude.worktree.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.delegation-off.claude.worktree.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.delegation-off.codex.root.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.delegation-off.codex.root.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.delegation-off.codex.root.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.delegation-off.codex.subdir.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.delegation-off.codex.subdir.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.delegation-off.codex.subdir.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.delegation-off.codex.worktree.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.delegation-off.codex.worktree.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.delegation-off.codex.worktree.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.runner-lost.claude.root.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.runner-lost.claude.root.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.runner-lost.claude.root.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.runner-lost.claude.subdir.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.runner-lost.claude.subdir.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.runner-lost.claude.subdir.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.runner-lost.claude.worktree.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.runner-lost.claude.worktree.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.runner-lost.claude.worktree.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.runner-lost.codex.root.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.runner-lost.codex.root.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.runner-lost.codex.root.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.runner-lost.codex.subdir.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.runner-lost.codex.subdir.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.runner-lost.codex.subdir.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.runner-lost.codex.worktree.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.runner-lost.codex.worktree.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.runner-lost.codex.worktree.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.hook-timeout.claude.root.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.hook-timeout.claude.root.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.hook-timeout.claude.root.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.hook-timeout.claude.subdir.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.hook-timeout.claude.subdir.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.hook-timeout.claude.subdir.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.hook-timeout.claude.worktree.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.hook-timeout.claude.worktree.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.hook-timeout.claude.worktree.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C1.hook-timeout.codex.root.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.hook-timeout.codex.root.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.hook-timeout.codex.root.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.hook-timeout.codex.subdir.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.hook-timeout.codex.subdir.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.hook-timeout.codex.subdir.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.hook-timeout.codex.worktree.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.hook-timeout.codex.worktree.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C1.hook-timeout.codex.worktree.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-token.claude.root.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-token.claude.root.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-token.claude.root.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-token.claude.subdir.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-token.claude.subdir.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-token.claude.subdir.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-token.claude.worktree.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-token.claude.worktree.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-token.claude.worktree.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-token.codex.root.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-token.codex.root.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-token.codex.root.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-token.codex.subdir.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-token.codex.subdir.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-token.codex.subdir.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-token.codex.worktree.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-token.codex.worktree.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-token.codex.worktree.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-key.claude.root.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-key.claude.root.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-key.claude.root.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-key.claude.subdir.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-key.claude.subdir.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-key.claude.subdir.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-key.claude.worktree.fresh: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-key.claude.worktree.resumed: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-key.claude.worktree.headless: runner-key (the runner's key, provisioned): S5 pending, doctor runner-pub: fail; keychain (the runner's secrets in the login keychain): Q24 pending; trust-claude (Claude trusts this checkout): S3 pending
SKIP live: C6.keychain-key.codex.root.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-key.codex.root.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-key.codex.root.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-key.codex.subdir.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-key.codex.subdir.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-key.codex.subdir.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-key.codex.worktree.fresh: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-key.codex.worktree.resumed: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: C6.keychain-key.codex.worktree.headless: codex-resumed (Codex is paused by the owner): answer S10 once Codex is resumed
SKIP live: bypass.ruleset-owner-review: github-identity (the machine account and the ruleset): Q14 pending
SKIP live: bypass.push-required-checks: github-identity (the machine account and the ruleset): Q14 pending
SKIP live: bypass.classifier.claude: live-claude-session (a live Claude session on the host): run `just dev-proof --live --harness claude` on the host
```

From `just coverage-proof`, one line per live probe the map names:

```text
SKIP live: bypass.ruleset-owner-review: live; requires github-identity; runs through `just dev-proof --require-live` on the host
SKIP live: C1: live; requires codex-resumed, runner-key, client-claude, trust-claude, client-codex, trust-codex; runs through `just dev-proof --require-live` on the host
SKIP live: C6: live; requires codex-resumed, runner-key, keychain, client-claude, trust-claude, client-codex, trust-codex; runs through `just dev-proof --require-live` on the host
SKIP live: bypass.push-required-checks: live; requires github-identity; runs through `just dev-proof --require-live` on the host
SKIP live: bypass.classifier: live; requires live-claude-session; runs through `just dev-proof --require-live` on the host
```

## What waits

- The live probes, C1 and C6 on Claude and Codex across the root, a subdirectory and a linked worktree and across fresh, resumed and headless sessions, run through `just dev-proof --require-live` on the host once S5 (the runner key), Q24 (the keychain) and S3 (Claude's workspace trust) are done and the owner resumes Codex and answers S10 in `TODO.HUMAN.md`. Codex's own trust steps, S1 and S2, are asked of it as well.
- The entries for the new probe kinds in `.agents/config/probes.toml` and `.agents/config/receipts.toml` are follow-up config: CI judges configuration with the base revision's checker, so the kinds entered the schema first. Until they land, the runner refuses each live kind by name.
- The GitHub-bound bypass attempts (the ruleset refusing a pull request that edits `src/tac` without the owner's review, a push without the required checks) wait for Q14; the auto-mode classifier check waits for a live Claude session.
- The pipeline run of the ledger from an issue to the recap (the chief specifies, a builder builds, the gates verify, the other provider reviews, a manager signs off, the recap writes `TODO.HUMAN.md`) waits for the runner key, both clients and Q14.
- The ledger's diagram renders through `just diagrams-render` once order 7 lands; until then `just dev-proof` prints `SKIP render: recipe lands with order 7`.
