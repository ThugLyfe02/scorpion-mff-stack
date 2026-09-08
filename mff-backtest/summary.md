# MoneyForFun Options KING backtest scrape

Scraped and appended only verified, non-duplicate trades from the requested channels. `lotto-options` was skipped. Current coverage runs from **2026-03-10 through 2026-09-04**.

## Captured rows
- high-confidence-options: 10
- options-king: 42
- etf-options: 34
- platinum-options: 7
- lotto-friday: 14
- **Total: 107**

## Outcome counts
- win: 27
- win_partial: 63
- loss: 8
- scratch: 3
- unknown: 6

## Backtest-depth notes
- Added 23 new ETF-options rows, including pre-May history and rapid exits/scratches.
- Preserved visible stops, breakeven closures, losses/scratches, runner percentages, and exit latency where shown.
- Earliest newly captured date is 2026-03-10; `today` expiries remain as displayed by Discord.
- No Discord messages were sent and no upgrade/payment controls were used.
