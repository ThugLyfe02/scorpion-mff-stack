# PR #1 — the architect lane

**Branch:** `hardening/deterministic-control-plane` · **Worktree:** `.worktrees/pr1`
**Continuation brief for `src/scorpion/`.** Read `master.prompt.md` first.

Measured baseline on 2026-10-29:

```text
drift vs main     81 ahead, 0 behind (this lane already contains main)
gate               ruff clean · mypy --strict clean · 824 passed
lane boundary      paper / review only; no live sender; no copy of scorpion_mff
```

Two facts that control this lane's next session:

1. `docs/REPLAY_GAP.md` still says **"Status: open"**, but commit `afaa7a8` closed it:
   the expiry sweep now appends `kind="expire"`, `verify.apply_applied` has an `expire`
   branch, and `tests/test_replay_guarantee.py` pins the corpus lockstep. **The code is
   right and the doc is lying.** Fix the doc before writing new code.
2. The corpus is only **3/107 contract-days measurable (2.8%)**. Three separate
   analyses refuse to answer because the data plane is missing. That is the lane's
   binding constraint, not the number of frontier modules.

---

## 1. What this lane is

The reference semantics and the research lab. It is deliberately not the live path.

```text
message -> parser -> association -> reducer (risk) -> effects
        -> authorization ladder -> outbox (durable, no blind retries)
        -> paper Sender -> settlement -> applied log -> verify
```

The applied log is the truth. `verify` replays it and diffs against the live book,
field by field. A book that cannot explain itself must not trade.

Source of truth for what exists: `docs/ARCHITECTURE_V2.md`,
`docs/IMPLEMENTATION_STATUS.md`, `docs/ROADMAP_20K.md`, `docs/FRONTIER_7.md`,
`docs/TOURNAMENT.md`, `docs/BACKTEST_CORRECTNESS.md`, `docs/SECURITY.md`.

**The lane boundary:** this lane may *define* a decision that the live lane ports, but
it may not place an order, and it may not grow a second `scorpion_mff/`. Both copies
were already deleted once; do not recreate them. The port matrix lives in the live
lane's `HOTPATH.md`, not here.

---

## 2. Invariants beyond the master law

1. **Event sourcing is a claim about the log, not about the code.** Every state
   change is an applied event: `signal`, `fill`, `expire`, `halt`. If a path closes,
   opens, releases or halts, it appends a row and `apply_applied` reproduces it.
   `replay_applied(rows) == live_state` at **every prefix**, not just the end.
2. **No order without an envelope.** `--live`, a configured quote feed, a valid
   non-stale policy, and an authorization envelope — all four, or no send.
3. **Idempotency keys are recorded at send time and read back.** Never re-derived; a
   second copy of the format drifts silently.
4. **A release frees the quantity as well as the slot.** A released position holding
   exposure jams the day roll.
5. **No timezone is assumed.** The corpus's `time_et` is **Pacific**, proved by
   measurement; a bar set spans exactly one session; an unmeasurable trade is named.
6. **The fit gate refuses what the data cannot support.** A model fit on too few
   trades is a refusal, not a low-confidence result. The path-signature gate on 107
   trades is the model.
7. **Never average below the floor.** Withholding an aggregate is a deliverable.
   Common random numbers across compared scenarios, so a delta is a threshold.

---

## 3. Where this lane goes next

### Move 1 — provenance repair (do this first; it is hours, not weeks)

Run the corpus verification, then make the docs say what the code does.

```bash
scorpion verify --db <corpus.db>          # expect ok: true, 0 divergences
python -m pytest -q tests/test_replay_guarantee.py
```

Update `docs/REPLAY_GAP.md` to **closed**, with the command output and the commit that
closed it, and keep the "wider lesson" section. Add a status test or a docs check that
fails when a status line contradicts a pinned test. Rationale: a stale "open" headline
is how a 43-minute re-fix starts.

### Move 2 — Phase 1 data plane (`bars.py`, `coverage.py`, `sessions.py`)

This is the highest-ROI work in the entire stack. Every measurement currently refuses
because the join is missing: mark-out 3/107, TCA 2/18, attribution on 91/93. Building
another analysis first is how a research desk becomes elaborate fiction.

```bash
scorpion bars ingest --source <path>      # accepted/rejected with a named reason each
scorpion bars coverage                    # per contract: session length and gaps
scorpion coverage --corpus ... --bars ... # measurable/total per channel, reasons listed
```

Acceptance is the roadmap's, not a paraphrase: non-monotonic clocks, duplicate
timestamps, bracket failures, short sessions, and unprovable timezones are **rejected
with a reason**; measurable coverage is reported per channel and per month.

### Move 3 — attribution, then lifecycle/partial fills

With lifecycles and the data plane, the counterfactual becomes measurable:

```bash
scorpion attribute --variants <file>      # each rule reproduces the total delta within 0.01R
```

Then the lifecycle/partial-fill work from Phase 3.1. Note the seam: partial fills in
the **live** lane are PR #8's move 1. Here they are a paper/corpus model. Share the
*decision*, not the module: a port matrix row plus an acceptance command in each lane.

### Do **not** build a second `exposure.py` or `stops.py`

The live lane already owns exposure, stops, and venue reconciliation, tested against
the money path. A PR #1 copy would be a second source of truth for a risk invariant.
If the control plane needs them, consume the live lane's policy JSON semantics through
an explicit port (matrix row + test), or defer until the live implementation is
certified. This is the highest-risk duplication left in the roadmap.

### Research frontier: hold the line

Path signatures, Hawkes, GP tails, Wasserstein drift, rough volatility: each ships
only behind a fit gate that refuses an under-sized fit. The tournament's honest verdict
is `INCUMBENT_HELD`. A twelfth frontier module before measurability moves is a
liability, not an edge.

---

## 4. What this lane refuses

- Any live order, any real Sender, any credential requirement in a test.
- A second copy of `scorpion_mff/` or a second exposure/stops implementation.
- A claimed model on <20 observations; a trend on 12 trades; a conformal interval on 93.
- Describing the plane as "event-sourced" without a green `verify` on the corpus.
- A verifier that raises on the first bad row; it lists every divergence.

---

## 5. Handoff to the fleet

- **To PR #8:** a decision is portable only as a named rule + a test + an acceptance
  command. Hand over the *finding*, not the branch. The live lane writes the port row.
- **To PR #3:** the adjudication and applied-log shapes the certification corpus should
  use. The semantic lane must not invent a second state fingerprint.
- **To the fleet:** the coverage number. Publish measurable/total per channel every
  time it moves; it is the fleet's leading indicator.

---

## 6. Kill criteria

- The lane grows another module without moving measurability or closing a defect.
- A research module cannot name a fit gate, a floor, and a refusal reason.
- The corpus still refuses after Phase 1: stop adding analyses and fix ingestion.
- A change makes the book less explainable from its log, even if a report improves.

---

## 7. Preflight

```bash
git status -sb
git rev-list --left-right --count main...HEAD
bash scripts/gate.sh
grep -n "Status:" docs/REPLAY_GAP.md     # expect "closed" once Move 1 lands
```

Then write. The standard is the one a desk applies to its own execution code: every
claim backed by a command's output, every invariant enforced by a test, every refusal
named.
