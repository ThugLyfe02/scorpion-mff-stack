# Scorpion MFF Stack

Trading stack for **Scorpion Capital**: MoneyForFun Discord options hone → Robinhood Agentic, plus Ross-style gapper research desk.

## Status (2026-10-22)

| Layer | State |
|---|---|
| Live Discord → RH executor | **Coded hot path built** — `scorpion_mff/` (Gateway → parser → state machine → RH adapter → fill log, kill switch, latency budget). **319 offline tests green.** See **`HOTPATH.md`**. Live wiring pending: RH endpoint confirmation, quote feed, Gateway token + MESSAGE_CONTENT intent. |
| Executor backtest | **Run over every alert on record** — 93 alerts / 59 days / 4 channels. Two live-path defects found and fixed (a carried position held a concurrency slot forever; an exit could not be attributed when two positions were open). See **`BACKTEST.md`**. |
| Ops watchdog | **Built and proven** — `watch --once` reads the heartbeat + fill log and pages the phone (same bot as the desk digest) only when the hone is down, stale, or has raised an engine alert. Delivered live (message id 21). See **`HOTPATH.md`**. |
| Risk layer | **Built** — a versioned, **reduce-only** risk policy (`live/policy.json`), a governor that turns it into a per-entry multiplier the engine cannot exceed, and a statistics layer (expectancy with confidence intervals, shrinkage, drawdown, runs test, channel allocation). Risk state is persisted so a restart cannot forget a drawdown; `arm` refuses without a valid, fresh, non-halted policy. See **`POLICY.md`**. |
| Pre-trade risk budget | **Built** — `scorpion_mff/risk.py` prices the loss before the order: worst-case day = realised − open premium − new premium, plus total/underlying/cluster premium caps and a declared correlation map. Entries and adds are both governed; a corrupt `live/risk-budget.json` refuses to arm. Also fixes four hot-path defects (expired-key tombstone, adds bypassing STOP and the governor, add tier falling back to the ticker). See **`RISK.md`**. |
| Evidence engine | **Built** — bootstrap BCa intervals on mean P&L, a Bayesian-bootstrap posterior, empirical-Bayes channel shrinkage, Benjamini–Hochberg selection control, a permutation change-point test, power, and a hard source gate: live fills may produce an allocation, the historical corpus never can (it is labelled `CALIBRATION_ONLY`). See **`RISK.md`** and `evidence report`. |
| Execution quality (TCA) | **Built** — `tca path` measures the contract's real path on the repo's bars and **withholds aggregates it cannot support** (only 2 of 18 matched trades have both entry and exit inside coverage); it does price the *carry*: median −12.9% at the 09:30 open, 17% of contracts expire at ~zero. `tca fills` measures limit vs realised fill in bps, partials and latency, and suggests a calibrated `ask_multiple` — empty until the first live fill. See **`TCA.md`**. |
| Adversarial hardening | **Built, and it found two real bugs** — a stochastic generator throws channel-shaped posts at the real parser and engine and checks ten invariants (no sale without a holding, no price from a percentage, no oversize book, kill switch blocks buys not exits, parser never raises...). 18,000 posts clean across six seeds, deterministic, `fuzz` exits 1 on a violation so CI can gate. Findings: a named exit for an unheld contract could flatten a different held position, and the kill-switch behaviour needed pinning. See **`HOTPATH.md`**. |
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
POLICY.md              # the reduce-only risk layer + what the record says
RISK.md                # pre-trade worst-case budget + the evidence engine
TCA.md                 # execution quality: what the bars proved, what fills will prove
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

Run it: `python -m scorpion_mff.cli doctor | replay tapes/sep8.json | backtest | desk |
         evidence report | tca path | tca fills | policy show | risk-budget show |
         watch --once | fuzz | arm | status`.
Details, including what is still unproven and why: **`HOTPATH.md`**.
What the executor does with 93 real alerts, and the two defects that found: **`BACKTEST.md`**.
The risk policy, the governor, and the expectancy/allocation statistics: **`POLICY.md`**.
The pre-trade worst-case budget, the four lifecycle fixes, and the evidence engine:
**`RISK.md`**.
What our fills cost and what the bars did (and did not) prove: **`TCA.md`**.

## Secrets

Do **not** commit Robinhood tokens, Discord bot tokens, or `.env`. Channel IDs are included (guild membership still required).

## License

Private. Collaborators only.
