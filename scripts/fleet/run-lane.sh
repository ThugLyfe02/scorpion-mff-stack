#!/usr/bin/env bash
# Drive one lane of the agent fleet end to end:
#
#   1. read the lane's definition from fleet.json
#   2. materialise the lane branch in an isolated worktree
#   3. run the lane's build agent (its model, its prompt)
#   4. gate the result (lint -> types -> tests) and loop back on failure
#   5. push the branch and open/refresh the lane's pull request
#
# The orchestration itself lives on the default branch; the lane's code lives in
# the worktree. A lane is idempotent: re-running it re-fetches the remote tip and
# re-drives the same gate, so a crashed run is always safe to retry.
#
# Usage:
#   scripts/fleet/run-lane.sh <lane-id> [--dry-run] [--no-agent] [--rounds N]
#
#   --dry-run    run the agent and the gate but never push or open a PR
#   --no-agent   skip the agent entirely (gate-only, for a CI smoke check)
#   --rounds N   bound the agent->gate->fix loop (default from fleet.json, else 3)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FLEET_JSON="${FLEET_JSON:-$REPO_ROOT/fleet.json}"

LANE_ID="${1:?usage: run-lane.sh <lane-id> [--dry-run] [--no-agent] [--rounds N]}"
shift

DRY_RUN=0
NO_AGENT=0
ROUNDS="${FLEET_ROUNDS:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-agent) NO_AGENT=1 ;;
    --rounds) ROUNDS="${2:?--rounds needs a value}"; shift ;;
    *) echo "run-lane: unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v git >/dev/null 2>&1 || { echo "run-lane: git is required" >&2; exit 2; }
command -v python >/dev/null 2>&1 || { echo "run-lane: python is required" >&2; exit 2; }

# ── resolve the lane definition ─────────────────────────────────────────────
if [ ! -f "$FLEET_JSON" ]; then
  echo "run-lane: $FLEET_JSON not found (run from the default branch)" >&2
  exit 2
fi

eval "$(python - "$LANE_ID" "$FLEET_JSON" <<'PY'
import json
import sys

lane_id, fleet_file = sys.argv[1], sys.argv[2]
fleet = json.load(open(fleet_file, encoding="utf-8"))

def q(value):
    return json.dumps(value)

try:
    lane = next(item for item in fleet["lanes"] if item["id"] == lane_id)
except StopIteration:
    raise SystemExit(f"run-lane: unknown lane {lane_id!r}")

print(f"BASE_REF={q(fleet['base_ref'])}")
print(f"BRANCH={q(lane['branch'])}")
print(f"TITLE={q(lane['title'])}")
print(f"MODEL={q(lane['model'])}")
print(f"THINKING={q(lane['thinking'])}")
print(f"PROMPT_FILE={q(lane['prompt_file'])}")
print(f"BODY_FILE={q(lane['body_file'])}")
print(f"GATE={q(lane['gate'])}")
print(f"CONFIG_ROUNDS={q(fleet.get('rounds', 3))}")
PY
)"

ROUNDS="${ROUNDS:-$CONFIG_ROUNDS}"

log() { printf '\033[1;34m[fleet:%s]\033[0m %s\n' "$LANE_ID" "$*"; }
fail() { printf '\033[1;31m[fleet:%s]\033[0m %s\n' "$LANE_ID" "$*" >&2; exit 1; }

PROMPT="$(cat "$REPO_ROOT/$PROMPT_FILE")"
BODY="$(cat "$REPO_ROOT/$BODY_FILE")"
WORKDIR="$REPO_ROOT/.worktrees/fleet-$LANE_ID"
RUNDIR="$(mktemp -d)"
GATE_LOG="$RUNDIR/gate.log"
AGENT_LOG="$RUNDIR/agent.log"

# ── materialise the lane branch ─────────────────────────────────────────────
log "fetching $BRANCH"
git -C "$REPO_ROOT" fetch --depth=1 origin "$BRANCH" "$BASE_REF" >/dev/null 2>&1 \
  || { echo "run-lane: cannot fetch origin/$BRANCH" >&2; exit 2; }

