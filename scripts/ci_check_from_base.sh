#!/usr/bin/env bash
# ci_check_from_base.sh: judge the candidate tree's configuration and generated
# files with the base revision's own checker, not the candidate's.
#
# Layer 3b (docs/DESIGN.md, section 9): the base's checker, stamped in the
# base's .agents/ and built outside the checkout as ci_work_from_base.sh builds
# it, runs `tac config check --base` (the floor may only tighten) and `tac
# check` (every generated file matches a fresh render and the lock) against the
# head tree. The verify job runs the candidate's own checker as well; this is
# the one a pull request cannot edit.
#
# A pull request that changes the checker itself (.agents/lib/) can be judged
# only by its new code, so there a failure of the base's checker is a warning
# and CODEOWNERS on /.agents/ is the gate; so it is on a base with no stamped
# checker yet.
#
# Usage: scripts/ci_check_from_base.sh <base-ref>
# Exit:  0 when both hold, or when they may only warn; 1 when either fails;
#        2 on a usage error.
set -euo pipefail

CHECKER=".agents/lib/tac/src/tac/sync.py"

usage() {
  sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
esac
if [ "$#" -ne 1 ]; then
  usage >&2
  exit 2
fi
base_ref="$1"

top="$(git rev-parse --show-toplevel)"
cd "$top"
base="$(git rev-parse --verify --quiet "${base_ref}^{commit}")" || {
  printf 'ci_check_from_base: %s is not a commit here\n' "$base_ref" >&2
  exit 2
}

if ! git cat-file -e "${base}:${CHECKER}" 2>/dev/null; then
  printf '::warning::the base revision %s has no stamped checker; the verify job judged with the candidate'"'"'s, and CODEOWNERS on /.agents/ is the gate\n' \
    "${base:0:12}"
  exit 0
fi

# The merge base, so a checker change on the base branch since the fork is not
# read as this pull request's.
fork="$(git merge-base "$base" HEAD)"
advisory=0
if ! git diff --quiet "$fork" HEAD -- .agents/lib; then
  advisory=1
fi

# The build must not be steered by the caller's environment; see
# ci_verify_receipts.sh for why each of these matters.
unset UV_PROJECT_ENVIRONMENT VIRTUAL_ENV UV_PYTHON PYTHONPATH

# TMPDIR by name: macOS mktemp alone picks the per-user folder, which a
# runner gate cannot write.
work="$(mktemp -d "${TMPDIR:-/tmp}/tac-ci.XXXXXX")"
trap 'rm -rf "$work"' EXIT
git archive --format=tar "$base" -- .agents | tar -xf - -C "$work"
uv sync --quiet --frozen --no-editable --directory "$work/.agents"
tac="$work/.agents/.venv/bin/tac"

status=0
"$tac" config check --root "$top" --base "$base" || status=1
"$tac" check --root "$top" || status=1

if [ "$status" -ne 0 ] && [ "$advisory" -eq 1 ]; then
  printf '::warning::this pull request changes the checker in .agents/lib/, so the base revision'"'"'s verdict above is advice; CODEOWNERS on /.agents/ is the gate\n'
  exit 0
fi
exit "$status"
