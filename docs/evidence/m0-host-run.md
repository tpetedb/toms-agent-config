# M0 host run: the confinement tests on macOS

CI runs on `ubuntu-latest`, where `/usr/bin/sandbox-exec` does not exist, so every
test marked `seatbelt` in `tests/test_runner_skeleton.py` skips there. The claims of
build condition C3 about the gate and probe children (docs/DESIGN.md, section 10,
gate children, and section 18, M0) rest on this run on a macOS host instead. It is
acceptance evidence for M0, and it is redone whenever the runner or those tests
change.

## Where it ran

- Host: macOS 26.6.2 (build 25G83), arm64, Apple M4.
- Tree: branch `tac/m0-bootstrap`, the commit that adds this file.
- The test process was not itself inside a Seatbelt profile:
  `tac.runner.default_sandbox()` returned `/usr/bin/sandbox-exec` and
  `inside_sandbox()` returned `False`, so no confinement test could skip.
- Every store, key and socket the tests use is a temporary one; the owner's
  controller store and keychain were not touched.

## The confinement tests

```sh
uv run --frozen pytest -q -rs tests/test_runner_skeleton.py
```

Result: `96 passed`, none skipped.

The same file run inside a Seatbelt profile, where the runner has no sandbox to
give a child, as on Linux CI:

```sh
/usr/bin/sandbox-exec -p '(version 1)(allow default)' \
  uv run --frozen pytest -q tests/test_runner_skeleton.py
```

Result: `54 passed, 42 skipped`. Those 42 are the tests that only a macOS host
proves.

They cover a gate criterion that reads the key into the checkout (and the write is
checked to have happened, so only the read denial stops it), moves the store, writes
the store, reaches the runner socket or a Unix socket the test binds outside its
scratch folder, or a loopback port over 127.0.0.1, ::1 or ::ffff:127.0.0.1 (each
server is checked to have seen no connection); a gate that writes the owner's
config, the judge's venv, the stamped toolchain, the harness config or the shared
git hooks and config; a gate environment without tokens; the runner's own
`git status` running sandboxed; and a client probe whose startup code tries to read
the key, see a token from the runner's environment and write the checkout.

## The real clients under the probe's confinement

The installed clients start and report their version through the confined probe
path, with a throwaway repository and a temporary controller store:

| harness | exit | client_version | matched |
|---|---|---|---|
| claude | 0 | 2.1.280 (Claude Code) | true |
| codex | 0 | codex-cli 0.156.1 | true |

## The rest of the M0 acceptance, same host and tree

| command | result |
|---|---|
| `uv sync --frozen` | exit 0 |
| `uv sync --frozen --no-editable --project .agents` | exit 0 |
| `uv run --frozen ruff check .` | all checks passed |
| `uv run --frozen ruff format --check .` | 58 files already formatted |
| `uv run --frozen basedpyright` | 0 errors, 0 warnings, 0 notes |
| `uv run --frozen pytest -q` | 419 passed, 1 xfailed |
| `uv run --frozen pytest -q tests/test_doctor_exit.py tests/test_doctor_ruleset.py tests/test_receipt_trust.py tests/test_receipt_ci.py tests/test_github_schema.py` | 143 passed |
| `uv run --frozen --no-sync --project .agents tac --version` | tac, version 0.1.0 |
| `bash scripts/private_scan.sh` | exit 0 |
| `just verify` | exit 0 |
| `tac doctor --json` | exit 1, as expected before the owner's steps |

`tac doctor` ran from an agent session with GitHub unreachable and no `gh` on
PATH, so it read no live GitHub setting: the ruleset check is `unknown`, which is
right, since only the owner's token on the host can see `bypass_actors`. It fails
on what waits for the owner or for M1: `.agents/config.toml` and
`.agents/generated.lock` arrive with tac-core, `runner.pub` holds no key until the
owner runs `just runner-init --write-pub`, and Codex does not trust this checkout
yet. Every other check passes: the deployed toolchain, the non-editable `tac`
import, both clients found, Claude trust, and no `gh` identity in the session.
