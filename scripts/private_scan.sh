#!/usr/bin/env bash
# private_scan.sh: refuse private terms in this public repository.
#
# The design under docs/ was distilled from private notes. This scan greps the
# committed content of every tracked file, read raw from the index, plus new and
# changed files in the working tree, for the terms below. In this file it skips
# only the term-list lines the running copy carries itself, so a term added
# anywhere else here is still caught; in CI the running copy is the base
# revision's, so a candidate cannot widen what is skipped.
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

Greps the committed content of tracked files, and new or changed files git does
not ignore, for private terms and internal citation keys. Prints each hit as path:line:text and exits 1 on a hit,
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

# Records a grep status: a hit (0) marks the scan found, clean (1) does nothing,
# anything else is an error and exits 2.
note_status() {
  case "$1" in
    0) FOUND=1 ;;
    1) ;;
    *)
      printf 'private_scan: grep failed with status %s\n' "$1" >&2
      exit 2
      ;;
  esac
}

# Appends lines from stdin to the hits file as path:line:text.
prefix_hits() {
  local line
  while IFS= read -r line; do
    printf '%s:%s\n' "$1" "$line" >>"$HITS"
  done
}

# Prints a file as UTF-8: UTF-16 that opens with a byte order mark is converted,
# since git grep -I skips it as binary for its NUL bytes while GitHub shows it as
# text. A file iconv cannot convert fails the scan rather than passing unread.
decoded() {
  if ! has_bom "$1"; then
    cat "$1"
  elif ! iconv -f UTF-16 -t UTF-8 "$1"; then
    printf 'private_scan: cannot decode %s as UTF-16\n' "$2" >&2
    exit 2
  fi
}

# Greps one file's text with the system grep and records its hits under a name.
grep_text() {
  local file="$1" name="$2" status=0
  shift 2
  decoded "$file" "$name" >"$WORK/text"
  grep -n "$@" "$WORK/text" >"$WORK/lines" || status=$?
  prefix_hits "$name" <"$WORK/lines"
  note_status "$status"
}

# Greps the committed content: every blob in the index but this file, read raw.
# The working tree is not what gets published: a checkout applies the
# candidate's working-tree-encoding, so a UTF-8 blob can land as UTF-16 there.
grep_committed() {
  local status=0
  git --attr-source="$ATTR_SOURCE" grep -n -I --cached "$@" \
    -- . ":(exclude)$SELF" >>"$HITS" || status=$?
  note_status "$status"
}

# Greps what is only in the working tree: new files git does not ignore, and
# tracked files changed since they were staged. None of them has been through a
# checkout, so no attribute has converted them.
grep_worktree() {
  [ "${#WORKTREE[@]}" -gt 0 ] || return 0
  local status=0
  git --attr-source="$ATTR_SOURCE" grep -n -I --untracked "$@" \
    -- "${WORKTREE[@]/#/:(literal)}" >>"$HITS" || status=$?
  note_status "$status"
}

# Greps the UTF-16 files git grep skipped as binary: committed blobs and
# working-tree files that open with a byte order mark.
grep_utf16() {
  local path
  for path in ${BINARY_BLOBS[@]+"${BINARY_BLOBS[@]}"}; do
    git cat-file blob ":$path" >"$WORK/blob"
    if has_bom "$WORK/blob"; then grep_text "$WORK/blob" "$path" "$@"; fi
  done
  for path in ${WORKTREE[@]+"${WORKTREE[@]}"}; do
    if has_bom "$path"; then grep_text "$path" "$path" "$@"; fi
  done
}

# True when a file opens with a UTF-16 byte order mark.
has_bom() {
  case "$(head -c 2 "$1" | od -An -tx1 | tr -d ' \n')" in
    fffe | feff) return 0 ;;
    *) return 1 ;;
  esac
}

# The term-list lines of the running copy, spelled as they stand in the file.
own_term_lines() {
  local term
  for term in "${FIXED_TERMS[@]}" "${KEY_PATTERNS[@]}"; do
    printf '  "%s"\n' "$term"
  done
}

# A copy of this file with each of the running copy's own term-list lines
# blanked, so line numbers still match the file.
blank_own_terms() {
  decoded "$1" "$SELF" >"$WORK/self"
  awk 'NR == FNR { own[$0] = 1; next } { print (($0 in own) ? "" : $0) }' \
    <(own_term_lines) "$WORK/self" >"$WORK/self-blanked"
}

# Greps this file, all but the lines blanked above: its committed blob and its
# working copy, each when present.
grep_self() {
  if [ -n "$(git ls-files -- "$SELF")" ]; then
    git cat-file blob ":$SELF" >"$WORK/self-blob"
    blank_own_terms "$WORK/self-blob"
    grep_text "$WORK/self-blanked" "$SELF" "$@"
  fi
  if [ -f "$SELF" ]; then
    blank_own_terms "$SELF"
    grep_text "$WORK/self-blanked" "$SELF" "$@"
  fi
}

# Fills WORKTREE with the new and changed regular files but this one, and
# BINARY_BLOBS with the committed paths whose content git calls binary.
list_files() {
  local path info
  WORKTREE=()
  BINARY_BLOBS=()
  git ls-files -z --others --exclude-standard --modified >"$WORK/worktree"
  while IFS= read -r -d '' path; do
    if [ "$path" != "$SELF" ] && [ -f "$path" ] && [ ! -L "$path" ]; then
      WORKTREE+=("$path")
    fi
  done <"$WORK/worktree"
  git --attr-source="$ATTR_SOURCE" ls-files -z --eol >"$WORK/eol"
  while IFS= read -r -d '' info; do
    path="${info#*$'\t'}"
    case "${info%%$'\t'*}" in
      i/-text*) [ "$path" = "$SELF" ] || BINARY_BLOBS+=("$path") ;;
    esac
  done <"$WORK/eol"
}

# Runs every pass with one set of grep arguments.
scan_with() {
  grep_committed "$@"
  grep_worktree "$@"
  grep_utf16 "$@"
  grep_self "$@"
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
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  HITS="$WORK/hits"
  : >"$HITS"
  FOUND=0
  list_files

  local -a fixed_args=() key_args=()
  local term
  for term in "${FIXED_TERMS[@]}"; do fixed_args+=(-e "$term"); done
  for term in "${KEY_PATTERNS[@]}"; do key_args+=(-e "$term"); done

  scan_with -i -F "${fixed_args[@]}"
  scan_with -E "${key_args[@]}"

  if [ "$FOUND" -ne 0 ]; then
    # A committed file unchanged in the working tree, or this file, can be read
    # twice; each hit is printed once.
    awk '!seen[$0]++' "$HITS"
    printf 'private_scan: private terms found; remove them before pushing\n' >&2
    return 1
  fi
  return 0
}

main "$@"
