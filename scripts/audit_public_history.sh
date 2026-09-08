#!/usr/bin/env bash
set -euo pipefail

# Local operator helper. Run after cloning from a trusted workstation.
# This script does not mutate history; it identifies sensitive strings before a manual rewrite.
PATTERNS=(
  '717950448'
  'Robinhood'
  'account'
  'token'
  'secret'
  'password'
  'authorization'
)

for pattern in "${PATTERNS[@]}"; do
  echo "== history search: ${pattern} =="
  git log --all -S"${pattern}" --oneline --decorate || true
done

cat <<'EOF'

If a sensitive identifier appears in historical commits:
  1. Make the repository private first.
  2. Rotate/revoke any credential-like material.
  3. Use git-filter-repo/BFG from a trusted workstation to purge history.
  4. Force-push only after collaborators coordinate.
  5. Re-clone all working copies afterwards.

Masking a value in a later commit does NOT remove it from Git history.
EOF
