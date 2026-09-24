#!/usr/bin/env bash
# bootstrap.sh: make this checkout ready to run tac. Idempotent; each step says
# what it does and skips what is already done. bash 3.2 compatible (macOS).
#
# Usage: bash bootstrap.sh [--help]
set -euo pipefail

step() { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

usage() {
  cat <<'USAGE'
Usage: bash bootstrap.sh [--help]

Installs uv if it is absent, builds the candidate environment (.venv, src/
editable) and the deployed toolchain (.agents/.venv, tac non-editable from the
stamped copy in .agents/lib/tac), then runs tac doctor. Safe to rerun.
USAGE
}

case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
  "") ;;
  *)
    printf 'bootstrap: unknown argument: %s\n' "$1" >&2
    usage >&2
    exit 2
    ;;
esac

cd "$(dirname "$0")"

step "uv"
if command -v uv >/dev/null 2>&1; then
  note "present: $(uv --version)"
else
  note "absent; installing with the official installer"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
  export PATH
  note "installed: $(uv --version)"
fi

step "mise"
if [ -f mise.toml ]; then
  if command -v mise >/dev/null 2>&1; then
    note "present; run 'mise trust' once, then 'mise install'"
  else
    note "absent; install it from https://mise.jdx.dev before pinning the CLIs"
  fi
else
  note "skipped: no mise.toml in this checkout yet"
fi

step "candidate environment (.venv, src/ editable, for tests)"
uv sync --frozen

step "deployed toolchain (.agents/.venv, tac non-editable from .agents/lib/tac)"
uv sync --frozen --no-editable --project .agents

step "tac init"
note "skipped: tac init arrives with tac-core"

step "tac doctor"
if uv run --frozen --no-sync --project .agents tac doctor; then
  note "all checks pass"
else
  note "some checks are not met yet; the list above names them"
fi
