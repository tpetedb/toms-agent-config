#!/usr/bin/env bash
# ci_config_from_base.sh: judge a pull request's configuration against the base
# revision's standards floor with the base revision's own checker.
#
# The candidate may only tighten the floor (docs/DESIGN.md, section 9). The
# checker is the tac stamped in the base's .agents/, extracted with git archive
# and built outside the checkout, so a pull request that loosens the floor and
# also edits .agents/lib/tac cannot pass by changing the code that judges it.
# A base with no floor yet has nothing to tighten; if it has no config checker
# either, the candidate's checks its config and says so. A base with a floor
# but no checker is refused, since nothing trusted could judge it. A config file
# or key the base's checker does not know yet is refused too: the checker that
# reads it lands in one pull request, the configuration in the next.
#
# Usage: scripts/ci_config_from_base.sh <base-ref>
# Exit:  the exit status of `tac config check --base`; 2 on a usage error.
set -euo pipefail

CHECKER=".agents/lib/tac/src/tac/config.py"
FLOOR=".agents/standards.floor.toml"

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
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
  printf 'ci_config_from_base: %s is not a commit here\n' "$base_ref" >&2
  exit 2
}

# The build must not be steered by the caller's environment; see
# ci_verify_receipts.sh for why each of these matters.
unset UV_PROJECT_ENVIRONMENT VIRTUAL_ENV UV_PYTHON PYTHONPATH

if git cat-file -e "${base}:${CHECKER}" 2>/dev/null; then
  # TMPDIR by name: macOS mktemp alone picks the per-user folder, which a
  # runner gate cannot write.
  work="$(mktemp -d "${TMPDIR:-/tmp}/tac-ci.XXXXXX")"
  trap 'rm -rf "$work"' EXIT
  git archive --format=tar "$base" -- .agents | tar -xf - -C "$work"
  uv sync --quiet --frozen --no-editable --directory "$work/.agents"
  tac="$work/.agents/.venv/bin/tac"
elif git cat-file -e "${base}:${FLOOR}" 2>/dev/null; then
  printf 'ci_config_from_base: %s carries %s but no checker to judge it\n' \
    "${base:0:12}" "$FLOOR" >&2
  exit 1
else
  printf '::warning::the base revision %s has no floor and no config checker; the candidate checks its own configuration\n' \
    "${base:0:12}"
  uv sync --quiet --frozen --no-editable --project .agents
  tac="$top/.agents/.venv/bin/tac"
fi

# The base's floor applies to the checkout's configuration: the revision is
# passed by commit, so a branch name never reaches git as an option.
"$tac" config check --base "$base" --root "$top"
