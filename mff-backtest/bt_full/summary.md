# MFF Discord options — honest tape backtest

**Book** start $5,000 · **1R** = $500 · base premium $2,000/clip · optional −15% add $1,000 once (cap $3,000).

## Method (authoritative)

- Discord `exit_pct` is a **target label only**. P&L comes from RH 5m RTH tape after our fill.
- Entry: first trade-date RTH bar with price ≈ alert bid (±5%, fallback ±10% / bid in bar range). Fill = bid if in range else close.
- Add once only if a later bar’s **low ≤ entry_fill×0.85**; skipped if a target already filled that same bar.
- Scale-outs only if highs/lows **print** Discord targets after entry; else flatten last bar / 0DTE 15:45 ET (can be a **loss**).
- No mechanical −25% stop in primary A/B (variant C compares hard −25%).

## Coverage (critical)

- Signals in `trades.json`: **93** (Mar 10 – Sep 3, 2026)
- Instrument IDs resolved: **90/90** unique contracts
- Skip breakdown (add15_multi): `{'no_bars': 39, 'skip_no_bars_on_trade_date': 35, 'skip_no_fill_match': 6}`
- **Tape-taken**: **13** only — mostly late Aug / early Sep where RH still has real 5m bars.
- Older Mar–Jul: RH often returns only interpolated flat bars → **excluded** (not assumed winners).

## Fantasy vs tape

- Naive Discord-% upper bound (Σ $2000×exit_pct/100): **$191,860** across 89 labeled exits — **not tradable**.
- Tape add15_multi net: **$12,210.16** on 13 fills.
- Gap (fantasy − tape): **$179,649.44** — do not confuse Discord callouts with account P&L.

## A) add15_multi — −15% add ON, all concurrent as posted

| Metric | Value |
|---|---|
| Taken / skipped | 13 / 80 |
| End book | $17,210.16 |
| Total P&L | **$12,210.16** |
| Max DD | $2,785.30 |
| Wins / losses / scratch | 9 / 3 / 1 |
| Win rate | 0.6923 |
| Avg R | 1.8785 |
| Best / worst | $8067.45 / $-2785.3 |
| Monthly | `{'2026-08': 3091.31, '2026-09': 9118.85}` |

**Losing trades (honest):**
- 2026-08-31 NVDA put 210: **$-2460.0** · eod_last_bar_flag · fill=2.0 · discord_tgt=30.0% · add=True
- 2026-09-02 QQQ put 705: **$-2785.3** · 0dte_1545 · fill=1.22 · discord_tgt=50.0% · add=True
- 2026-09-03 TSLA call 390: **$-488.0** · eod_last_bar_flag · fill=1.9 · discord_tgt=150.0% · add=True

## B) no_add_multi — no average-down, all concurrent

| Metric | Value |
|---|---|
| Taken / skipped | 13 / 80 |
| End book | $11,017.50 |
| Total P&L | **$6,017.50** |
| Max DD | $1,888.00 |
| Wins / losses / scratch | 9 / 3 / 1 |
| Win rate | 0.6923 |
| Avg R | 0.9258 |
| Best / worst | $4205.0 / $-1888.0 |
| Monthly | `{'2026-08': 1044.0, '2026-09': 4973.5}` |

**Losing trades (honest):**
- 2026-08-31 NVDA put 210: **$-1740.0** · eod_last_bar_flag · fill=2.0 · discord_tgt=30.0% · add=False
- 2026-09-02 QQQ put 705: **$-1888.0** · 0dte_1545 · fill=1.22 · discord_tgt=50.0% · add=False
- 2026-09-03 TSLA call 390: **$-470.0** · eod_last_bar_flag · fill=1.9 · discord_tgt=150.0% · add=False

## A′) add15_max2 — −15% add, max 2 concurrent

| Metric | Value |
|---|---|
| Taken / skipped | 8 / 85 |
| End book | $13,620.10 |
| Total P&L | **$8,620.10** |
| Max DD | $2,460.00 |
| Wins / losses / scratch | 6 / 1 / 1 |
| Win rate | 0.75 |
| Avg R | 2.155 |
| Best / worst | $8067.45 / $-2460.0 |
| Monthly | `{'2026-08': -1499.8, '2026-09': 10119.9}` |

