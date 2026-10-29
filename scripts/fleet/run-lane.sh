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

# ── host translation ────────────────────────────────────────────────────────
# A native CPython on Windows cannot open the POSIX path Git-Bash hands it: `/c/...`
# is not a drive, so the resolver died with a FileNotFoundError that the `eval`
# swallowed and `set -u` reported as an unbound CONFIG_ROUNDS. Translate the path
# explicitly, and pick the interpreter the lane's own gate would pick (`.venv`
# first, exactly like scripts/gate.sh) so the resolver and the gate agree about
# which environment is being described.
native_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w -- "$1"; else printf '%s' "$1"; fi
}

WORKDIR="$REPO_ROOT/.worktrees/fleet-$LANE_ID"
# Git for Windows is a native binary and this environment runs with
# MSYS2_ARG_CONV_EXCL=* - no POSIX path handed to it is translated, so `git -C
# /c/...` fails with "cannot change to". Every path that crosses into a native
# tool goes through native_path; bash keeps the POSIX form for its own file
# tests and redirections.
REPO_NATIVE="$(native_path "$REPO_ROOT")"
WORKDIR_NATIVE="$(native_path "$WORKDIR")"
PY="$WORKDIR/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$WORKDIR/.venv/bin/python"
[ -x "$PY" ] || PY="$REPO_ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$REPO_ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python"
command -v "$PY" >/dev/null 2>&1 || { echo "run-lane: python is required" >&2; exit 2; }

# The gate re-resolves its own interpreter from the worktree: `.venv/Scripts` there
# if one exists, otherwise whatever `python` means on PATH. Put the interpreter this
# script resolved - the one it installs into - at the front of PATH, so a worktree
# without a venv cannot gate with a second, dependency-free python.
if [ "$PY" != "python" ]; then
  PATH="$(dirname "$PY"):$PATH"
  export PATH
fi

# ── resolve the lane definition ─────────────────────────────────────────────
if [ ! -f "$FLEET_JSON" ]; then
  echo "run-lane: $FLEET_JSON not found (run from the default branch)" >&2
  exit 2
fi

lane_def="$("$PY" - "$LANE_ID" "$(native_path "$FLEET_JSON")" <<'PY'
import json
import shlex
import sys

lane_id, fleet_file = sys.argv[1], sys.argv[2]
with open(fleet_file, encoding="utf-8") as handle:
    fleet = json.load(handle)

def q(value):
    # Shell-quoted, not JSON-quoted: the output is eval'd by bash, and a title or
    # model with a `$` or a backtick in it must not be re-interpreted.
    return shlex.quote(str(value))

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
print(f"MASTER_FILE={q(lane.get('master_file', ''))}")
print(f"BODY_FILE={q(lane['body_file'])}")
print(f"GATE={q(lane['gate'])}")
print(f"CONFIG_ROUNDS={q(fleet.get('rounds', 3))}")
PY
)" || { echo "run-lane: cannot parse $FLEET_JSON" >&2; exit 2; }

[ -n "$lane_def" ] || { echo "run-lane: $FLEET_JSON produced no lane definition" >&2; exit 2; }
eval "$lane_def"
: "${CONFIG_ROUNDS:?run-lane: $FLEET_JSON did not declare rounds}"

ROUNDS="${ROUNDS:-$CONFIG_ROUNDS}"

log() { printf '\033[1;34m[fleet:%s]\033[0m %s\n' "$LANE_ID" "$*"; }
fail() { printf '\033[1;31m[fleet:%s]\033[0m %s\n' "$LANE_ID" "$*" >&2; exit 1; }

# ── compose the brief ────────────────────────────────────────────────────────
# Three documents, in order of authority: the fleet's shared law (`master.prompt.md`),
# this lane's continuation brief (`master_file`), and the session brief. They are sent
# once, in round 1; a fix round re-sends only the failing block (see run_agent below).
MASTER_PROMPT_FILE="$REPO_ROOT/docs/fleet/master.prompt.md"
[ -f "$MASTER_PROMPT_FILE" ] || fail "missing $MASTER_PROMPT_FILE"
if [ -n "$MASTER_FILE" ]; then
  [ -f "$REPO_ROOT/$MASTER_FILE" ] || fail "missing lane master brief: $MASTER_FILE"
  LANE_MASTER="$(cat "$REPO_ROOT/$MASTER_FILE")"
