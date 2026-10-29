#!/usr/bin/env bash
# The three gates, in the only order that works: lint, then types, then tests.
#
# The order is not stylistic. Every red push this stack has seen came from running
# pytest first, seeing green, and discovering a lint/type error afterwards - and the
# follow-up edit that fixed the long line is what corrupted a docstring three separate
# times on the control-plane lane.
#
# Usage:  scripts/gate.sh          (all three)
#         scripts/gate.sh --fast   (lint + types only, for a pre-commit hook)
set -euo pipefail

cd "$(dirname "$0")/.."

# Prefer the project virtualenv; fall back to whatever `python` resolves to in
# CI where the environment is already prepared.
PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY="python"

echo "== ruff =="
"$PY" -m ruff check src tests scorpion_mff

echo "== mypy (strict) =="
"$PY" -m mypy src/scorpion scorpion_mff

if [ "${1:-}" = "--fast" ]; then
  echo "gates: lint and types clean"
  exit 0
fi

echo "== pytest =="
"$PY" -m pytest -q

echo "gates: all three clean"
