# MoneyForFun trades_seed.csv — seed notes

**Generated:** 2026-09-06 (ET)  
**Assets scanned:** `/home/box/agent-data/agents/03ab987e-1a5a-44fc-a854-9f58cf738f15/assets/`  
**Image inventory:** ~420 webp/png modified in last 2 days (mostly Discord scrape screenshots).

## Counts

| Metric | Value |
|--------|------:|
| Seed trade rows | **35** |
| outcome=`unknown` (entry-only / no clear exit) | 19 |
| outcome=`closed` (exit update visible) | 16 |
| Channels included | options-king, high-confidence-options, etf-options, platinum-options, lotto-friday |
| Channels **excluded** | **lotto-options** (per steering); also non-alert UI (login, imposters, friends, other servers) |

### By channel (approx.)
- platinum-options: majority (historical scroll + recent)
- options-king: 4
- high-confidence-options: 2
- etf-options: 1
- lotto-friday: 3

## Method

1. Listed recent png/webp under agent assets.
2. Vision descriptions via Read tool on key screenshots (channel browser + platinum scrolls).
3. **EasyOCR 1.7.2** (venv) on ~23 alert screenshots — `tesseract-ocr` apt package unavailable in this environment.
4. Deduped overlapping screenshots of the same alert thread into one row per contract/alert where possible.
5. Did **not** invent outcomes: entry-only → `outcome=unknown`. Exit/close/% messages → `outcome=closed` with visible `exit_price` / `exit_pct` when present.

## Quality caveats

- **Coverage incomplete:** ~429 webps; only a sampled subset OCR’d/Read. Parallel computerUse scrape should fill gaps. Many frames are UI chrome, channel-browser, or duplicate scrolls.
- **Truncation:** Discord embed headers often cut expiry (`Exp T...`, `Exp ...`). Strikes occasionally truncated (e.g. lotto-friday TSLA `$3...`).
- **Decimal / OCR ambiguity:** EasyOCR sometimes drops decimals. Examples recorded as-written with notes:
  - AAPL BID `$263` (likely `$2.63`)
  - DJT BID `$295` (likely `$2.95`)
  - COST BID `$310` (likely `$3.10`)
  - AMD exit OCR `$490` corrected to **`$4.90`** using vision + “200%+” context
  - TSLA strike OCR `5450` corrected to **`450`**
  - Late TSLA put exit text OCR `$200 to $2100` — **low confidence** absolute prices
- **Partial fills / runners:** Some rows collapse multi-message threads (trim half, runners, close remaining) into one seed row; see `notes`.
- **% without exit price:** Several closes only state `20%` / `35%` / `75%` etc.; `exit_price` left blank rather than computed.
- **Timestamps:** Discord local times treated as **ET** per column name; not independently verified.
- **Year formats:** Screenshots mix `24`/`25`/`26` two-digit years → normalized to 20xx.
- **Lotto language in allowed channels:** Alerts labeled “0DTE LOTTO” / “ER LOTTO” **inside** platinum-options / lotto-friday were kept. Only the **`lotto-options` channel** was skipped.
- **No PnL invention:** `outcome` is not win/loss labeled unless an explicit close/update exists; many profitable-sounding % updates still leave absolute exit blank.

## Files

- `/workspace/mff-backtest/trades_seed.csv`
- `/workspace/mff-backtest/seed_notes.md` (this file)
- `/workspace/mff-backtest/ocr_raw/*.txt` (raw EasyOCR dumps for audited images)
