# Exec core v1: forward paper execution, measured

Scope: implements the execution-core spec on top of the existing `src/scorpion/` package
(no parallel package). **Paper only.** Robinhood stays read-only, and no live orders are possible
from this path: `PaperExecutionSession` refuses to start unless the broker mode is PAPER or
REVIEW_ONLY.

Frozen rules (fwd-v2-FINAL, hashed in `live/RULES.lock.md`, checked at startup):
- entry limit at the first session bar at or after max(seen, post + 2 min), at that bar's open
- fills if the low over 3 minutes is at or below L·(1 − 2 bps), otherwise one chase at that
  window's high
- skip if seen more than 15 minutes late
- exit at the open 60 minutes later (or the 15:55 bar)
- costs 10 bps; no stop, target, trail, hedge or QQQ filter (the loader rejects any other value)
- posts after 11:00 ET go to a shadow track that is logged and never traded
- the main tier is under $20

Every number below was produced on this box (Linux 6.12, Python 3.13.5). Commands are listed
so anyone can reproduce them.

## Speed: post to paper order intent

`python scripts/bench_exec_latency.py`, 2,000 posts.

| path | p50 | p99 | max |
|---|---|---|---|
| before: legacy receipt → pipeline (SQLite WAL, FULL sync) → fastpath | 3.905 ms | 30.60 ms | 114.6 ms |
| **after: post → paper intent (in-memory decision, journal off-path)** | **0.131 ms** | **0.202 ms** | 14.2 ms |
| after: post → decision only | 0.092 ms | 0.137 ms | 0.29 ms |
| after: entry timer → intent | 0.021 ms | 0.041 ms | 0.13 ms |
| journal durable ack (fsync thread), isolated | 0.28 ms | 1.01 ms | |
| journal durable ack, 2,000-post burst (queueing) | 139 ms | 260 ms | |

That is about 30× faster at p50 and 150× at p99. Durability is unchanged: every decision is
fsync'd, but on a writer thread, so a slow disk can no longer delay a decision. The 14 ms max is
a single GC/scheduler outlier.

Stale-data guard: each quote stores a monotonic receive time. `QuoteCache.fresh()` returns a
`StaleQuote` with no price once a quote is older than 3,000 ms, and the order is skipped, never
priced off old data. If the feed is silent for 10 s, or clock skew lasts 30 s, the session
latches NO_TRADE and sends one page. Entry limits are min(ask, ref·(1 + 150 bps)), rounded
down to the Reg NMS tick.

## Accuracy

**Backtest parity.** `research/exec_core_parity.py` replays the edge_study call set through
the new engine and diffs it against the frozen `fwd_logger_v2_1.py`:
- **0 cell differences across 391 rows** (discovery 187, holdout 204)
- OOS-128 set: 197/197 calls identical, mean +0.3943% (published +0.3943%)
- main track: n=156, discovery +0.447% (72), holdout +0.646% (84)
- shadow post-11:00: n=41, discovery −0.288%, holdout −0.186%, which confirms it stays untraded
- main under-$20: n=20, ≈ +3.78%
- `tests/fixtures/replay_golden.csv` is byte-identical to the frozen logger's output on the
  synthetic fixture, and preflight re-checks this before every session

**Chase sensitivity (LIMIT_THROUGH vs minute high):** 1 chase is skipped. Main means move to
+0.450% / +0.648%, so the result does not depend on the chase model.

**Parser (largest accuracy defect found).** Across 1,324 real channel callouts:

| | before (v3) | after (v4) |
|---|---|---|
| parsed as ENTRY | 0 | 1,290 |
| ticker and side correct | 0 | 1,286 |
| every field exact | 0 | 1,265 |
| 2026 callouts | 0/153 | 152/153 (the miss is an excluded ticker) |

Before this change, the structured "CALL … TICKER … STRIKE" template never reached the
decision path.

**Fill model vs real tape.** `research/fill_model_tape_check.py` pulled Databento trades for a
window around each of the 197 OOS-128 calls. Estimated cost: $0.63 total, checked before every
pull.

| check | result |
|---|---|
| limit fill (bar rule) agrees with the tape | **197 / 197** (0 false fills, 0 missed fills) |
| filled orders with ≥ $1,000 of size printed strictly below the limit | 177 / 177 |
| median time to first print through the limit | 5.4 s |
| model entry (bar open) vs first real print at the entry minute | median 0.0 bps |
| "current price" (last print before entry) vs bar open | median 0.0 bps |
| chase: model price (minute high) minus first real print after chase time | median +3.2 bps, mean +4.3 bps (n=20) |

The chase model is slightly **pessimistic**: it pays more than the tape did, so the backtest
does not overstate edge.

## Safety

- NO_TRADE latch: the sentinel file is fsync'd and checked on every decision. An unreadable
  or uninitialised state counts as latched. Only `scorpion latch clear --operator NAME` can
  clear it, and the session has no code path that clears it.
- Paper/review-only brokers; no live order path.
- Secrets come from the environment only, and log records are redacted.
  `DATABENTO_API_KEY` never appears in logs or errors.
- Every Databento pull is estimated first (`metadata.get_cost`). Pulls over $30 are refused
  unless `--approve-cost` is given (exit 3).
- CI adds a tracked-artifact guard (sqlite, cache, raw ticks, Discord exports), a public-history
  audit, and gitleaks over the PR's commits.

## Tests

- 501 pass locally on 3 consecutive runs: 482 existing plus 19 new in
  `tests/test_exec_core_v1.py`, covering the spec's 11 plus rules lock, stop rejection, parser
  template, shadow/late, alert schema, pager failure, calendar, and preflight
- ruff and mypy clean

## Not done / open

- No real broker fills exist yet, so the only modeled-vs-actual comparison is against the
  public tape. Reconciling against Robinhood order history needs the account number.
- Live Databento subscription (licence, subscribe-after-start) is unverified. The reconnect and
  gap logic is tested with a fake client only.
- No cached bars for 2026-10-30, and no channel calls that day.
- The systemd NO_TRADE drill has not been run under real systemd.
- Pre-existing tracked `mff-backtest/charts_1m/cache/` scripts are grandfathered in the hygiene
  allowlist. Discord-derived files already in this public repo are flagged in CI, not removed.
