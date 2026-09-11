# Scorpion v0.9 — Durable Causality, Point-in-Time Learning, and Poison-Event Containment

v0.9 focuses on seams that can invalidate an otherwise sophisticated system: runtime/replay order drift, training leakage, poison-event boot loops, and recovery certification that proves the wrong ordering semantics.

It does not modify Ryan's locked strategy rules and does not add unattended live securities/options submission.

## 1. Durable receipt order and normalized process order

Two orders now exist explicitly because they answer different questions.

### Raw receipt order

`raw_receipt_order(receipt_seq, raw_event_id)` records the durable arrival order of immutable Discord revisions.

This is the recovery order for raw revisions that reached durable storage but were not normalized before a process failure.

### Normalized process order

`event_processing_order(process_seq, event_id)` records the order in which normalized event transactions became durable.

This is the canonical state-machine order for runtime restart, backup verification, checkpoints, reconciliation, and runtime certification.

The key invariant is:

```text
normalized transaction COMMIT
        ↓
process_seq is durable
        ↓
only then may in-memory BookState advance
```

Therefore replay does not need to guess state order from source timestamps.

### Legacy upgrade behavior

Databases created before v0.9 do not contain these tables. On migration:

- raw revisions are backfilled by persisted receive time;
- normalized events are backfilled by the previous canonical source/receive/event ordering;
- subsequent live events receive actual commit sequence numbers.

This preserves prior behavior while making new ordering explicit.

## 2. Explicit replay-order API

`replay()` now accepts `ReplayOrder`:

- `SOURCE_TIME` — historical/counterfactual research behavior;
- `INPUT` — caller-supplied durable order.

Production recovery loads events through `load_signals_in_processing_order()` and uses `INPUT` semantics.

Source-time replay remains available for research. It is no longer silently treated as equivalent to live mutation order.

## 3. Order fingerprints

`scorpion-order --db scorpion.db` reports:

- raw count;
- receipt-ordered count;
- normalized signal count;
- process-ordered count;
- missing identities;
- sequence contiguity;
- receipt-order SHA-256 fingerprint;
- process-order SHA-256 fingerprint.

The command exits nonzero if the durable order contract is incomplete.

Backup verification also compares process-order fingerprints between source and backup.

## 4. Checkpoints now preserve process order

State checkpoints are created from process-ordered normalized history.

Full replay fallback uses the same process order. Tail replay begins exactly after the checkpoint's durable event boundary.

`scorpion-certify` verifies checkpoint/tail reconstruction against full process-order replay.

## 5. Point-in-time causal feature store

`causal_features.py` creates immutable training features keyed by `event_id` and `process_seq`.

The feature builder may consume only:

- current raw-context information knowable before the decision, such as channel/author/ticker and source-to-receive lag;
- normalized/audit evidence from events with `process_seq < target_process_seq`.

It may not consume:

- the current normalized action label;
- later human adjudication;
- events committed after the target event.

The current `event_kind` is deliberately excluded from feature payloads.

### Human labels arrive later

Adjudications can be attached after the fact:

```text
frozen point-in-time feature snapshot
        +
later human adjudication
        ↓
training row
```

This separates feature time from label time.

### Commands

Backfill missing snapshots:

```bash
scorpion-features --db scorpion.db --backfill
```

Export labeled rows:

```bash
scorpion-features --db scorpion.db --export causal-training.jsonl
```

Feature verification checks process-sequence identity and SHA-256 payload integrity.

## 6. Poison-event quarantine

A raw revision that repeatedly crashes normalization can no longer remain an immortal startup poison pill.

Default state progression is:

```text
PENDING
  ↓ failure 1
PENDING / retryable
  ↓ failure 2
PENDING / retryable
  ↓ failure 3
QUARANTINED
```

Every failure attempt is append-only in `raw_failure_events`. Current retry/quarantine state is stored separately in `raw_failure_state`.

When the retry ceiling is reached:

- raw evidence remains preserved;
- `raw_processing.status` becomes `QUARANTINED`;
- startup may continue in a halted/operator-required posture;
- the revision is no longer automatically retried forever.

### Operator workflow

Inspect:

```bash
scorpion-quarantine --db scorpion.db
```

Requeue only after the defect or source issue is understood:

```bash
scorpion-quarantine \
  --db scorpion.db \
  --requeue <raw_revision_id> \
  --operator <name> \
  --note "reason for requeue"
```

Requeue identity and note are persisted.

## 7. Deterministic fault certification

`scorpion-fault-certify --db scorpion.db` performs reproducible, read-only fault probes against the actual normalized history.

Current probes include:

- durable processing-order completeness;
- duplicate-delivery idempotency by duplicating every normalized event;
- prefix/tail reconstruction at deterministic cut points;
- checkpoint-or-full-recovery equivalence;
- causal feature-store integrity;
- source-time-vs-process-order state comparison as a diagnostic.

The source-time comparison is intentionally diagnostic. If source-time sorting differs from durable process order, that does not invalidate runtime state; it demonstrates why the explicit process-order contract is necessary.

## 8. Runtime certification changes

`scorpion-certify` now uses process-ordered events for canonical runtime state.

It checks:

- schema compatibility;
- source-bound evidence integrity;
- durable processing-order completeness;
- duplicate-event idempotency under process order;
- checkpoint/process-order equivalence;
- causal feature-store integrity;
- temporal integrity;
- poison-event quarantine visibility;
- optional process-order-preserving verified backup equivalence.

The old assumption that arbitrary input reversal should reconstruct identical state is removed. Stateful event systems are not generally commutative; the correct invariant is that **durable live order can be reproduced exactly**.

## 9. Read-only reconciliation uses live state semantics

`scorpion-reconcile` now rebuilds Scorpion state from durable process order before comparing it with an externally observed position snapshot.

This prevents the operator from seeing one account reconciliation state while restart/runtime would reconstruct another.

## 10. Authority boundary

v0.9 can autonomously improve:

- evidence ordering;
- recovery determinism;
- failure containment;
- feature integrity;
- training-data causality;
- runtime certification;
- diagnostic fault testing.

It does not autonomously:

- submit live securities/options orders;
- choose personalized live position sizes;
- promote a strategy/parser/model into production;
- activate a rollback;
- promote ETF/lotto research-only buckets.
