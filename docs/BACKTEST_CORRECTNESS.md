# Backtest correctness reset

The legacy `mff-backtest/bt_full/run_tape_bt.py` should be treated as research history, not as a
production sizing oracle.

## Known mismatches / defects addressed by v2

### 1. Live add rule vs legacy add rule

Locked live policy says add only on an explicit source `added/avg` message. The legacy tape
simulation auto-adds whenever a 5-minute low reaches approximately -15%.

V2 replay must consume actual source follow-up events. It never synthesizes an add from price alone.

### 2. Entry-bar OHLC ordering

Five-minute OHLC cannot prove whether a bar's high/low occurred before or after an inferred entry.
Legacy same-bar target/add logic can therefore introduce look-ahead.

`research/corrected_tape.py::target_bounds_on_entry_bar` exposes optimistic/conservative bounds.
Conservative replay does not credit an entry-bar target merely because the high/low printed it.

### 3. Partial exit then add cost basis

Selling a portion of a long options lot leaves the remaining contracts' per-contract average cost
unchanged. V2 lot accounting explicitly preserves the average on reductions before later additions.

### 4. Max concurrency

A position consumes a slot only while `exit_ts > candidate_entry_ts`.
Checking against a generic 09:30 day boundary is incorrect.

### 5. Time semantics

Fields derived from Discord UI screenshots must not be named/treated as ET unless actually converted.
New replay corpora should preserve source API UTC timestamps and optionally retain display time only
as metadata.

### 6. Aggregated `exit_pct`

A final reported percentage is not an executable event stream. V2 historical evaluation should use:
entry -> add -> trim -> runner/stop -> exit messages at their actual timestamps.

## Migration rule

Keep legacy outputs for forensic comparison, but prefix future reports with one of:

- `legacy_upper_bound`
- `legacy_5m_ohlc`
- `event_replay_conservative`
- `event_replay_optimistic`

Do not mix them in one performance headline.
