# dev/: the proving ground

`dev/` holds what acceptance 8 proves the harness against ([docs/DESIGN.md](../docs/DESIGN.md), sections 13 and 18). Nothing in `src/`, `hooks/`, `.agents/` or `tests/` may import, read or depend on anything here: the harness must work in a project that has no `dev/` at all, and a change here can never change what the checker decides.

| Path | What it is |
|---|---|
| [`ledger/`](ledger/README.md) | A small real tool, built to every standard the harness enforces: its own uv project at 0.1.0 with a lock, tests, a schema drift gate, a Keep a Changelog file, an ADR and a diagram. |
| [`coverage/map.toml`](coverage/map.toml) | The coverage manifest: one entry per condition `tac proof coverage --list` derives from the configuration, naming the tests that prove it, or the live probe that will. |
| `out/` | What the proofs write: `dev-proof.json`, `coverage.json` and the JUnit reports. Never committed. |

## How the proofs read it

- `just dev-proof` (`tac proof dev`) syncs `ledger/` with `uv sync --frozen --project dev/ledger`, runs its `verify` and `gate-schema-drift` recipes through `just --justfile dev/ledger/justfile --working-directory dev/ledger`, runs `tests/test_bypass.py` and `tests/test_mutation.py`, then evaluates every live probe and prints `SKIP live: <probe>: <reason>` for each one whose requirements are not met. It writes `out/dev-proof.json`, bound to the tested revision and a clean tree.
- `just coverage-proof` (`tac proof coverage`) derives every guard, gate, hook, stage, handoff, memory, human and layer condition from the configuration, reads `coverage/map.toml`, refuses a condition without an entry or a test id that does not collect, runs the mapped tests and writes `out/coverage.json`.

Both refuse a dirty tree unless given `--allow-dirty`, and both exit non-zero on missing evidence. A live probe is never reported passed without a signed runner receipt.
