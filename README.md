# Scorpion MFF Stack

Trading stack for **Scorpion Capital**: MoneyForFun Discord options hone → Robinhood Agentic, plus Ross-style gapper research desk.

## Status (2026-10-21)

| Layer | State |
|---|---|
| Live Discord → RH executor | **Coded hot path built** — `scorpion_mff/` (Gateway → parser → state machine → RH adapter → fill log, kill switch, latency budget). **536 offline tests green.** See **`HOTPATH.md`**. Live wiring pending: RH endpoint confirmation, quote feed, Gateway token + MESSAGE_CONTENT intent. |
| Control-plane ports (PR#1 → PR#2) | **Ported without importing PR#1** — the fail-closed ET calendar, never-silent expiry slot release, one-probe circuit breaker, fail-closed spread gate, and tri-state order ledger are live in `scorpion_mff/`. Port matrix and the lane/merge boundary: **`HOTPATH.md`**. One acceptance command per gem: **`docs/GEMS.md`**. |
| Expiry guard + audit | **Built** — three-valued `HOLD` / `FORCE_CLOSE` / `HALT_AND_PAGE`: flatten at a live bid, or page and leave the position on the book; the close is a clock (`Listener.tick`), not a post. `python -m scorpion_mff.cli expiry audit` reports 40/93 same-day, 65/93 no close, 0/40 cutoff force-close, 10/40 no exit time, and **REFUSES** assignment projection. |
| Crash recovery | **Built** — `intent` is written before the broker call; `verify` flags an intent with no terminal; `recover` resolves it from a venue lookup (`SEEN` / `NOT_SEEN` / `CANNOT_SAY`) and **never places**. A transport failure is a non-terminal `transport_unknown` quarantine (never a fake `reject`), so an unconfirmed **exit** stays visible, pages immediately, and remains recoverable. |
| Executor backtest | **Run over every alert on record** — 93 alerts / 59 days / 4 channels. Two live-path defects found and fixed (a carried position held a concurrency slot forever; an exit could not be attributed when two positions were open). See **`BACKTEST.md`**. |
| Ops watchdog | **Built and proven** — `watch --once` reads the heartbeat + fill log and pages the phone (same bot as the desk digest) only when the hone is down, stale, or has raised an engine alert. Delivered live (message id 21). See **`HOTPATH.md`**. |
| Risk layer | **Built** — a versioned, **reduce-only** risk policy (`live/policy.json`), a governor that turns it into a per-entry multiplier the engine cannot exceed, and a statistics layer (expectancy with confidence intervals, shrinkage, drawdown, runs test, channel allocation). Risk state is persisted so a restart cannot forget a drawdown; `arm` refuses without a valid, fresh, non-halted policy. See **`POLICY.md`**. |
| Concentration cap | **Built, opt-in** — `scorpion_mff/exposure.py` refuses a second position in the same underlying, an expiry cluster, or a per-underlying gross-premium breach. Off by default (`exposure_limits=None`); `arm` activates it when `live/exposure.json` exists. `python -m scorpion_mff.cli exposure init | show`. See **`POLICY.md`**. |
| Position stop + marks + log integrity | **Built, opt-in** — `scorpion_mff/stops.py` enforces the breakeven stop the trim already armed but nothing read back (`arm --enforce-breakeven`), marks the open book to a live bid with a total that **withholds itself** when any position cannot be priced, and `verify` proves the fill log parses back contiguous. `python -m scorpion_mff.cli marks [--check] | verify`. See **`HOTPATH.md`**. |
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
docs/GEMS.md           # one acceptance command per gem: invariant, failure mode, non-builds
HOTPATH.md             # design decisions, non-changes, what is unproven
BACKTEST.md            # the executor against 93 historical alerts
POLICY.md              # the reduce-only risk layer + what the record says
TCA.md                 # execution quality: what the bars proved, what fills will prove
```

## Primary files

1. `docs/HANDOFF.md` — start here
2. `docs/GEMS.md` — the acceptance ledger (run one command per gem)
3. `BACKTEST.md` — what the executor does with the record
4. `mff-backtest/bt_full/trades.json`
5. `mff-backtest/bt_full/run_tape_bt.py`
6. `mff-backtest/bt_full/sep8_alerts.json`

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

Run it: `python -m scorpion_mff.cli doctor | replay tapes/sep8.json | backtest |
         expiry audit | recover | verify | desk | tca path | tca fills | policy show |
         exposure show | watch --once | fuzz | arm | status | marks [--check]`.
`doctor` prints the **spread-gate setting** (`OFF (MFF_MAX_SPREAD_RATIO unset)` by
default), the kill switch, the broker mode, and masked tokens — the arming surface is
visible before `arm` is run. `expiry audit` measures the corpus against the 15-minute
cutoff and **refuses** an assignment projection it cannot support. `recover` resolves a
crash-left intent from a venue lookup and never places.
Details, including what is still unproven and why: **`HOTPATH.md`**.
One acceptance command per gem, with its invariant and failure mode: **`docs/GEMS.md`**.
What the executor does with 93 real alerts, and the two defects that found: **`BACKTEST.md`**.
The risk policy, the governor, and the expectancy/allocation statistics: **`POLICY.md`**.
What our fills cost and what the bars did (and did not) prove: **`TCA.md`**.

## Lane boundary

- **PR#2 (this tree, `hardening/correlated-book-guard`) is the live lane.** Only it can
  place an order, and only through `arm --live` after the token, policy, quote-feed and
  kill-switch refusals.
- **PR#1 (`.worktrees/pr1`, `hardening/deterministic-control-plane`) is the paper plane.**
  It is read-only source for ports; it never places an order.
- **No cross-imports and no worktree edits.** `scorpion_mff/` never imports `scorpion` or
  reaches into `.worktrees/`; `.worktrees/pr1/src/` never imports `scorpion_mff/`. The
  two commands that prove the boundary and the merge order are in **`HOTPATH.md`**.

| PR#1 read-only source | PR#2 live port | Verdict |
|---|---|---|
| `.worktrees/pr1/src/scorpion/sessions.py` | `scorpion_mff/calendar.py` | ported + fail-closed hardening |
| `.worktrees/pr1/src/scorpion/expiry.py` | `scorpion_mff/engine.py` `_expire_carried` | ported as a superset |
| `.worktrees/pr1/src/scorpion/outbox.py` | `scorpion_mff/execution.py` `CircuitBreaker` | ported + stricter one-probe |
| `.worktrees/pr1/src/scorpion/outbox.py` (UNKNOWN quarantine) | `scorpion_mff/listener.py` + `ledger.py` `transport_unknown` | ported in final integration; unknown exits page, nothing retries |
| `.worktrees/pr1/src/scorpion/guard.py` | `scorpion_mff/execution.py` `spread_ok` | fail-closed philosophy only; check set not ported |
| `.worktrees/pr1/src/scorpion/reconciliation.py` | `scorpion_mff/broker.py` `OrderLookup` + `ledger.py` | tri-state only; drift-halt not ported |

The full matrix, with live line references and the two unported surfaces, is in
**`HOTPATH.md`**.

## Secrets

Do **not** commit Robinhood tokens, Discord bot tokens, or `.env`. Channel IDs are included (guild membership still required).

## License

Private. Collaborators only.
