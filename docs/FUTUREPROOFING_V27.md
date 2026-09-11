# Futureproofing v0.27 — migration provenance, capability journaling, single-snapshot activation

v0.27 hardens three production seams that become dangerous only after the control plane is already mature: implicit schema evolution, mutable readiness capability state, and activation decisions assembled from multiple database moments.

## 1. Versioned transactional schema migration ledger

Production schema evolution now has an explicit target contract: `2026.09.v10`.

The migration engine records durable attempts and a tamper-evident event chain:

`STARTED → APPLIED`

or, on abnormal execution:

`STARTED → INTERRUPTED / FAILED`

Each event binds the migration id, from/to schema versions, canonical migration SHA-256, application identity, timestamp, predecessor event hash and deterministic event id. A separate integrity checkpoint binds event count, head hash and chain hash.

The v0.27 adoption migration moves legacy/untracked production databases to v10 and owns the deployment/readiness columns plus readiness-capability journal tables. DDL runs inside `BEGIN EXCLUSIVE`; postconditions are verified before the schema version advances. A process that dies after recording STARTED but before the migration commits leaves an interrupted attempt that is reconciled on the next invocation before any new migration is considered.

The migration engine refuses to proceed when:

- the database advertises an unknown/newer schema version;
- a migration event/hash/checksum or integrity checkpoint is inconsistent;
- v10 postconditions are missing after an allegedly applied migration;
- the migration manifest recorded in history differs from the code's canonical manifest.

`scorpion-schema-migrate --db <path> --apply` exposes the same engine used by the production deployment machine. Constructor-time compatibility remains, but schema mutation is no longer an untracked collection of `ALTER TABLE` repairs.

## 2. Tamper-evident readiness capability journal

A production readiness certificate now enters an authoritative lifecycle when it attempts to create a PREPARED rollout:

`ISSUED → CONSUMED → ACTIVATED`

or:

`ISSUED → CONSUMED → INVALIDATED / EXPIRED`

The journal is append-only at the SQLite layer. Each event carries certificate/component/candidate identity, rollout id, actor, reason, immutable capability payload hash, optional deployment event hash, predecessor hash and event hash. Per-certificate integrity state maintains event count, head hash and chain hash.

CONSUMED is cross-bound to the deployment `PREPARED` event hash. ACTIVATED, INVALIDATED and EXPIRED are cross-bound to their corresponding deployment event hashes. Journal verification recomputes both hash chains and checks that materialized `production_readiness_consumptions` state agrees with the journal.

A corrupt or missing capability journal therefore blocks activation even when the mutable materialized readiness row still looks valid.

## 3. Single-snapshot activation certification

v0.26 sealed activation semantically but still composed some activation evidence through helpers that opened separate SQLite connections. v0.27 makes the authoritative activation decision from the connection already holding the activation `BEGIN IMMEDIATE` transaction.

One certification snapshot now binds:

- schema contract compatibility;
- migration-ledger version/head/chain integrity;
- readiness-capability journal head/chain and CONSUMED state;
- exact incumbent/candidate lineage;
- safety generation and source release;
- runtime halt/heartbeat/backlog control state;
- exact readiness-time bottleneck policy;
- activation-semantic bottleneck state;
- candidate activatability.

The resulting `activation_snapshot_hash` is stored on both the consumed readiness capability and PREPARED rollout. Immediately before any release mutation, activation recomputes the certification from the same transaction snapshot and requires exact equality.

This eliminates the remaining class of read/read races where release state could come from snapshot A, liveness from snapshot B and readiness/journal state from snapshot C.

Harmless heartbeat clock progression remains allowed because the activation fingerprint binds heartbeat semantics, staleness and pressure rather than raw observation timestamps. Material semantic changes require fresh readiness.

## Failure direction

All three layers preserve Scorpion's authority asymmetry:

- migrations may reconcile an interrupted upgrade but cannot silently downgrade or reinterpret a newer DB;
- readiness journal corruption can only block or invalidate production authority;
- activation snapshot mismatch cancels stale PREPARED authority before release mutation;
- journal failure after an otherwise successful activation forces the rollout toward fail-closed handling.

No unattended securities/options submission, autonomous production promotion, autonomous rollback activation or personalized live sizing is introduced.
