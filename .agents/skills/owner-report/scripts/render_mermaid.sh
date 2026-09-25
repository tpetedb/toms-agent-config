#!/usr/bin/env bash
# Render one Mermaid source file to PNG for an owner report.
#
# Usage: render_mermaid.sh <in.mmd> <out.png> [width]
#        render_mermaid.sh --help
# Uses mmdc from PATH if present, else npx with the mermaid-cli version that
# mise.toml at the repository root pins under [tools], else the major 12.
set -euo pipefail

usage() {
  sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
  "")
    usage >&2
    exit 2
    ;;
esac

in=${1:?input .mmd}
out=${2:?output .png}
width=${3:-1400}

pinned() {
  local top
  top="$(git rev-parse --show-toplevel 2>/dev/null)" || return 1
  [ -f "$top/mise.toml" ] || return 1
  sed -n 's/^"npm:@mermaid-js\/mermaid-cli" *= *"\([^"]*\)".*/\1/p' "$top/mise.toml" | head -n 1
}

if command -v mmdc >/dev/null 2>&1; then
  mmdc_cmd=(mmdc)
else
  version="$(pinned || true)"
  mmdc_cmd=(npx --yes "@mermaid-js/mermaid-cli@${version:-12}")
fi
# White background and 2x scale so the image reads on a phone and in dark mail clients.
"${mmdc_cmd[@]}" -i "$in" -o "$out" -b white -s 2 -w "$width" -q
test -s "$out" && echo "$out"