if [ -d "$WORKDIR/.git" ]; then
  git -C "$WORKDIR" fetch --depth=1 origin "$BRANCH" >/dev/null 2>&1 || true
  git -C "$WORKDIR" checkout --detach "origin/$BRANCH" >/dev/null 2>&1 || true
  git -C "$WORKDIR" reset --hard "origin/$BRANCH" >/dev/null 2>&1 || true
else
  git -C "$REPO_ROOT" worktree add --detach "$WORKDIR" "origin/$BRANCH" >/dev/null
fi

log "worktree ready at $WORKDIR"

# ── tooling ─────────────────────────────────────────────────────────────────
# The lane's own gate picks `.venv` when present and falls back to `python`.
# In CI there is no venv, so install the lane's dev extras into the active python.
if [ "$NO_AGENT" -eq 0 ] && ! command -v pi >/dev/null 2>&1; then
  fail "pi is not installed (npm install -g @earendil-works/pi-coding-agent)"
fi
if ! command -v gh >/dev/null 2>&1 && [ "$DRY_RUN" -eq 0 ]; then
  fail "gh is not installed (required to open the pull request)"
fi

if [ "${FLEET_SKIP_DEPS:-0}" = "1" ]; then
  log "dependency install skipped (FLEET_SKIP_DEPS=1)"
else
  ( cd "$WORKDIR" && python -m pip install -q -e ".[dev]" ) \
    || fail "dependency install failed for $BRANCH"
fi

# ── the agent -> gate -> fix loop ───────────────────────────────────────────
run_agent() {
  local prompt="$1"
  ( cd "$WORKDIR" && pi --print \
      --model "$MODEL" \
      --thinking "$THINKING" \
      --session-id "fleet-$LANE_ID" \
      --approve \
      "$prompt" ) >"$AGENT_LOG" 2>&1 || true
}

run_gate() {
  ( cd "$WORKDIR" && bash -c "$GATE" ) >"$GATE_LOG" 2>&1
}

if [ "$NO_AGENT" -eq 0 ]; then
  log "building with $MODEL (thinking=$THINKING)"
  run_agent "$PROMPT"
else
  log "agent skipped (--no-agent); gating the tree as-is"
fi

gate_ok=0
round=1
while [ "$round" -le "$ROUNDS" ]; do
  log "gate round $round/$ROUNDS"
  if run_gate; then
    log "gate green"
    gate_ok=1
    break
  fi
  if [ "$round" -lt "$ROUNDS" ] && [ "$NO_AGENT" -eq 0 ]; then
    log "gate red; returning failures to the agent for a fix"
    run_agent "$(printf '%s\n\nThe quality gate failed after your changes. Read the output below, '
      'fix every failure in the source or the tests, and re-run the gate yourself until it is '
      'green. Do not lower the gate, silence it, or add ignores. Gate output:\n\n%s' \
      "$PROMPT" "$(cat "$GATE_LOG")")"
  fi
  round=$((round + 1))
done

if [ "$gate_ok" -eq 0 ]; then
  fail "gate stayed red after $ROUNDS rounds - see $GATE_LOG"
fi

# ── ship ────────────────────────────────────────────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
  log "dry run: gate green, nothing pushed and no PR opened"
  exit 0
fi

cd "$WORKDIR"
if git status --porcelain | grep -q .; then
  git add -A
  git commit -q -m "fleet($LANE_ID): build $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    || log "nothing to commit"
fi

log "pushing $BRANCH"
git push origin "HEAD:$BRANCH" >/dev/null 2>&1 || fail "push failed for $BRANCH"

log "opening the pull request into $BASE_REF"
EXISTING="$(gh pr list --head "$BRANCH" --state open --json number --jq '.[0].number' 2>/dev/null || true)"
if [ -n "$EXISTING" ]; then
  gh pr edit "$EXISTING" --title "$TITLE" --body "$BODY" >/dev/null
  log "updated existing PR #$EXISTING"
else
  URL="$(gh pr create --base "$BASE_REF" --head "$BRANCH" --title "$TITLE" --body "$BODY")"
  log "opened $URL"
fi

log "done"
