#!/usr/bin/env bash
# private_scan.sh: refuse private terms in this public repository.
#
# The scan is private_scan.py next to this file and reads the term list
# private_terms.txt next to it too; this wrapper keeps the one name every caller
# uses. CI copies all three from the base revision into one folder and runs them
# from there (docs/DESIGN.md, section 8).
#
# Usage: scripts/private_scan.sh [--range BASE..HEAD] [--branch NAME] [--help]
# Exit:  0 and no output when clean; 1 with each hit on its own line; 2 on error.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Isolated mode: no PYTHONPATH, no user site and not the script's folder on the
# import path, so nothing beside the scan or in the environment is imported.
exec python3 -I "$here/private_scan.py" "$@"
