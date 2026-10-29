# Live observation layer

This folder is for **shared, drift-resistant** state your coding collaborator and the desk both read/write.

| Path | Purpose |
|---|---|
| `fills/` | Structured fill logs (post_ts, submit_ts, fill, slip, P&L) |
| `misses/` | Missed alerts + why (late arm, >+25%, stand-down, parse fail) |
| `watch-health/` | Discord tab / Gateway health pings |
| `decisions.md` | Append-only decisions that change rules (date, who, change) |
| `RULES.lock.md` | Canonical locked rules — edit only via PR, never ad-hoc chat drift |
| `risk-budget.example.json` | Reviewed starter for the pre-trade worst-case budget (`risk init`) |
| `venue-snapshot.example.json` | Shape of the venue book for `reconcile --snapshot`: `[{contract_key, contracts}]` |
| `execution.jsonl` | **Machine state, never committed.** The confirmed-execution event log (`scorpion_mff/execution_book.py`): intent, ack, fill, reject, unknown, expire, baseline. Replayed by `book show / replay / verify / sync` |
| `venue-snapshot.json` | **Machine state, never committed.** The venue's positions, written by the adapter and read by the scheduled reconcile (`arm --venue-snapshot`, or `MFF_VENUE_URL` for the HTTP feed). An undatable or stale file is refused, never treated as a flat book |
| `executions.jsonl` | **Machine state, never committed.** One `tca.Execution` per filled order, written by `book export`; the input to `tca fills` |

Bot/agent should write here; humans review in PRs.
