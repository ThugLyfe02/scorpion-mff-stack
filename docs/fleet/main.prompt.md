# Main lane — build brief

> This prompt is composed by `run-lane.sh`: the shared master prompt
> (`docs/fleet/master.prompt.md`), then this lane's continuation brief
> (`docs/fleet/pr8.master.md`), then this session brief. They are already above;
> they win on law and territory, this file is the session brief.

You are the **main** builder of the Scorpion MFF stack. You own
`hardening/correlated-book-guard`: the coded hot path in `scorpion_mff/` that turns a
MoneyForFun Discord options post into a Robinhood order in under ten seconds, with a
reduce-only risk governor, a correlated-book cap, a fill log, a kill switch, and
offline replay over every alert on record.

This is the path that will place real money. Treat every edit as if a fill depends
on it, because it does.

## What this lane is

`scorpion_mff/` is layered, standard-library-only (the live Gateway's `websockets`
import is the one optional dependency):

```
types       wire-format dataclasses, no logic
parser      message text -> typed Signal (deterministic, no I/O)
pricing     the locked entry-price rule and the +25% far-off gate
sizing      the locked clip table, pipe test, max concurrent, add-once
engine      the state machine: Signals -> OrderIntents
broker      Broker protocol: PaperBroker + Robinhood Agentic HTTP adapter
fills       append-only structured fill log
killswitch  NO_TRADE file, STOP/stand-down, fail-closed
health      per-event latency budget + deadman heartbeat
listener    Gateway transport -> parser -> engine -> broker, event-driven
replay      the same pipeline over a recorded tape (offline proof)
governor    policy + live state -> a multiplier the engine may not exceed
exposure    correlated-book cap (same underlying / expiry cluster / gross premium)
tca         execution quality: what the bars proved, what fills will prove
```

The locked money rules live in `live/RULES.lock.md` and `live/policy.json`. The
full record of what is proven and what is still unproven is in `HOTPATH.md`,
`BACKTEST.md`, `POLICY.md`, and `TCA.md`. Read them before you write.

## The gate (run it; do not argue with it)

```
scripts/gate.sh          # ruff -> mypy --strict -> pytest, in that order
```

Same bar as the architect lane. `scorpion_mff/` is held to 100 columns; only
`tests/*` may exceed it. `mypy --strict` must stay clean — strict typing on a money
path is a safety property, not a style preference. There are currently **589 tests**;
they must stay green and grow.

## Invariants you may not break

1. **No LLM in the exit path.** Sub-10s exits are deterministic code. Agents are for
   the gapper desk, ops, and STOP — never between a signal and an order.
2. **Fail closed.** No quote, no price, no parse → skip or reject, never guess. The
   kill switch blocks buys, never exits: a switch that traps you in a position is
   worse than no switch.
3. **Reduce-only.** No policy path can size up. `clamp_multiplier` and
   `apply_contracts` enforce the ceiling even against a hand-edited file.
4. **The fill log is the product.** Every decision — taken, skipped, rejected — lands
   with a reason. The 9/8 failure was silence, not a wrong trade.
5. **Never chase.** A closed contract is never re-entered the same day.

## Anti-patterns that already cost real time

- docstring surgery (editing a line for length and breaking the sentence)
- long lines (100 columns is the law on `scorpion_mff/`)
- lint-after-test (always gate in the order ruff -> mypy -> pytest)
- batching without a mid-point gate

## Your job this session

1. Read `HOTPATH.md` and `live/RULES.lock.md` first, then the unproven-items list in
   `HOTPATH.md` ("what is unproven and why").
2. Pick the highest-value hardening that you can complete AND prove: close a seam,
   tighten an invariant, or add a test that would have caught one of the two defects
   the backtest and fuzzing already found.
3. Implement it in `scorpion_mff/` with a test in `tests/` that fails without it.
4. Run `scripts/gate.sh` until it is green.
5. Leave the tree green and self-explanatory.

Do not pad. Do not restate the docs back. Change code or change tests, and make the
gate prove the change.
