# Architecture + failure brief (Tue 2026-09-08)

## Goal
Mirror MoneyForFun Discord options → Robinhood **Agentic** (`••••0448`) in seconds. Gapper desk is research-only.

## Agents
- **Trader** — live MFF hone executor (orders)
- **Scanner** — Ross premarket/RTH boards (no orders)
- **Chief of Staff** — desk alerts + STOP/ops

## Discord (included channels)
Guild `912747256736800838`. See `mff-backtest/bt_full/discord_channel_ids.json`.
Skip: lotto-options, lotto-friday, NKE, Fridays for live hone.

## Locked live rules
```
ENTRY: min(ask, his_bid*1.15); skip if >+25% off his bid
SIZE:  first trade/day = 1 contract pipe test
       then king/etf/platinum $1500 (add $2250); high-confidence $3000 (add $4500)
MAX:   2 concurrent; add only if he posts added/avg
EXIT:  Discord close/trim/%/sold runners → trim-to-1+BE or flatten
GATE:  none (no CoS permission); STOP/stand-down only
```

## What runs today (broken for latency)
Cron wakes + browser Discord tabs. **No Gateway webhook.** Single agent turn queue serializes chat + routines.

Post-9/8 cron mitigations (still agent-shaped): 8:50 tabs-only, 9:20 arm Discord-first, */5 deadman <30s, 4am equity dump.

## 9/8 measured failure
- Arm cron was 9:25; wake started **9:56:26** (~31m queue lag). Work once awake ~2m.
- Deadman 9:08 ran **579s** and starved queue; only 2/15 */5 slots fired AM.
- Missed: QQQ 719C 0DTE (+10% @9:56) and NVDA 227.5P Sep11 (~+19% by ~10:05).
- Counterfactual ~$295–315; fills $0.

## Code present
`mff-backtest/` — offline scrape + `run_tape_bt.py` (Discord % = target; P&L from RH 5m tape). No live bot.

## Code missing (build this)
Discord Gateway listener, deterministic parser, state machine off chat queue, RH adapter, fill log, healthcheck, kill switch.
