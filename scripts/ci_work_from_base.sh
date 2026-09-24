#!/usr/bin/env bash
# ci_work_from_base.sh: judge the work orders a pull request carries with the
# base revision's own checker, not the candidate's.
#
# Layer 3b (docs/DESIGN.md, section 9): CI runs the base's checker against the
# head tree. The checker is the tac stamped in the base's .agents/, extracted
# with git archive and built outside the checkout, so a pull request that edits
# .agents/lib/tac cannot change how its own orders are judged. A base with no
# stamped checker yet (the bootstrap pull request) falls back to the candidate's
# and says so; CODEOWNERS on /.agents/ is then the gate.
#
# Usage: scripts/ci_work_from_base.sh <base-ref> <head-branch>
# Exit:  the exit status of `tac work ci`; 2 on a usage error.
set -euo pipefail

CHECKER=".agents/lib/tac/src/tac/work.py"

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
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
head_ref="$2"

top="$(git rev-parse --show-toplevel)"
cd "$top"
base="$(git rev-parse --verify --quiet "${base_ref}^{commit}")" || {
  printf 'ci_work_from_base: %s is not a commit here\n' "$base_ref" >&2
  exit 2
}

# The build must not be steered by the caller's environment; see
# ci_verify_receipts.sh for why each of these matters.
unset UV_PROJECT_ENVIRONMENT VIRTUAL_ENV UV_PYTHON PYTHONPATH

if git cat-file -e "${base}:${CHECKER}" 2>/dev/null; then
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  git archive --format=tar "$base" -- .agents | tar -xf - -C "$work"
  uv sync --quiet --frozen --no-editable --directory "$work/.agents"
  tac="$work/.agents/.venv/bin/tac"
else
  printf '::warning::the base revision %s has no stamped checker; the candidate judges its own orders, and CODEOWNERS on /.agents/ is the gate\n' \
    "${base:0:12}"
  uv sync --quiet --frozen --no-editable --project .agents
  tac="$top/.agents/.venv/bin/tac"
fi

# The venv's own entry point, run from the checkout: the orders are read here,
# and nothing in the checkout is on the checker's import path.
"$tac" work ci --base "$base_ref" --head "$head_ref"
