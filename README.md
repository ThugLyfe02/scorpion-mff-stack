# Scorpion MFF Stack

Trading stack for **Scorpion Capital**: MoneyForFun Discord options hone → Robinhood Agentic, plus Ross-style gapper research desk.

## Status (2026-09-08)

| Layer | State |
|---|---|
| Live Discord → RH executor | **LLM agent + browser** (failed open: queue starvation). **Needs coded hot path.** |
| Backtest / signal corpus | In `mff-backtest/` (tape BT + scrapes) |
| Gapper scanner | Agent cron + Ross skill — **no scanner binary** |

## Repo layout

```
mff-backtest/          # tape backtest, channel IDs, trades.json, bars
skills/                # Ross gapper skill
config/                # agent profiles + cron routine snapshots (prompts)
docs/HANDOFF.md        # full architecture + 9/8 failure + build target
```

## Primary files

1. `docs/HANDOFF.md` — start here
2. `mff-backtest/bt_full/discord_channel_ids.json`
3. `mff-backtest/bt_full/run_tape_bt.py`
4. `mff-backtest/bt_full/trades.json`
5. `mff-backtest/bt_full/sep8_alerts.json`

## Build target (hot path)

```
Discord Gateway MESSAGE_CREATE (4 channel IDs)
  → parse Entry|Exit|Ignore
  → state machine (1-lot pipe → size; max 2; +25% skip; no chase closed)
  → Robinhood Agentic order adapter
  → structured fill log + kill switch
```

Keep LLM agents for gapper desk / ops / STOP — not sub-10s exits.

## Secrets

Do **not** commit Robinhood tokens, Discord bot tokens, or `.env`. Channel IDs are included (guild membership still required).

## License

Private. Collaborators only.
