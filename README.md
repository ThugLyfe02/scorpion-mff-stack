# Scorpion MFF Stack

Trading stack for **Scorpion Capital**: MoneyForFun Discord options hone → Robinhood Agentic, plus Ross-style gapper research desk.

## Status (2026-10-21)

| Layer | State |
|---|---|
| Live Discord → RH executor | **Coded hot path built** — `scorpion_mff/` (Gateway → parser → state machine → RH adapter → fill log, kill switch, latency budget). **146 offline tests green.** See **`HOTPATH.md`**. Live wiring pending: RH endpoint confirmation, quote feed, Gateway token + MESSAGE_CONTENT intent. |
| Executor backtest | **Run over every alert on record** — 93 alerts / 59 days / 4 channels. Two live-path defects found and fixed (a carried position held a concurrency slot forever; an exit could not be attributed when two positions were open). See **`BACKTEST.md`**. |
| Backtest / signal corpus | In `mff-backtest/` (tape BT + scrapes) |
| Gapper scanner | Agent cron + Ross skill — **no scanner binary** |

## Repo layout

```
mff-backtest/          # tape backtest, channel IDs, trades.json, bars
scorpion_mff/          # the hot path (parser, engine, pricing, sizing, broker, fills, ...)
tapes/                 # recorded replay tapes (sep8.json = the 9/8 post-mortem)
reports/               # generated artefacts (executor backtest)
skills/                # Ross gapper skill
config/                # agent profiles + cron routine snapshots (prompts)
docs/HANDOFF.md        # full architecture + 9/8 failure + build target
HOTPATH.md             # design decisions, non-changes, what is unproven
BACKTEST.md            # the executor against 93 historical alerts
```

## Primary files

1. `docs/HANDOFF.md` — start here
2. `BACKTEST.md` — what the executor does with the record
3. `mff-backtest/bt_full/trades.json`
4. `mff-backtest/bt_full/run_tape_bt.py`
5. `mff-backtest/bt_full/sep8_alerts.json`

## Build target (hot path) — BUILT

```
Discord Gateway MESSAGE_CREATE (4 channel IDs)
  → parse Entry|Exit|Ignore               scorpion_mff/parser.py
  → state machine (1-lot pipe → size; max 2; +25% skip; no chase closed)
                                          scorpion_mff/engine.py + sizing.py + pricing.py
  → Robinhood Agentic order adapter       scorpion_mff/broker.py
  → structured fill log + kill switch     scorpion_mff/fills.py + killswitch.py
```

Keep LLM agents for gapper desk / ops / STOP — not sub-10s exits.

Run it: `python -m scorpion_mff.cli doctor | replay tapes/sep8.json | backtest | arm | status`.
Details, including what is still unproven and why: **`HOTPATH.md`**.
What the executor does with 93 real alerts, and the two defects that found: **`BACKTEST.md`**.

## Secrets

Do **not** commit Robinhood tokens, Discord bot tokens, or `.env`. Channel IDs are included (guild membership still required).

## License

Private. Collaborators only.
