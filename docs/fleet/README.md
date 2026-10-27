# The fleet

Two lanes, one gate, in tandem. The fleet is the machine that turns this repo from
"two branches" into "two reviewed pull requests" without a human typing `git`.

```
                 ┌────────────────────────────────────────────────┐
                 │                 agent-fleet (main)             │
                 │   workflow_dispatch / weekly cron              │
                 └───────────────┬────────────────┬───────────────┘
                                 │                │
                  architect lane │                │ main lane
                 ┌───────────────▼───┐        ┌───▼───────────────┐
                 │ claude-opus-5-5   │        │ deepseek-v4-pro    │
                 │ deterministic     │        │ coded hot path     │
                 │ control plane     │        │ hardening          │
                 │   -> PR #1        │        │   -> PR #2         │
                 └───────┬───────────┘        └─────────┬─────────┘
                         │                              │
                    scripts/gate.sh               scripts/gate.sh
                    ruff -> mypy -> pytest        ruff -> mypy -> pytest
```

## Lanes

`fleet.json` is the single source of truth. Each lane declares its id, branch,
model, prompt, and gate:

| lane | branch | model | deliverable |
|---|---|---|---|
| `architect` | `hardening/deterministic-control-plane` | `claude-opus-5-5` | event-sourced control plane + research frontier |
| `main` | `hardening/correlated-book-guard` | `deepseek-v4-pro` | Discord → Robinhood hot path hardening |

Both lanes hold the **same gate** (`scripts/gate.sh`: ruff → `mypy --strict` →
pytest). A lane that cannot pass the gate cannot open a pull request; the driver
loops agent → gate → fix up to `rounds` (default 3) and fails the job rather than
ship red.

## Running a lane

```bash
# smoke: gate the lane as it exists, no agent, no push
FLEET_SKIP_DEPS=1 scripts/fleet/run-lane.sh main --dry-run --no-agent

# full: agent builds, gates, pushes, opens the PR
scripts/fleet/run-lane.sh architect
```

Flags:

- `--dry-run` — run the agent and the gate, never push or open a PR
- `--no-agent` — skip the agent, gate the tree as-is (CI smoke check)
- `--rounds N` — bound the agent → gate → fix loop

`FLEET_SKIP_DEPS=1` skips the `pip install -e ".[dev]"` step for re-runs where the
tooling is already present.

## In CI

`.github/workflows/agent-fleet.yml` runs both lanes in parallel on
`workflow_dispatch` and on a weekly cron. **Dispatch defaults to `--dry-run`** so a
human can watch the agents and the gates without shipping. Set `dry_run: false` to
actually open the pull requests.

Secrets required: `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`
(the lane model's provider credential is the one that gets used).

## Why two lanes

The architect lane and the main lane are two different codebases answering two
different questions:

- **Architect** — *what is the right control plane?* Event-sourced, durable,
  ambiguity-named, review-only. This is where the frontier techniques live, each
  behind a fit gate that refuses what the data cannot support.
- **Main** — *does the hot path that will place money hold its invariants?* The
  deterministic parser → engine → broker with the reduce-only governor, the
  correlated-book cap, the fill log, and the kill switch. 271 tests, strict types.

They must never drift into each other. The fleet keeps them separate, gated, and
reviewable as two PRs.

## Token cost, made small on purpose

The fleet is a loop that pays per token, so the cost levers are built into the
config and the driver rather than left to the model:

| Lever | Where | What it does |
|---|---|---|
| Thinking level per lane | `fleet.json` | architect `medium`, main `low` - the build turn, not `high` |
| Fix-turn thinking | `run-lane.sh` | a red gate re-runs the agent at `low`, because a fix is mechanical |
| Fix-turn input | `run-lane.sh` | only the **failure block** is re-sent, never the full brief + full log |
| Fix-turn session | `run-lane.sh` | each fix round gets a **fresh** `--session-id`, so pi does not reload round 1's whole transcript |
| Thinking token caps | `.pi/settings.json` | `thinkingBudgets` bounds reasoning output per level |
| Cache-miss noise | `.pi/settings.json` | `showCacheMissNotices` off - the fleet has no one watching the notice |

Why each lever exists:

* **`thinking` is the dominant output cost.** `high` on a frontier model spends
  far more on reasoning than on the diff. A build turn is `medium`/`low`; a fix
  turn is `low`. Both still pass the same `ruff -> mypy -> pytest` gate.
* **Re-sending the brief is the dominant input cost.** Round 1 gets the full
  brief. Rounds 2..N get a two-line instruction and the failing block, because
  that is all a fix decision needs. The full gate log can be thousands of lines
  of green dots above one failing test.
* **Resuming the session was re-sending the brief anyway.** Reusing one
  `--session-id` made pi reload the whole round-1 transcript (brief + build +
  every tool call) before appending the fix prompt. A fresh session per fix
  round drops that; the agent reads the working tree instead, which already
  holds its changes on disk - `read` is cheaper than re-transmitting the
  conversation that produced them.
* **The caps are a ceiling, not a target.** `.pi/settings.json` is committed so
  the fleet and a human get the same budget; tune `thinkingBudgets` in one place
  when a lane demonstrably needs more room to reason.

If a lane starts failing its gate on round 1 instead of building, raise that
lane's `thinking` one notch before touching the caps - the caps are the last
lever, not the first.
