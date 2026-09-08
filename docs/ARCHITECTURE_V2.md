# Scorpion control-plane architecture v2

## Scope

This implementation separates fast deterministic signal handling from research/chat workloads.
The repository's legacy cron/browser approach remains historical evidence only.

The code in `src/scorpion/` supports:

- push-based Discord `MESSAGE_CREATE` ingestion;
- immutable UTC-normalized raw events;
- deterministic parser outcomes (`ENTRY`, `ADD`, `TRIM`, `EXIT`, `IGNORE`, `AMBIGUOUS`);
- event-sourced state reduction with generation checks and idempotency;
- a SQLite WAL audit/event store;
- heartbeat + halt state;
- conservative replay primitives;
- paper/shadow execution;
- explicit operator review.

**Live brokerage submission is intentionally not implemented in the package.**
A live integration must consume reviewed effects behind an explicit authorization boundary.

## Process isolation

```text
discord-ingest
    |
    | RawDiscordMessage
    v
event store --> parser --> association --> pure reducer --> proposed effects
                                                   |
                                                   +--> review/operator
                                                   +--> paper/shadow broker

research/scanner/chat processes are separate and cannot block discord-ingest.
```

There is no browser deadman in the money path.

## Time model

Internally all times are timezone-aware UTC:

- `source_ts_utc`: canonical Discord API timestamp;
- `received_ts_utc`: local receipt timestamp;
- edited/reference metadata preserved independently.

UI-displayed Pacific timestamps are never treated as authoritative system clocks.

## Identity

IDs, not display names, are authoritative:

- guild ID;
- channel ID;
- author ID;
- Discord message ID;
- contract key.

Channel labels are presentation metadata only.

## Reducer

`reduce_book(old_state, event) -> (new_state, effects)` performs no I/O, no network calls,
no randomness, and no wall-clock reads. This makes replay and crash forensics deterministic.

The reducer includes:

- event idempotency;
- max-open-position constraint;
- first-entry pipe-test hint;
- one-add-per-generation constraint;
- closed-generation stale-entry rejection;
- source timestamp ordering checks;
- explicit ambiguity/review effects;
- halt behavior.

## Follow-up association

Follow-ups do not guess. They associate by:

1. explicit Discord reply/reference if known;
2. a unique live contract;
3. otherwise `REVIEW`.

This is deliberately conservative because a wrong exit association is worse than latency.

## Persistence

SQLite is configured with WAL, `synchronous=FULL`, foreign keys, and unique keys covering:

- raw Discord messages;
- normalized signals;
- effect idempotency;
- approvals;
- heartbeats;
- runtime halt state.

Signal volume is tiny; correctness matters more than distributed-system complexity.

## Broker boundary

`PaperBroker` can shadow effects.

`ReviewOnlyBroker` returns `AWAITING_HUMAN_AUTHORIZATION`.

A live adapter is not included. If one is developed outside this package, it should require:
- immutable reviewed effect ID;
- current generation match;
- fresh quote;
- operator authorization;
- broker/account reconciliation;
- idempotency key;
- audit record.

## SLOs to measure

Recommended initial objectives:

- gateway receive -> raw-event persistence: p95 < 100 ms
- raw-event persistence -> normalized signal: p95 < 100 ms
- normalized signal -> proposed effect: p95 < 100 ms
- heartbeat age during monitored session: < 5 s
- duplicate execution proposals: 0
- unassociated follow-ups auto-executed: 0
