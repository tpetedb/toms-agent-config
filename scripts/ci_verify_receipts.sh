#!/usr/bin/env bash
# ci_verify_receipts.sh: judge the receipts committed in this checkout with the
# base revision's own verifier and the base revision's runner.pub.
#
# Build condition C3 (docs/DESIGN.md, section 10): the candidate supplies the
# receipts and nothing else. The verifier is the tac stamped in the base's
# .agents/, extracted with git archive and built outside the checkout, so a pull
# request that edits .agents/lib/tac cannot change how its own receipts are read.
#
# The base's verifier runs even when no receipt is committed: the base's
# .agents/config/receipts.toml may require some of every changed order.
#
# Usage: scripts/ci_verify_receipts.sh <base-revision> <owner/name>
# Exit:  0 when all verify and none required is missing; 1 when any is refused
#        or missing, or receipts arrive at a base with no verifier; 2 on a usage
#        error.
set -euo pipefail

RECEIPTS_GLOB="work/orders/*/receipts/*.json"
VERIFIER=".agents/lib/tac/src/tac/receipts.py"

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
esac
if [ "$#" -ne 2 ]; then
  usage >&2
  exit 2
fi
base_ref="$1"
repository="$2"

top="$(git rev-parse --show-toplevel)"
cd "$top"
base="$(git rev-parse --verify --quiet "${base_ref}^{commit}")" || {
  printf 'ci_verify_receipts: %s is not a commit here\n' "$base_ref" >&2
  exit 1
}

# A glob that matches nothing stays literal, so test the first expansion.
# shellcheck disable=SC2086  # reason: the glob has to expand
set -- $RECEIPTS_GLOB
if ! git cat-file -e "${base}:${VERIFIER}" 2>/dev/null; then
  # Before the first stamped verifier lands nothing can be required either.
  if [ ! -e "$1" ]; then
    echo "no committed receipts"
    exit 0
  fi
  printf 'ci_verify_receipts: the base revision %s has no receipt verifier, so the %s committed receipt(s) cannot be judged\n' \
    "${base:0:12}" "$#" >&2
  exit 1
fi

# TMPDIR by name: macOS mktemp alone picks the per-user folder, which a
# runner gate cannot write.
work="$(mktemp -d "${TMPDIR:-/tmp}/tac-ci.XXXXXX")"
trap 'rm -rf "$work"' EXIT
git archive --format=tar "$base" -- .agents | tar -xf - -C "$work"

# --directory runs uv from the extracted base, so no uv.toml or pyproject.toml in
# the candidate can reach its settings; the variables below could point the
# build at another environment or another interpreter.
unset UV_PROJECT_ENVIRONMENT VIRTUAL_ENV UV_PYTHON PYTHONPATH
uv sync --quiet --frozen --no-editable --directory "$work/.agents"
uv run --frozen --no-sync --directory "$work/.agents" \
  tac receipt verify-tree --base "$base" --repository "$repository" --repo "$top"
