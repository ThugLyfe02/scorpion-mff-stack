# week_bars meta

Robinhood MCP: `get_option_instruments` (state=`expired` for past 0DTE; GOOGL active) → `get_option_historicals` (interval=`5minute`, bounds=`24_5`).
Stored bars = RTH only (`session=reg`, `interpolated=false`) on each trade date (~13:30Z–19:55Z).

| trade_key | instrument_id | state | RTH bars | notes |
|---|---|---|---:|---|
| `QQQ_put_710_2026-09-01` | `ca5d39f3-aa9a-4563-bcae-cc75825fa564` | expired | 81 | QQQ put 710 exp 2026-09-01; OCC `QQQ   260901P00710000` |
| `TSLA_call_355_2026-09-02` | `3f98187d-4560-42d0-b5c5-122c2d5e1613` | expired | 78 | TSLA call 355 exp 2026-09-02; OCC `TSLA  260902C00355000` |
| `QQQ_put_705_2026-09-02` | `f64b55b8-ebf2-4492-a212-f2a5470bd179` | expired | 81 | QQQ put 705 exp 2026-09-02; OCC `QQQ   260902P00705000` |
| `GOOGL_call_340_2026-09-09` | `065b2142-27c3-4726-8192-090371ac1b30` | active | 156 | GOOGL call 340 exp 2026-09-09 (bars Sep 2–3); OCC `GOOGL 260909C00340000` |
| `QQQ_call_712_2026-09-03` | `f6740c29-86e6-4e95-9bb1-cce0a84d89e7` | expired | 78 | QQQ call 712 exp 2026-09-03; OCC `QQQ   260903C00712000` |
| `TSLA_call_390_2026-09-04` | `dc18d5ae-32b9-4439-8902-e2c542320469` | expired | 156 | TSLA call 390 exp 2026-09-04 (bars Sep 3–4); OCC `TSLA  260904C00390000` |

## Skipped / optional

| trade_key | instrument_id | notes |
|---|---|---|
| `TSLA_call_360_2026-09-04` | `39eddb1f-5486-4785-b39c-a193dbde28b6` | Id resolved (expired); **bars skipped** per Aug 31 optional context |

## Missing ids

None — all 7 contracts resolved.

## Success check (Sep 1–3 priority)

- `QQQ_put_710_2026-09-01`: 81 real RTH bars — **OK**
- `TSLA_call_355_2026-09-02`: 78 real RTH bars — **OK**
- `QQQ_put_705_2026-09-02`: 81 real RTH bars — **OK**
- `QQQ_call_712_2026-09-03`: 78 real RTH bars — **OK**

Overall priority: **PASS**

