# MoneyForFun 2-week option charts (5-minute RTH)

Robinhood `interval=minute` option historicals return flat interpolated bars; charts use **5-minute** `bounds=24_5` non-interpolated RTH bars.

OK: 22 / 25

| Date | Ticker | Side | Strike | Outcome | Entry ET | Fill | Exit | PNG | Status |
|---|---|---|---|---|---|---:|---|---|---|
| 2026-08-24 | NVDA | call | 225 | loss | None | None |  |  | error |
| 2026-08-24 | QQQ | call | 706 | win | 2026-08-24 16:10 | 1.05 | miss_no_bars | png/2026-08-24_QQQ_call_706.png | ok |
| 2026-08-25 | QQQ | call | 714 | win_partial | 2026-08-25 16:10 | 1.25 | miss_no_bars | png/2026-08-25_QQQ_call_714.png | ok |
| 2026-08-25 | NKE | call | 42.5 | unknown | None | None |  |  | error |
| 2026-08-25 | QQQ | call | 711 | win_partial | 2026-08-25 16:10 | 0.73 | miss_no_bars | png/2026-08-25_QQQ_call_711.png | ok |
| 2026-08-25 | SPX | call | 7670 | win | 2026-08-25 15:55 | 1.8 | miss_no_bars | png/2026-08-25_SPX_call_7670.png | ok |
| 2026-08-26 | TSLA | call | 360 | win_partial | None | None |  |  | error |
| 2026-08-26 | QQQ | call | 712 | win_partial | 2026-08-26 16:10 | 1.39 | miss_no_bars | png/2026-08-26_QQQ_call_712.png | ok |
| 2026-08-27 | QQQ | call | 720 | win_partial | 2026-08-27 10:15 | 0.9 | +10% @ 2026-08-27 10:20 | png/2026-08-27_QQQ_call_720.png | ok |
| 2026-08-27 | NVDA | call | 230 | win_partial | 2026-08-27 09:50 | 1.4 | +17% @ 2026-08-27 10:00 | png/2026-08-27_NVDA_call_230.png | ok |
| 2026-08-27 | TSLA | call | 360 | win_partial | 2026-08-27 10:20 | 2.56 | +29% @ 2026-08-27 10:35 | png/2026-08-27_TSLA_call_360.png | ok |
| 2026-08-28 | QQQ | call | 720 | win | 2026-08-28 10:15 | 1.56 | +150% @ 2026-08-28 10:55 | png/2026-08-28_QQQ_call_720.png | ok |
| 2026-08-28 | NVDA | call | 225 | win | 2026-08-28 10:25 | 1.22 | TARGET miss 100.0 | png/2026-08-28_NVDA_call_225.png | ok |
| 2026-08-31 | TSLA | call | 360 | win_partial | 2026-08-31 09:30 | 4.13 | +50% @ 2026-08-31 09:45 | png/2026-08-31_TSLA_call_360.png | ok |
| 2026-08-31 | TSLL | call | 10 | win_partial | 2026-08-31 09:35 | 0.75 | +22% @ 2026-08-31 09:50 | png/2026-08-31_TSLL_call_10.png | ok |
| 2026-08-31 | NVDA | put | 210 | win_partial | 2026-08-31 09:40 | 2.0 | TARGET miss 30.0 | png/2026-08-31_NVDA_put_210.png | ok |
| 2026-08-31 | TSLA | call | 360 | win | 2026-08-31 09:45 | 2.6 | +100% @ 2026-08-31 10:25 | png/2026-08-31_TSLA_call_360_platinum-options.png | ok |
| 2026-09-01 | QQQ | put | 710 | win_partial | 2026-09-01 10:40 | 1.17 | +20% @ 2026-09-01 11:00 | png/2026-09-01_QQQ_put_710.png | ok |
| 2026-09-02 | TSLA | call | 355 | win | 2026-09-02 09:40 | 1.27 | +100% @ 2026-09-02 10:40 | png/2026-09-02_TSLA_call_355.png | ok |
| 2026-09-02 | QQQ | put | 705 | win_partial | 2026-09-02 09:30 | 1.22 | TARGET miss 50.0 | png/2026-09-02_QQQ_put_705.png | ok |
| 2026-09-02 | GOOGL | call | 340 | win | 2026-09-02 09:50 | 3.05 | +100% @ 2026-09-03 09:35 | png/2026-09-02_GOOGL_call_340.png | ok |
| 2026-09-03 | QQQ | call | 712 | win | 2026-09-03 09:30 | 1.27 | +400% @ 2026-09-03 14:00 | png/2026-09-03_QQQ_call_712.png | ok |
| 2026-09-03 | TSLA | call | 390 | win | 2026-09-03 09:40 | 1.9 | TARGET miss 150.0 | png/2026-09-03_TSLA_call_390.png | ok |
| 2026-09-04 | TSLA | call | 362.5 | unknown | 2026-09-04 09:35 | 2.15 | no Discord exit % | png/2026-09-04_TSLA_call_362p5.png | ok |
| 2026-09-04 | TSLA | call | 355 | unknown | 2026-09-04 11:05 | 1.53 | no Discord exit % | png/2026-09-04_TSLA_call_355.png | ok |
