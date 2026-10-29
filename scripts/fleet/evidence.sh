#!/usr/bin/env bash
# The evidence manifest, launched: resolve an interpreter and a path the host can
# open, then hand the whole job to evidence.py (standard library only).
#
# Used by run-lane.sh to append a measured manifest to the pull-request body, and by
# a human to check what a lane would claim before writing the body by hand:
#
#   scripts/fleet/evidence.sh                                  # describe this tree
#   scripts/fleet/evidence.sh --lane main --json               # machine-readable
#   scripts/fleet/evidence.sh --no-gate                        # facts only (gate REFUSED)
#   scripts/fleet/evidence.sh --gate-log /tmp/gate.log         # reuse a run
#   scripts/fleet/evidence.sh --claim tests="pytest -q"        # one command per claim
#
# The tree described is the CURRENT directory, never the orchestration checkout: a
# lane's manifest must describe the lane's tree.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Git-Bash hands a native CPython a POSIX path it cannot open when automatic
# conversion is disabled (MSYS2_ARG_CONV_EXCL=*, which is how this fleet runs), so
# translate explicitly. On CI/Linux cygpath is absent and the path passes through.
native_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w -- "$1"; else printf '%s' "$1"; fi
}

PY="$REPO_ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$REPO_ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python"
command -v "$PY" >/dev/null 2>&1 || { echo "evidence: python is required" >&2; exit 2; }

exec "$PY" "$(native_path "$SCRIPT_DIR/evidence.py")" "$@"
