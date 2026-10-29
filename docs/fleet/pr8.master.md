# PR #8 — the live lane

**Branch:** `hardening/correlated-book-guard` · **Worktree:** this tree
**Continuation brief for `scorpion_mff/`.** Read `master.prompt.md` first.

Measured baseline on 2026-10-29:

```text
drift vs main     11 ahead, 0 behind
gate               ruff clean · mypy --strict clean (30 files) · 589 passed
live wiring        RH order endpoint unconfirmed · quote feed unconfigured · Gateway token absent
```

---

## 1. What this lane is

The only lane that can place an order. A MoneyForFun Discord options post becomes a
Robinhood order in under ten seconds through deterministic code:

```text
Gateway MESSAGE_CREATE
  -> parser (Entry | Add | Trim | Exit | Ignore | Ambiguous)
  -> engine (sizing, pricing, one-lot pipe, max 2, +25% gate, no chase)
  -> ladder (halt -> kill switch -> governor -> pre-trade budget -> exposure)
  -> broker (PaperBroker | Robinhood Agentic HTTP)
  -> fill log + ledger (tri-state) + stops + marks
  -> reconcile (book vs venue) + verify (log integrity) + recover (triage)
```

Read before writing, in this order: `HOTPATH.md` (especially "Status, honestly"),
`live/RULES.lock.md`, `live/policy.json`, `POLICY.md`, `TCA.md`, then the last ten
commits. The design history is in `docs/GEMS.md`; one acceptance command per gem.

**The lane boundary:** this lane never imports `src/scorpion`. PR #1 is paper/review.
A safety decision from PR #1 is *ported* here — with a matrix row, its own test, and a
GEMS entry — never imported, never copied wholesale.

---

## 2. Invariants beyond the master law

1. **The fill log is the product.** Every decision — taken, skipped, rejected, refused,
   unknown — lands with a reason. The 9/8 failure was silence, not a wrong trade.
2. **An order is not real until an ack says so.** Intent is written before the send;
   a transport exception is `UNKNOWN` (non-terminal, pages on an exit), never a fake
   reject. `recover` resolves by venue lookup; it never re-sends.
3. **Partial fills are first-class.** A 5-lot filling 3-then-2 is the same realised R
   as one 5-lot; a 2-only fill leaves the remainder tracked. A partial never counts as
   a completed intent.
4. **The book must be reconcilable with the venue.** Drift halts *buys* and pages;
   exits stay legal. A malformed snapshot is refused, not repaired.
5. **No chase, no re-entry after close, add-once per generation.** These are locked
   in `live/RULES.lock.md`; a test pins each one.
6. **Quote age is a safety input.** A stale or crossed quote is `no_quote`, not a
   price. The breakeven stop, marks, and exits all inherit that rule.

---

## 3. Where this lane goes next

The lane is offline-proven and live-unproven. The next three moves convert offline
proof into execution truth, in dependency order. Do not start move 3 before move 2.

### Move 1 — make confirmed execution the authority (not the engine's memory)

Today the engine's position book is a decision cache; the broker is the filler; the
ledger is tri-state. What is still missing is the *durable* middle: intent → ack →
fill applied cumulatively, surviving a restart and a duplicate ack, with partials and
oversells handled explicitly.

Harvest the orphan's `scorpion_mff/execution.py` **as a design reference, by hunk** —
not as a merge, not as a copy. It models cumulative acks (a second ack of 3 after 2
applies delta 1), append-only `live/execution.jsonl`, baseline persistence so a restart
cannot erase a drawdown, and `sync_engine()` from confirmed events. Some of that is
already better in this lane's ledger/reconcile work; keep only what the tests prove
missing.

Acceptance:

```bash
python -m scorpion_mff.cli verify            # log contiguous, intents terminal
python -m pytest -q tests/test_ledger.py     # cumulative ack, oversell clamp, restart replay
```

- 5-lot fills 3 then 2 == one 5-lot fill, same realised R.
- A duplicate ack cannot double-fill; a torn final line is ignored, mid-file corruption refuses.
- Restart mid-send leaves `UNKNOWN`; `recover` settles it from a lookup; nothing re-sends.

### Move 2 — a scheduled venue reconciliation, on a recorded snapshot

`reconcile.py` already names `phantom_local` / `missing_local` / `quantity_mismatch`
and halts buys on drift. The missing piece is the snapshot source. Build the adapter
against a **recorded fixture** first (the same discipline that made the HTTP adapter
provable against a local server), and wire it into the ops loop.

Acceptance:

```bash
python -m scorpion_mff.cli reconcile --snapshot tests/fixtures/venue-positions.json
# exit 0 clean · 1 drift (halted:reconcile-drift, page sent) · 2 malformed (no halt)
```

- A drifted snapshot halts buys, pages, and never blocks an exit.
- A malformed snapshot exits 2 and does not mutate the book.
- The clean case is idempotent: running twice produces one state, no duplicate alerts.

### Move 3 — shadow-live parity, then one real ack

Run the *same* listener/engine code over a recorded live session and prove decision
parity with the offline replay, then take one credentialed round trip.

Acceptance:

```bash
python -m scorpion_mff.cli replay tapes/<session>.json
python -m scorpion_mff.cli tca fills          # refuses under 10 priced fills
python -m scorpion_mff.cli marks --check      # withholds an unpriceable total
```

- Shadow and replay produce identical intents per event, in order, with the same reasons.
- One real ack is captured with tokens stripped; the credential never appears in an error string.
- `ask_multiple` changes only on `tca fills` evidence; before ten priced fills it refuses.

If credentials stay blocked, Move 1 and Move 2 still ship. The lane's job is to be
ready the day they clear, not to pretend they cleared.

---

## 4. What this lane refuses

- An LLM anywhere between a post and an order.
- A live action on a missing/stale/crossed quote, an unparsed post, a drifted book, or
  a missing policy. Refusal is the feature.
- A kill switch that blocks exits; a retry on a transport-unknown; a "reject" written
  for a send that may have reached the venue.
- Absorbing the orphan's `evidence.py` / `context.py`. Measurement belongs to PR #1;
  scanner context belongs to PR #6 (research-only). This lane may consume a pinned
  report, never own the study.
- A copy of `src/scorpion`. If a decision from PR #1 is needed, port it with a matrix
  row and a GEMS acceptance command.

---

## 5. Handoff to the fleet

- **To PR #1:** the venue snapshot schema and the fill-log schema, so the control
  plane's reconciliation and TCA study the same shapes the live lane emits.
- **To PR #3:** the parser version currently pinned in `scorpion_mff/parser.py` and the
  fixture set that must keep passing. A parser promotion in PR #3 is not adopted here
  until a certificate digest is pinned in this lane's tests.
- **From the orphan:** only the gaps Move 1 names. See `docs/fleet/ORPHAN.md` for the
  harvest map and the hunk-level rule.

---

## 6. Kill criteria

Stop adding to this lane and ship what is green when any of these is true:

- The gate cannot stay green for two consecutive sessions (the lane is over-built).
- A new capability cannot name an acceptance command in one line.
- Live credentials remain blocked after the offline stack is complete: ship shadow
  mode, keep `arm --live` refusing, and do not widen scope to compensate.
- The next change would make the book less explainable from its log, even if it makes
  a report look better.

---

## 7. Preflight

```bash
git status -sb
git log --oneline -5
bash scripts/gate.sh
python -m scorpion_mff.cli expiry audit     # refuses assignment projection; read the refusal
```

Then write. The commit log is the audit trail; make it readable by someone who was
not here.