else
  LANE_MASTER=""
fi
PROMPT="$(cat "$MASTER_PROMPT_FILE")"$'\n\n'"$LANE_MASTER"$'\n\n'"$(cat "$REPO_ROOT/$PROMPT_FILE")"
BODY="$(cat "$REPO_ROOT/$BODY_FILE")"
RUNDIR="$(mktemp -d)"
GATE_LOG="$RUNDIR/gate.log"
AGENT_LOG="$RUNDIR/agent.log"

# ── materialise the lane branch ─────────────────────────────────────────────
# Shallow fetches are a CI optimisation and must be opt-in: run on a developer clone
# they rewrite that clone into a shallow repository (`--depth=1` truncates the history
# behind `origin/<branch>` and drops a `.git/shallow` marker), which breaks `git log`,
# `git blame` and any history check after the first dry run. `FLEET_SHALLOW=1` (which
# the workflow sets) restores the fast path; an already-shallow clone keeps it.
FETCH_DEPTH=""
if [ "${FLEET_SHALLOW:-0}" = "1" ] \
  || [ "$(git -C "$REPO_NATIVE" rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
  FETCH_DEPTH="--depth=1"
fi

log "fetching $BRANCH"
# $FETCH_DEPTH is intentionally unquoted so an empty value expands to no argument.
git -C "$REPO_NATIVE" fetch $FETCH_DEPTH origin "$BRANCH" "$BASE_REF" >/dev/null 2>&1 \
  || { echo "run-lane: cannot fetch origin/$BRANCH" >&2; exit 2; }

# In a linked worktree `.git` is a file (a gitdir pointer), not a directory, so the
# -d test never matched and every re-run tried to create the worktree a second time.
if [ -e "$WORKDIR/.git" ]; then
  git -C "$WORKDIR_NATIVE" fetch $FETCH_DEPTH origin "$BRANCH" >/dev/null 2>&1 || true
  git -C "$WORKDIR_NATIVE" checkout --detach "origin/$BRANCH" >/dev/null 2>&1 || true
  git -C "$WORKDIR_NATIVE" reset --hard "origin/$BRANCH" >/dev/null 2>&1 || true
else
  git -C "$REPO_NATIVE" worktree add --detach "$WORKDIR_NATIVE" "origin/$BRANCH" >/dev/null
fi

log "worktree ready at $WORKDIR"

# ── tooling ─────────────────────────────────────────────────────────────────
# The lane's own gate picks `.venv` when present and falls back to `python`.
# In CI there is no venv, so install the lane's dev extras into the active python.
# `$PY` is resolved once, above, with the same preference - installing into one
# interpreter and gating with another is how a "missing dependency" red appears.
if [ "$NO_AGENT" -eq 0 ] && ! command -v pi >/dev/null 2>&1; then
  fail "pi is not installed (npm install -g @earendil-works/pi-coding-agent)"
fi
if ! command -v gh >/dev/null 2>&1 && [ "$DRY_RUN" -eq 0 ]; then
  fail "gh is not installed (required to open the pull request)"
fi

if [ "${FLEET_SKIP_DEPS:-0}" = "1" ]; then
  log "dependency install skipped (FLEET_SKIP_DEPS=1)"
elif "$PY" -c "import mypy, pytest, ruff" >/dev/null 2>&1; then
  # The lane's own interpreter already carries the three gate tools (the usual case
  # when the repository venv is reused). Editable-installing the worktree anyway would
  # repoint the repository venv at a throwaway tree for no gain: pytest resolves the
  # package from the worktree through `pythonpath = ["."]`.
  log "gate tools already present in $PY; install skipped"
else
  ( cd "$WORKDIR" && "$PY" -m pip install -q -e ".[dev]" ) \
    || fail "dependency install failed for $BRANCH"
fi

# ── the agent -> gate -> fix loop ───────────────────────────────────────────
#
# Token cost is a real budget line, and it is mostly burned in four places:
#   1. `--thinking high` on a frontier model (output tokens, per turn);
#   2. re-sending the FULL build brief on every fix round (input tokens);
#   3. re-sending the FULL gate log on every fix round (input tokens);
#   4. RESUMING the same `--session-id` on every fix round - pi reloads the whole
#      prior transcript (brief + build + every tool call) before adding the fix
#      prompt, so the "tail-only" fix turn was still paying for round 1 in full.
#
# The lane's configured thinking level is for the build turn. A fix turn is
# mechanical - read the failure, fix it, re-run the gate - so it runs one level
# below the build, sees only the failure block (never the green dots above it),
# and gets a FRESH session. The working tree is the agent's memory: the changes
# it made in round 1 are on disk, and `read` is cheaper than re-transmitting the
# whole conversation that produced them.
run_agent() {
  local prompt="$1"
  local thinking="${2:-$THINKING}"
  local session="${3:-fleet-$LANE_ID}"
  ( cd "$WORKDIR" && pi --print \
      --model "$MODEL" \
      --thinking "$thinking" \
      --session-id "$session" \
      --approve \
      "$prompt" ) >"$AGENT_LOG" 2>&1 || true
}

gate_brief() {
  # The gate stops at the first failing tool (`set -e`), so the failure is always
  # near the end. pytest's green dots above the FAILURES block are noise the
  # agent does not need re-sent: when a FAILURES/ERRORS block exists, send from
  # there to the end. Otherwise (ruff/mypy fail first, and their log is already
  # short) fall back to the raw tail.
  if grep -qE '^=+ (FAILURES|ERRORS) =+' "$GATE_LOG"; then
    sed -n '/^=* \(FAILURES\|ERRORS\) =*/,$p' "$GATE_LOG"
  else
    tail -n 120 "$GATE_LOG"
  fi
}

run_gate() {
  ( cd "$WORKDIR" && bash -c "$GATE" ) >"$GATE_LOG" 2>&1
}

if [ "$NO_AGENT" -eq 0 ]; then
  log "building with $MODEL (thinking=$THINKING)"
  run_agent "$PROMPT" "$THINKING"
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
  # With no agent in the loop there is nothing that can change the tree, so a red
  # gate is final. Burning the remaining rounds would only re-run the same failure.
  if [ "$NO_AGENT" -eq 1 ]; then
    fail "gate red (agent skipped, so no fix round is possible) - see $GATE_LOG"
  fi
  if [ "$round" -lt "$ROUNDS" ] && [ "$NO_AGENT" -eq 0 ]; then
    log "gate red; returning a compact fix brief to the agent"
    # The full brief was already given in round 1. Sending it again plus the
    # whole log is the fleet's largest avoidable token sink; a fix round needs
    # only the failing block and the instruction not to weaken the gate. A
    # fresh session id means the agent does NOT reload round 1's transcript -
    # it reads the working tree instead, which already holds its changes.
    fix_prompt="The quality gate (ruff -> mypy --strict -> pytest, in that order) failed after your changes. Read the failing block below, fix every failure in the source or the tests, and re-run the gate yourself until it is green. Do not lower the gate, silence it, or add ignores. Read any file you need; change only what the failure names. Gate output (failure block):"
    fix_prompt="$fix_prompt"$'\n\n'"$(gate_brief)"
    run_agent "$fix_prompt" "low" "fleet-$LANE_ID-r$round"
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

# ── the PR body is a manifest, not a story ──────────────────────────────────
# "Reviewers do not merge prose, they merge a diff they can re-run" (DIRECTION.md
# §4). The gate has just run, so the manifest describes that run (--gate-log)
# instead of paying for it twice. It is generated AFTER the commit, so its `dirty`
# row describes the commit being shipped rather than the agent's leftovers. If the
# recorder refuses, its refusals are still appended: a body that names an
# unmeasurable row is worth more than a body that hides one.
MANIFEST="$RUNDIR/manifest.md"
"$REPO_ROOT/scripts/fleet/evidence.sh" --lane "$LANE_ID" \
  --gate-log "$GATE_LOG" --gate-exit 0 --out "$MANIFEST" >/dev/null 2>&1 || true
if [ -s "$MANIFEST" ]; then
  BODY="$(printf '%s\n\n---\n\n%s' "$BODY" "$(cat "$MANIFEST")")"
  log "manifest appended to the PR body: $(grep -c '' "$MANIFEST") lines"
else
  log "manifest: evidence produced nothing; the PR body stays prose"
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
