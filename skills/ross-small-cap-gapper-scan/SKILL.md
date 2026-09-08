---
name: Ross small-cap gapper scan
description: >-
  use this when scanning US premarket or RTH small/micro-cap gappers with
  Ross-style boxes (price, rvol, float, news, 10% gap)
---
# Ross-style small-cap gapper scan

Use this when scanning US premarket or RTH for small/micro-cap momentum (not mega-cap earnings).

## Five boxes
1. Price **$2–$20**
2. Relative volume **≥5x** vs the stock's average daily volume (PM volume counts toward today vs ADV)
3. Float **≤20 million** shares (prefer ≤10M; sub-5M is rocket fuel — still flag the risk)
4. News catalyst (earnings, FDA, contract, offering, 13G, etc.)
5. Gap vs prior close **or** day change **≥10%**

**Pass = 4 of 5.** News is the skippable box: no-news China ADRs and sector ramps still make the board if the other four hit. Label catalyst `none found` when that's the miss.

## Always report
Ticker, last, gap % vs prior close, today's volume (note if premarket-only), relative volume vs ADV, float, boxes hit (e.g. 4/5, missed news), one-line catalyst or `none found`, GO vs FADE vs MIXED with one tape reason.

## Skip
- Buyouts / M&A at a fixed deal price
- OTC / sub-penny if you cannot verify volume
- Names that miss 2+ boxes

## Honesty
Never invent price, volume, float, or news. If float or rvol cannot be verified, say so and do not check that box. Research and tape only — no orders, no P&L.
