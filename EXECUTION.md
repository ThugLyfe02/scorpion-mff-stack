# Confirmed execution: the book the broker would recognise

The engine decides. The broker fills. Until now there was nothing durable between
those two facts:

* a rejected buy stayed in the engine's book — the listener only logged it;
* a partial fill was recorded as the full clip;
* a rejected sell had already moved realised P&L and the governor;
* `ack.ok=False` was ignored;
* a restart forgot every open position, so the bot could re-enter or fail to exit.

`scorpion_mff/execution.py` is the missing authority. `live/execution.jsonl` records
every intent **before** the broker is called, applies only **acknowledged** fills, and
`sync_engine()` rebuilds the engine's positions and realised state from confirmed
events. The engine is a decision cache; the execution book is the truth.

```
Discord → engine (optimistic decision)
             │
             ├─ ExecutionBook.record_intent()      ← append before any network call
             ├─ broker.place(intent)
             ├─ ack.ok?  ├─ no  → reject → sync_engine() (rollback)
             │           └─ yes → apply_ack → sync_engine() (resize to the fill)
             └─ fill log: buy decision / ack / reject / alert
```

## What the listener does now

For each intent:

1. **Persist the intent** with a client order id. If the log cannot be written, the
   order is not placed — no durable intent, no trade.
2. Call the broker.
3. On `BrokerError` → append `reject`, sync the engine, log `reject`.
4. On `Ack(ok=False)` → same rollback path. The old code ignored this completely.
5. On a fill → apply the ack to the book, sync the engine, log the `ack` with the
   **actual** quantity and price; a partial is annotated `partial:5/14`.
6. If the book cannot represent an ack, re-sync what landed, halt new buys, and
   record `alert: execution_desync:<why>`. Exits still run.

## The log

Append-only JSONL. Replay is the recovery mechanism.

```json
{"v":1,"t":"intent","cid":"QQQ|CALL|719|0DTE:buy:2026-03-10:1@2026-03-10",
 "ts":1772000000.0,"action":"buy","contract_key":"QQQ|CALL|719|0DTE",
 "qty":14,"limit":1.02,"channel":"etf-options","reason":"entry:standard:..."}

{"v":1,"t":"ack","cid":"...","ts":1772000000.0,"qty":5,"price":1.02,
 "broker_id":"order-123","ok":true}

{"v":1,"t":"reject","cid":"...","ts":1772000060.0,"reason":"broker:insufficient buying power"}

{"v":1,"t":"fill","cid":"...","ts":1772000120.0,"qty":9,"price":1.02,
 "broker_id":"order-123"}

{"v":1,"t":"baseline","equity":12400.0,"peak":15100.0,"day":"2026-03-10",
 "realized_today":-300.0,"trades_today":2,"losses":1}
```

* `ack.qty` is **cumulative** for that order. A second ack of 3 after 2 applies a delta
  of 1; duplicate broker responses cannot double-fill.
* `fill.qty` is **incremental** (a late partial from a poll or a fill report). It is
  clamped to the order's remaining quantity.
* A torn final line is ignored (a crash mid-append). Corruption earlier in the file
  raises `ExecutionError` and `arm` refuses — a book that cannot be replayed could
  re-enter a position or forget one.

## Rejects, partials and oversells

| event | book effect | engine after sync |
|---|---|---|
| rejected buy | no lot | optimistic position removed |
| partial buy | lot = filled qty, order `PARTIAL` | position resized to filled qty |
| rejected sell | lot unchanged; prior partial fills kept | position/realised restored |
| partial sell | lot reduced by executed qty; realised booked | remaining position restored |
| oversell | clamped to the lot, `oversell_clamped` anomaly | no synthetic contracts |

The engine's optimistic mutation is deliberately kept: it is what makes the decision
path synchronous and fast. `sync_engine` is the correction step, applied immediately
after the broker answers, so the window where the cache is wrong is one call wide.

## Restart recovery

`arm` loads the book before the first message:

```python
execution = ExecutionBook.load(ROOT / "live" / "execution.jsonl")
execution.ensure_baseline(...from the persisted governor...)
engine = Engine(...)
execution.sync_engine(engine, time.time())
listener = Listener(engine=engine, execution=execution, ...)
```

`sync_engine` restores:

* open positions (quantity, average price, channel, adds/trimmed, breakeven stop);
* `engine.closed` for today (never chase a contract already flattened);
* realised P&L and equity;
* today's realised P&L, trades and losing streak;
* the equity peak — without this, a restart silently resets the drawdown;
* expired lots — a carried `0DTE` or an ISO expiry before today is closed at zero
  (the maximum loss for a long option), flagged `expired_at_zero`, so a restart
  cannot hold a dead slot or count premium that no longer exists. A real settlement
  feed should replace the zero assumption; the event makes the assumption visible.

### Baseline, not amnesia

The book can be introduced mid-history. `ensure_baseline()` persists the governor's
prior equity, peak, day and counters **once**. Subsequent syncs add only new realised
P&L on top. Without it, the first sync would set equity to zero and erase a drawdown
limit that had already been earned.

## Operator surface

```bash
python -m scorpion_mff.cli execution show     # confirmed orders, lots, realised P&L
python -m scorpion_mff.cli execution verify   # replay the log; non-zero if corrupt
python -m scorpion_mff.cli execution show --json
```

`arm` prints restored positions, confirmed orders, and any pending orders. Pending
orders are **not auto-retried** on restart: a crash may have happened before or after
the broker accepted the order, and replaying a market-moving action on a guess is
worse than an operator reviewing it. The idempotency key makes a deliberate retry
safe; the choice is explicit.

## Bounded reconciliation

`reconcile(book, broker_positions)` compares contract and quantity:

```python
{"matched": False,
 "differences": [{"contract_key": "QQQ|CALL|719|0DTE", "book": 10, "broker": 7, "delta": -3}],
 "book_positions": 1, "broker_positions": 1}
```

It is deliberately quantity-based. A broker position feed is not configured by
default; when one exists, this is the check that should run before arming and after a
disconnect. A mismatch is an operator decision, not an automatic flatten — auto-
flattening on a bad feed is how a reconciliation bug becomes a market order.

## What this does not solve

* **Broker position import.** The book is authoritative for orders this process sent.
  Positions opened outside it, or by a previous version, need an explicit import or a
  broker snapshot comparison. `reconcile` exposes the check; it does not invent the
  feed.
* **Partial-fill detection inside the RH adapter.** `RobinhoodAgenticBroker` parses
  `average_price` and an order id, but the vendor's filled-quantity field is not
  invented here. When the endpoint is confirmed, return the real cumulative quantity
  in `Ack.qty`; the book already handles partials.
* **Out-of-order broker callbacks.** `record_fill` applies incremental fills in the
  order they arrive. A venue that reports a fill after a cancel needs an explicit
  policy; the book currently keeps any fill that landed.
* **Cross-process locking.** One writer is assumed. Two armed processes on the same
  `execution.jsonl` would interleave appends. That is an operator error the file
  format cannot prevent.
