# Live observation layer

This folder is for **shared, drift-resistant** state your coding collaborator and the desk both read/write.

| Path | Purpose |
|---|---|
| `fills/` | Structured fill logs (post_ts, submit_ts, fill, slip, P&L) |
| `misses/` | Missed alerts + why (late arm, >+25%, stand-down, parse fail) |
| `watch-health/` | Discord tab / Gateway health pings |
| `decisions.md` | Append-only decisions that change rules (date, who, change) |
| `RULES.lock.md` | Canonical locked rules — edit only via PR, never ad-hoc chat drift |

Bot/agent should write here; humans review in PRs.
