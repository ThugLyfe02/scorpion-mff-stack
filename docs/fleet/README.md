# The fleet

Three lanes, one gate, in tandem. The fleet is the machine that turns this repo from
"a pile of branches" into "reviewed pull requests" without a human typing `git`.

```
              ┌──────────────────────────────────────────────────────────┐
              │                       agent-fleet                        │
              │             workflow_dispatch / weekly cron              │
              └────────────┬──────────────────┬──────────────────┬───────┘
                           │                  │                  │
            architect lane │        main lane │                  │ semantic lane
        ┌──────────────────▼──┐   ┌───────────▼────────┐   ┌─────▼──────────────────┐
        │ claude-opus-5-5     │   │ deepseek-v4-pro    │   │ deepseek-v4-pro        │
        │ deterministic       │   │ coded hot path     │   │ calibrated semantic    │
        │ control plane       │   │ hardening          │   │ interpretation         │
        │        -> PR #1     │   │        -> PR #8    │   │        -> PR #3        │
        └──────────┬──────────┘   └──────────┬─────────┘   └──────────┬─────────────┘
                   │                         │                        │
              scripts/gate.sh          scripts/gate.sh          scripts/gate.sh
              ruff -> mypy -> pytest   ruff -> mypy -> pytest   ruff -> mypy -> pytest
```

## Lanes

`fleet.json` is the single source of truth. Each lane declares its id, branch,
model, prompt (`prompt_file`), continuation brief (`master_file`), and gate:

| lane | branch | model | deliverable |
|---|---|---|---|
| `architect` | `hardening/deterministic-control-plane` | `claude-opus-5-5` | event-sourced control plane + research frontier (PR **#1**) |
| `main` | `hardening/correlated-book-guard` | `deepseek-v4-pro` | Discord → Robinhood hot path hardening (GitHub PR **#8**; the older prose called it "PR#2") |
| `semantic` | `hardening/calibrated-semantic-acceleration` | `deepseek-v4-pro` | parser calibration, review queue, certification (PR **#3**) |

All three lanes hold the **same gate** (`scripts/gate.sh`: ruff → `mypy --strict` →
pytest), executed inside the lane's own worktree so it gates that lane's tree. A lane
that cannot pass the gate cannot open a pull request; the driver loops agent → gate →
fix up to `rounds` (default 3) and fails the job rather than ship red.

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

## The evidence manifest

`scripts/fleet/evidence.sh` turns a lane's tree into the PR body: every row is a check,
a verdict, and the command that produced it. `run-lane.sh` runs it after the commit it
ships and appends the result under the lane's prose, so a reviewer can re-run the claim
instead of believing it.

```bash
scripts/fleet/evidence.sh                       # describe the current tree
scripts/fleet/evidence.sh --lane main --json     # machine-readable, lane-checked
scripts/fleet/evidence.sh --no-gate              # facts only; the gate is REFUSED
scripts/fleet/evidence.sh --gate-log /tmp/g.log  # describe a gate run that happened
scripts/fleet/evidence.sh --claim tests="pytest -q"   # one command per claim
```

| row | what it refuses to claim |
|---|---|
| `identity`, `dirty` | a manifest generated over uncommitted work describes no commit |
| `lane` | a detached tip or branch that is not the lane's declared branch |
| `drift` | more than `--max-behind` commits behind `origin/<base>` (default 10); an unresolvable base is refused, not read as zero |
| `purity.imports:*` | a cross-lane import in either direction (`scorpion_mff` ↔ `src/scorpion`) |
| `purity.tree` | two packages in one tree (two truths for one invariant) |
| `gate`, `gate.<stage>` | a gate that did not run (`--no-gate` is REFUSED, never green) |
| `tests.collected` | a suite count the gate log did not print (collected, not re-run) |
| `claim:<label>` | a claim whose command exits non-zero |

Exit codes: `0` every row passed, `1` a row failed, `2` something could not be measured
(or the arguments were wrong). A refusal is a fact about the evidence, not a crash — the
same discipline `arm` applies to a stale quote.

## In CI

`.github/workflows/agent-fleet.yml` runs all three lanes in parallel on
`workflow_dispatch` and on a weekly cron. **Dispatch defaults to `--dry-run`** so a
human can watch the agents and the gates without shipping. Set `dry_run: false` to
actually open the pull requests.

Secrets required: `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`
(the lane model's provider credential is the one that gets used).

## Why three lanes

The architect lane, the main lane, and the semantic lane are three different codebases
answering three different questions:

- **Architect** — *what is the right control plane?* Event-sourced, durable,
  ambiguity-named, review-only. This is where the frontier techniques live, each
  behind a fit gate that refuses what the data cannot support. Owns **correctness
  truth**: a book must explain itself from its log.
- **Main** — *does the hot path that will place money hold its invariants?* The
  deterministic parser → engine → broker with the reduce-only governor, the
  correlated-book cap, the fill log, the confirmed-execution book, and the kill switch.
  708 tests, strict types.
  Owns **execution truth**: the only lane that can place an order.
- **Semantic** — *is the parse evidence or opinion?* Calibration, novelty, review
  queue, and a certificate a parser promotion must pass. Owns **interpretation truth**:
  no execution authority, no book mutation, no promotion on aggregate accuracy alone.

They must never drift into each other. The fleet keeps them separate, gated, and
reviewable as three PRs. See `DIRECTION.md` for where each one goes next, and
`ORPHAN.md` for the branch that is deliberately not a lane.

## Where the lanes are going

The fleet builds; the direction decides what deserves building. The documents, in order
of authority:

| Document | Answers |
|---|---|
| `master.prompt.md` | the shared law: lane map, seams, code/evidence/safety/change law, definition of done |
| `pr1.master.md`, `pr3.master.md`, `pr8.master.md` | the continuation brief for one lane: measured state, next three moves, refusals, kill criteria |
| `DIRECTION.md` | the thesis (not a lane brief): merge order, convergence gates, the one metric, what not to do |

A lane agent receives a composed brief, in this order: `docs/fleet/master.prompt.md`
(shared law) + the lane's `master_file` (continuation brief) + the lane's
`prompt_file` (session brief). The driver sends that composition once, in round 1; a
red gate re-sends only the failing block. The master files live on the default branch,
so they are read from the orchestration tree, never from the lane worktree.

If they disagree: the master prompt wins on law, the lane brief wins on territory, and
the session brief wins on this session's work.

## Worktrees and the orphan

Live worktrees (one per lane, plus one archive):

| Path | Branch | Status |
|---|---|---|
| `.` | `hardening/correlated-book-guard` | PR #8, the live lane |
| `.worktrees/pr1` | `hardening/deterministic-control-plane` | PR #1, the architect lane |
| `.worktrees/pr3` | `hardening/calibrated-semantic-acceleration` | PR #3, the semantic lane |
| `.worktrees/feat` | `feat/risk-budget-context` | **archive, no PR** — the orphan |

`.worktrees/feat` used to sit on a detached HEAD, which made its 9 commits invisible to
`git branch` and unreachable by any gate. It is attached to its branch now and treated as
a read-only harvest source; the row-by-row disposal map is `ORPHAN.md`. Never commit
there, never run a lane on it, never merge it.

## Token cost, made small on purpose

The fleet is a loop that pays per token, so the cost levers are built into the
config and the driver rather than left to the model:

| Lever | Where | What it does |
|---|---|---|
| Thinking level per lane | `fleet.json` | architect `medium`, main `low`, semantic `medium` - the build turn, not `high` |
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
