# Scorpion operator intelligence and resilience

This document describes the operational-intelligence layer added on top of the deterministic MFF control plane. It does **not** change the locked Discord strategy semantics and it does **not** authorize autonomous live brokerage submission.

## Design boundary

Scorpion is allowed to become autonomous about **observation, evidence preservation, recovery, degradation, blocking, prioritization, and reconciliation**.

It is not allowed to become autonomous about live securities/options order submission.

That distinction is deliberate: the system can become more self-protecting and productive without letting an interpretation model silently become an execution authority.

## Operational modes

The resilience controller continuously evaluates durable health and maintains one cached mode:

- `NORMAL` — evidence quality, latency, backlog, heartbeat and storage signals are within policy.
- `DEGRADED` — ingestion remains active, but actionable events are forced through explicit review because one or more warning signals are present.
- `HALTED` — raw Discord ingestion and evidence journaling continue, but newly proposed effects are persisted as `BLOCKED_SYSTEM` until the condition is investigated/acknowledged.

Inputs include:

- explicit halt state
- raw-processing backlog
- operator-review backlog
- ambiguity rate
- low-confidence rate
- unresolved association rate
- parser and pipeline p95 latency
- pipeline-heartbeat age
- WAL growth
- cached Discord source-behavior shifts

The watchdog is asynchronous and cached. The Discord hot path reads its latest assessment instead of recomputing expensive source analytics per message.

## Discord source intelligence

For each configured author/channel pair, Scorpion can compare a recent behavioral window with a non-overlapping historical baseline.

Signals include:

- edit-rate shift
- ambiguity-rate shift
- actionable-rate shift
- unique-contract density shift

A suspicious source shift does not create or cancel a trade. It changes review posture and is surfaced to the operator.

This is intended to detect source-format drift, unusual posting behavior, compromised/changed channel behavior, or parser-distribution changes without pretending to infer trade quality from the author's historical P&L.

## Sequence integrity

Every normalized event can be checked against recent deterministic source history before its decision packet is produced.

Examples include:

- follow-up without a resolved contract
- follow-up without a live deterministic position
- new entry while that contract is already live
- source timestamp regression
- very rapid entry-to-exit reversal
- add/trim following an exit for the same contract

Critical findings force review. They do not rewrite the original Discord event.

## Operator Decision Packet

Each accepted normalized Discord event gets an immutable decision packet containing:

- event/message identity
- event kind and contract
- disposition
- evidence-strength score
- parser rule/confidence
- association method/confidence
- operational mode
- sequence findings
- resilience signals
- cached source-shift score
- machine-readable reason codes

Dispositions are:

- `OBSERVE`
- `READY_FOR_OPERATOR_REVIEW`
- `REVIEW_REQUIRED`
- `BLOCKED_SYSTEM`

The packet is committed in the **same normalized SQLite transaction** as signal/effects/audit/integrity/stage trace/raw completion/heartbeat.

Operator resolution is recorded later as a separate immutable lifecycle step rather than mutating the original decision evidence.

## Failure behavior

### Normal transaction

1. Persist immutable raw Discord revision.
2. Parse and associate.
3. Check sequence integrity.
4. Build decision packet from cached operational evidence.
5. Reduce deterministic source state.
6. Atomically persist signal, proposed effects, packet, audit, integrity record, stage trace, raw completion and heartbeat.
7. Advance in-memory state only after commit succeeds.

### Normalized commit failure

- raw revision remains recoverable
- signal/effects/packet are not partially committed
- in-memory state does not advance

### `HALTED` mode

- Discord raw ingestion continues
- deterministic event history remains faithful to the source
- proposed effects are stored with `BLOCKED_SYSTEM`
- the packet records `BLOCKED_SYSTEM` and the operational mode/reasons
- blocked effects are not counted as ordinary pending-review effects

## Risk-prioritized operator inbox

`scorpion-ops` includes unresolved decision packets ordered by operational urgency.

Default priority behavior:

- P0: system-blocked event
- P1: review-required event
- P2: ready-for-operator-review event
- P3: observation-only event

Exit/trim/stop classes and sufficiently old actionable packets are promoted in priority. Actionable packets also receive a stale flag after the configured age threshold.

This prioritizes human attention. It does not execute the item.

## Read-only reconciliation

`scorpion-reconcile` compares deterministic Scorpion position state against an externally supplied observation snapshot.

It can flag:

- deterministic quantity with no corresponding external position
- quantity mismatch
- average-price mismatch
- external position not reflected in deterministic state
- unexpected external position absent from Scorpion state

The reconciliation module is provider-neutral and **read-only**. It cannot place, cancel, resize, close, or otherwise modify an external position.

## Operator commands

### Consolidated operations snapshot

```bash
scorpion-ops --db scorpion.db
```

Shows:

- current operational mode
- resilience signals and recommended actions
- prioritized unresolved decision inbox
- semantic decision health
- pending raw/review counts
- per-stage latency report
- SQLite/WAL storage snapshot
- source-bound integrity verification

### Reconcile an external account observation

```bash
scorpion-reconcile observed_positions.json --db scorpion.db
```

Input shape:

```json
[
  {
    "contract_key": "QQQ|CALL|719|2026-09-08",
    "quantity": 1,
    "average_price": "1.01"
  }
]
```

This command only reports divergence.

## Invariants worth preserving

Future work should preserve these guarantees:

1. Raw receipt is durable before normalized processing.
2. No in-memory state advance before the normalized transaction commits.
3. Source history is never rewritten merely because operational mode degrades.
4. `HALTED` blocks proposed effects but continues evidence collection.
5. Model/shadow intelligence cannot mutate deterministic state.
6. Reconciliation remains read-only unless an explicit separately reviewed design changes that boundary.
7. Any new intelligence must be measurable through replay/adjudication/tournament evidence rather than promoted on intuition alone.
