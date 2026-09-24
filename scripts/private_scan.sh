#!/usr/bin/env bash
# private_scan.sh: refuse private terms in this public repository.
#
# The design under docs/ was distilled from private notes. This scan greps every
# tracked file, plus new files git does not ignore, for the terms below. It skips
# itself, since it has to spell the terms out.
#
# Usage: scripts/private_scan.sh [--help]
# Exit:  0 and no output when clean; 1 with each hit as path:line:text; 2 on error.
set -euo pipefail

SELF="scripts/private_scan.sh"

# Fixed strings, matched without regard to case: local paths, private repository,
# business and machine names, personal addresses, subscription details.
# shellcheck disable=SC2088  # reason: the literal tilde is the text being searched for
FIXED_TERMS=(
  "/Users/"
  "~/platform"
  "~/repos"
  "maxgroup"
  "MaxGroup"
  "t.petersprivate"
  "@gmail.com"
  "e-Boekhouden"
  "Pay.nl"
  "C-Plan"
  "doc-doc"
  "docdoc"
  "R2-D2"
  "toms-toolbox"
  "Mac-mini"
  "host.md"
  ".git/board"
  "max-20x"
  "pro-5x"
  "Max 20x"
  "codex-watch"
  "usage-watch"
)

# Citation keys of the private design notes, matched with case: fact keys (F079),
# the earlier decision key (D193), review keys ((OP 3), ; AS weak 1) and
# requirement or lesson keys ((R10), (L7)).
KEY_PATTERNS=(
  "(^|[^[:alnum:]_])F[0-9]{3}([^[:alnum:]_]|$)"
  "(^|[^[:alnum:]_])D193([^[:alnum:]_]|$)"
  "[(;,] ?(AP|OP|AS|DD|OR|EV)( |[;,)]|$)"
  "[(;,] ?(R|L)[0-9]{1,2}( to |[;,)])"
)

usage() {
  cat <<'EOF'
Usage: scripts/private_scan.sh [--help]

Greps tracked files, and new files git does not ignore, for private terms and
internal citation keys. Prints each hit as path:line:text and exits 1 on a hit,
exits 0 with no output when clean, exits 2 when git grep itself fails.
EOF
}

# Runs git grep; returns 0 on hits (already printed), 1 when clean, exits 2 on error.
grep_repo() {
  local status=0
  git grep -n -I --untracked "$@" -- . ":(exclude)$SELF" || status=$?
  case "$status" in
    0) return 0 ;;
    1) return 1 ;;
    *)
      printf 'private_scan: git grep failed with status %s\n' "$status" >&2
      exit 2
      ;;
  esac
}

main() {
  case "${1:-}" in
    -h | --help)
      usage
      return 0
      ;;
    "") ;;
    *)
      printf 'private_scan: unknown argument: %s\n' "$1" >&2
      usage >&2
      return 2
      ;;
  esac

  cd "$(git rev-parse --show-toplevel)"

  local -a fixed_args=() key_args=()
  local term
  for term in "${FIXED_TERMS[@]}"; do fixed_args+=(-e "$term"); done
  for term in "${KEY_PATTERNS[@]}"; do key_args+=(-e "$term"); done

  local found=0
  if grep_repo -i -F "${fixed_args[@]}"; then found=1; fi
  if grep_repo -E "${key_args[@]}"; then found=1; fi

  if [ "$found" -ne 0 ]; then
    printf 'private_scan: private terms found; remove them before pushing\n' >&2
    return 1
  fi
  return 0
}

main "$@"
