# The replay gap: open defect, fully characterised

**Status:** open. `verify` returns `ok: false` on a corpus-scale run. The applied log does not
yet reproduce the live book. Do not describe the control plane as event-sourced until this is
closed.

Measured on the 107-trade corpus, 88 taken, 327 contracts:

    verified            false
    divergences         47
      29 positions      live=present, replayed=missing
      10 log entries    "SettlementError: no position for <key>"
       4 status/quantity  CLOSED vs OPEN, 0 vs 1  (QQQ|PUT|724|2026-08-12, SPY|CALL|712|2026-04-21)
       4 risk fields      market_day 2026-09-03 vs 2026-08-12, equity, peak, realised

## What has been ruled out, with evidence

1. **Serialization loss.** Every stored payload round-trips through `_signal_from_payload`
   unchanged, and a cross-check of the indexed `applied_events.contract_key` column against the
   key the payload derives returns **0 mismatches in 107 rows**. `contract_key` is a property
   and therefore absent from `dataclasses.asdict`, which is why the cross-check reads the
   column instead of the payload.
2. **Key derivation.** No row's stored key disagrees with its derived key.
3. **Divergence direction.** All 29 position divergences point one way (`present` / `missing`),
   never the reverse. Nothing is invented by the replay; things are omitted.

## The cascade, as characterised

The four risk-field divergences name the mechanism: **the replay's `market_day` is stuck at
2026-08-12 while the live book reached 2026-09-03.** The replay stopped rolling days roughly
three quarters of the way through the corpus, and everything after that point degrades:

    first fill whose position cannot be applied
        -> SettlementError, collected (not raised)
        -> that position never closes
        -> quantity stays on the book past its expiry
        -> the next day roll is refused / does not advance
        -> every later signal is applied against a stale book
        -> more fills fail, more positions go missing

This is a **cascade with one origin**, not 47 independent defects. The count of divergences is a
function of where the cascade starts, which is why fixing the last symptom would change nothing.

## PINPOINTED: first divergence at seq 22

The lockstep diff ran 173 marks across the corpus and returned the first disagreement:

    seq 22   kind=fill   contract_key=SQQQ|CALL|43|2026-05-15
    SPY|CALL|712|2026-04-21   status  live=CLOSED   replayed=OPEN
    SPY|CALL|712|2026-04-21   quantity live=0        replayed=1

Read precisely: the replay's book still holds one contract of a position the live book closed,
and the counts of missing/extra positions at that mark are both empty. So the exit FILL was
applied live and **skipped on replay** - it raised `SettlementError: no position` and the
collector swallowed it, leaving the entry fill's quantity stranded.

That kills the sweep hypothesis and the day-roll hypothesis: the market day agrees at seq 22
(2026-05-13), and nothing is missing or extra at that point. The remaining explanation is that
`settle_fill` only ADJUSTS a position that `reduce_book` created, so an orphaned fill means the
position's key on replay is not the key the fill names. Since the key cross-check over all 107
rows returns zero mismatches, the difference must be in the *generation* the fill carries, not
the key.

Next command, exactly: print applied rows 15-25 with `kind`, `contract_key`, the fill payload's
`generation`/`quantity_delta`/`final`, and the generation of the position the replayed book
holds at that seq. That distinguishes "fill names generation 2 against a generation-1 position"
from "the entry fill never ran". One query, and the fix follows from the answer.

## Where the origin is, and the test that names it

The origin is the *first* `SettlementError`, and there are exactly two candidate mechanisms:

* a fill arrives for a contract whose position the sweep (`expiry.expire_on_roll`) released in a
  later-day roll, because the replay applies fills and signals in log order while the live path
  interleaves them with settlement hooks; or
* a fill's `generation` does not match the position's generation, because a re-entry increments
  it and the envelope captured the earlier one.

The discriminating test is a **lockstep diff**, and every piece of it already exists:

```python
# drive the real pipeline, snapshotting after every applied row
states = []
for step in scripted_operations:          # handle() / settle() in corpus order
    states.append((len(store.load_applied()), pipeline.state))

# then find the first index where the replay disagrees with what the pipeline held
for k, expected in states:
    replayed = replay_applied(store.load_applied()[:k])
    if replayed != expected:
        return k        # <- this row, and the field that differs
```

The first `k` names the exact row. That is the fix's starting point, and it is roughly 30 lines
because `apply_applied`, `replay_applied` and the divergence differ already exist.

## The invariant this defect is protecting

The reason this matters more than any feature: `verify` is the only mechanism that can tell an
operator the book has drifted from the decisions that produced it. A control plane that reports
`verified: false` is honest but not trustworthy; one that reports `verified: true` while the
replay is silently skipping rows would be worse than having no verifier at all.

Nothing in this branch should be described as production-ready until a 107-trade corpus run
returns `ok: true` with **zero** divergences and the lockstep diff is a permanent test.
