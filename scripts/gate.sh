#!/usr/bin/env bash
# The three gates, in the only order that works.
#
# Lint first, then types, then tests. The order is not stylistic: every red push in this branch came
# from running pytest first, seeing green, and discovering E501 afterwards - and the follow-up edit
# that fixed the line length is what corrupted a docstring three separate times.
#
# Usage:  scripts/gate.sh          (all three)
#         scripts/gate.sh --fast   (lint + types only, for a pre-commit hook)
set -euo pipefail

cd "$(dirname "$0")/.."

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
