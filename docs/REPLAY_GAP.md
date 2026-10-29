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

## ROOT CAUSE: the expiry sweep is a side effect outside the event stream

Rows 19-23, verbatim:

    19 signal  SPY|CALL|712|2026-04-21   kind ENTRY
    20 fill    SPY|CALL|712|2026-04-21   gen 1  dq +1  final False
    21 signal  SQQQ|CALL|43|2026-05-15   kind ENTRY
    22 fill    SQQQ|CALL|43|2026-05-15   gen 1  dq +1  final False

There is **no exit fill for SPY anywhere in the log** - not a mismatched generation, not a
missing row, none at all. Yet the live book holds that position CLOSED with quantity 0.

The only code that can close a position without a settlement is the expiry sweep
(`expiry.expire_on_roll`), which runs inside the reduction when the market day advances: at row
21 the day rolls 2026-04-21 -> 2026-05-15, SPY's expiry is behind it, and the sweep releases the
position. That release changes the book and **appends nothing to `applied_events`**, so the
replay - which only folds the applied log - never sees it.

Every downstream symptom follows from this one property:

    a position closed outside the log
        -> replay keeps holding it (qty 1, OPEN)
        -> its quantity blocks the next day roll
        -> market_day stops advancing for the replay (2026-08-12 vs 2026-09-03)
        -> later signals apply against a stale book
        -> 29 positions missing, 10 orphaned fills, 47 divergences

The verifier was right, and it was right for the right reason: **the book contained state its
own log could not explain.** That is precisely the drift it exists to detect.

## The fix, and why it is the right one

The sweep must become part of the event stream rather than a side effect of the reducer:

* `reduce_book` (or the pipeline, immediately after it) returns the expired keys, and the
  pipeline appends one `kind="expire"` row per released position, carrying key, generation and
  the day that released it;
* `apply_applied` gains an `expire` branch that marks the position CLOSED without a fill, so a
  replay reproduces the release exactly;
* the sweep becomes **deterministic on (day, expiry)** rather than on ambient time, which is
  also what makes it testable.

Then the permanent test is the lockstep diff at every mark, not just the final state - 173 marks
in the corpus, each one compared. A system that only checks the ending cannot see a book that
took a different path to it.

## The wider lesson, worth more than the fix

This defect is invisible to every unit test that exercises the reducer alone, because the
reducer is self-consistent: it sweeps, and the state it returns is correct. The hole only shows
up when a *second* consumer rebuilds the book from the log and disagrees. That is the argument
for keeping `verify` in the hot path rather than as a debugging aid: it is the only component
that can tell you the system's story about itself is incomplete.

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
