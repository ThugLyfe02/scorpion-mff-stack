#!/usr/bin/env bash
# The three gates for the semantic lane, in the only order that works:
# lint, then types, then tests.
#
# This lane owns `src/scorpion` and its research packages. It does not carry the
# live hot path (`scorpion_mff/`): those safety answers are single-source in the
# live lane, and a copy here would be a second source of truth for an invariant
# this lane only measures.
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
"$PY" -m ruff check src tests

echo "== mypy (strict) =="
"$PY" -m mypy src/scorpion

if [ "${1:-}" = "--fast" ]; then
  echo "gates: lint and types clean"
  exit 0
fi

echo "== pytest =="
PYTHONPATH=src:. "$PY" -m pytest -q

echo "gates: all three clean"