**Losing trades (honest):**
- 2026-08-31 NVDA put 210: **$-2460.0** · eod_last_bar_flag · fill=2.0 · discord_tgt=30.0% · add=True

## B′) no_add_max2 — no add, max 2 concurrent

| Metric | Value |
|---|---|
| Taken / skipped | 8 / 85 |
| End book | $10,179.20 |
| Total P&L | **$5,179.20** |
| Max DD | $1,740.00 |
| Wins / losses / scratch | 6 / 1 / 1 |
| Win rate | 0.75 |
| Avg R | 1.2948 |
| Best / worst | $4205.0 / $-1740.0 |
| Monthly | `{'2026-08': -779.8, '2026-09': 5959.0}` |

**Losing trades (honest):**
- 2026-08-31 NVDA put 210: **$-1740.0** · eod_last_bar_flag · fill=2.0 · discord_tgt=30.0% · add=False

## C) hard −25% stop + Discord targets + no add (multi)

| Metric | Value |
|---|---|
| Taken / skipped | 13 / 80 |
| End book | $4,433.20 |
| Total P&L | **$-566.80** |
| Max DD | $1,142.80 |
| Wins / losses / scratch | 5 / 7 / 1 |
| Win rate | 0.3846 |
| Avg R | -0.0872 |
| Best / worst | $1372.5 / $-500.0 |
| Monthly | `{'2026-08': -1142.8, '2026-09': 576.0}` |

**Losing trades (honest):**
- 2026-08-27 QQQ call 720: **$-495.0** · hard_-25 · fill=0.9 · discord_tgt=10.0% · add=False
- 2026-08-27 TSLA call 360: **$-455.0** · hard_-25 · fill=2.6 · discord_tgt=28.78% · add=False
- 2026-08-31 NVDA put 210: **$-500.0** · hard_-25 · fill=2.0 · discord_tgt=30.0% · add=False
- 2026-08-31 TSLA call 360: **$-455.0** · hard_-25 · fill=2.6 · discord_tgt=100.0% · add=False
- 2026-09-02 QQQ put 705: **$-488.0** · hard_-25 · fill=1.22 · discord_tgt=50.0% · add=False
- 2026-09-03 QQQ call 712: **$-476.25** · hard_-25 · fill=1.27 · discord_tgt=400.0% · add=False
- 2026-09-03 TSLA call 390: **$-475.0** · hard_-25 · fill=1.9 · discord_tgt=150.0% · add=False

## Comparison (C)

Hard −25% stop + his targets + no add ends at **$4,433.20** (P&L **$-566.80**, maxDD $1,142.80) vs add15_multi **$12,210.16** and no_add_multi **$6,017.50**. Stops/targets still require tape prints — this variant cut several runners and flipped more trades to losses.

## Output files (absolute paths)

- `/workspace/mff-backtest/bt_full/equity_add15.csv`
- `/workspace/mff-backtest/bt_full/equity_no_add.csv`
- `/workspace/mff-backtest/bt_full/equity_add15_multi.csv`
- `/workspace/mff-backtest/bt_full/equity_no_add_multi.csv`
- `/workspace/mff-backtest/bt_full/equity_add15_max2.csv`
- `/workspace/mff-backtest/bt_full/equity_no_add_max2.csv`
- `/workspace/mff-backtest/bt_full/trades_sim_add15.csv`
- `/workspace/mff-backtest/bt_full/trades_sim_no_add.csv`
- `/workspace/mff-backtest/bt_full/trades_sim_add15_multi.csv`
- `/workspace/mff-backtest/bt_full/trades_sim_no_add_multi.csv`
- `/workspace/mff-backtest/bt_full/trades_sim_add15_max2.csv`
- `/workspace/mff-backtest/bt_full/trades_sim_no_add_max2.csv`
- `/workspace/mff-backtest/bt_full/summary.json`
- `/workspace/mff-backtest/bt_full/summary.md`

## Caveats

- Sample is small (13 tape fills). One QQQ 9/3 clip dominates add15 P&L (~+$8.0k) because tape printed +50% then +400% after a later −15% add.
- Large losses also real: e.g. NVDA 8/31 −$2,460 (discord +30% never printed → last-bar flatten after add); QQQ 9/2 −$2,785 (0DTE 15:45 flatten after add; discord +50% missed).
- Contracts that failed to resolve usable trade-date bars: see skip counts above (~74 of 93).
