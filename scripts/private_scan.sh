#!/usr/bin/env bash
# private_scan.sh: refuse private terms in this public repository.
#
# The design under docs/ was distilled from private notes. This scan greps every
# tracked file, plus new files git does not ignore, for the terms below. In this
# file it skips only the term-list lines the running copy carries itself, so a
# term added anywhere else here is still caught; in CI the running copy is the
# base revision's, so a candidate cannot widen what is skipped.
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

# Citation keys of the private design notes, matched with case: fact keys (an F
# and three digits), one earlier decision key, review keys (two capitals after an
# opening bracket or a separator) and requirement or lesson keys (an R or an L
# and one or two digits in brackets).
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
exits 0 with no output when clean, exits 2 when a grep itself fails.
EOF
}

# The empty tree, as the source of every gitattribute: git grep -I honours the
# diff and binary attributes, and the candidate's .gitattributes could mark a
# text file binary to hide it. With none, only a NUL byte in the content makes a
# file binary.
empty_tree() {
  git hash-object -t tree /dev/null
}

# Maps a grep status: 0 on hits (already printed), 1 when clean, exits 2 on error.
grep_status() {
  case "$1" in
    0) return 0 ;;
    1) return 1 ;;
    *)
      printf 'private_scan: grep failed with status %s\n' "$1" >&2
      exit 2
      ;;
  esac
}

# Greps every file but this one, with no gitattributes read from any tree.
grep_repo() {
  local status=0
  git --attr-source="$ATTR_SOURCE" grep -n -I --untracked "$@" \
    -- . ":(exclude)$SELF" || status=$?
  grep_status "$status"
}

# The term-list lines of the running copy, spelled as they stand in the file.
own_term_lines() {
  local term
  for term in "${FIXED_TERMS[@]}" "${KEY_PATTERNS[@]}"; do
    printf '  "%s"\n' "$term"
  done
}

# This file with each of the running copy's own term-list lines blanked, so
# line numbers still match the file.
blank_own_terms() {
  awk 'NR == FNR { own[$0] = 1; next } { print (($0 in own) ? "" : $0) }' \
    <(own_term_lines) "$SELF"
}

# Greps this file, all but the lines blanked above; 1 when it is absent.
grep_self() {
  [ -f "$SELF" ] || return 1
  local status=0
  blank_own_terms | grep -n "$@" | sed "s|^|$SELF:|" || status=$?
  grep_status "$status"
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
  ATTR_SOURCE="$(empty_tree)"

  local -a fixed_args=() key_args=()
  local term
  for term in "${FIXED_TERMS[@]}"; do fixed_args+=(-e "$term"); done
  for term in "${KEY_PATTERNS[@]}"; do key_args+=(-e "$term"); done

  local found=0
  if grep_repo -i -F "${fixed_args[@]}"; then found=1; fi
  if grep_repo -E "${key_args[@]}"; then found=1; fi
  if grep_self -i -F "${fixed_args[@]}"; then found=1; fi
  if grep_self -E "${key_args[@]}"; then found=1; fi

  if [ "$found" -ne 0 ]; then
    printf 'private_scan: private terms found; remove them before pushing\n' >&2
    return 1
  fi
  return 0
}

main "$@"
