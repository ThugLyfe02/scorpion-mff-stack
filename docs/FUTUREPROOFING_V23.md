# Scorpion v0.23 — production state machines, bottleneck control, and partial-outage chaos

v0.23 hardens the production-control layer without changing locked MFF strategy semantics or granting unattended brokerage authority. The focus is operational speed, deterministic state continuity, conflict detection, and fail-closed recovery.

## 1. Production bottleneck and conflict audit

`production_bottleneck_audit.py` inspects the actual SQLite control plane and produces a hashed rollout-readiness report. It measures durable raw backlog and oldest age, ingress queue pressure, delivery backlog and expired leases, pipeline and SQLite pre-commit p95 latency, WAL growth, freelist bloat, control-plane heartbeat freshness, duplicate active releases/shadows/rollouts, safety/release conflicts, and stale promotion dossiers.

Critical findings block rollout. Storage/review maintenance findings are surfaced separately so operational debt does not become hidden execution latency.

The audit intentionally uses traffic-independent liveness signals (`resilience-watchdog` and `discord-ingress-queue`) so an idle but healthy system is not misclassified as dead merely because no new trade signal arrived.

## 2. Rollout state machine

`deployment_state_machine.py` turns production changes into a durable explicit lifecycle:

```text
PREPARED
  -> ACTIVE_GUARDED
  -> STABLE
  -> SUPERSEDED
```

Preparation requires an exact candidate release, valid formal promotion evidence, an unexpired human production authorization, an initialized/verified safety ledger, and a fresh bottleneck audit. Activation is a separate operator action and re-checks bottlenecks, safety integrity, candidate lineage, and the exact expected active predecessor.

A stable rollout does not block preparation of its successor. It becomes `SUPERSEDED` only when the successor actually activates. Prepared rollouts may be cancelled and expired authorizations transition to `EXPIRED`, preventing abandoned deployment records from deadlocking future upgrades.

## 3. Fail-closed rollback state machine

Automated logic may reduce risk but may not silently increase it:

```text
ACTIVE_GUARDED / STABLE
  -> HALTED + NO_TRADE
  -> ROLLBACK_PENDING
  -> ROLLBACK_VERIFYING
  -> RECOVERED_GUARDED
  -> RESUME_PENDING
  -> ACTIVE_GUARDED
```

A degradation alarm first verifies that the release recorded by the rollout is still the exact active release. If identity has diverged, Scorpion trips NO_TRADE and enters `FAILED_SAFE` without quarantining an unrelated release.

A known superseded rollback target must be explicitly authorized by an operator. By default, a second distinct operator applies it. The rollback release becomes active only while NO_TRADE remains latched. Independent recovery evidence then verifies runtime certification, replay equivalence, safety-ledger integrity, quote consensus, canary health, drift state, and non-safety bottlenecks.

After verification, the system remains fail-closed in `RECOVERED_GUARDED`. Operator resume is two-phase: `RESUME_PENDING` is persisted before NO_TRADE is cleared; the safety latch is rebound to the exact recovered release; only then does the rollout return to `ACTIVE_GUARDED` and begin a fresh soak. Any resume-finalization exception re-trips NO_TRADE.

## 4. Cryptographic rollout journal

Every rollout transition is persisted as a sequenced event with from/to state, rollback state, actor, reason, timestamp, predecessor hash, event hash, and deterministic event ID. A separate integrity checkpoint stores event count, head hash, and a rolling event-ID chain.

Integrity verification detects sequence gaps, event mutation, event-ID substitution, chain breaks, event deletion, and divergence between the latest journaled state and the materialized rollout row.

## 5. Partial-outage chaos drills

`production_chaos_drills.py` runs only against isolated drill databases and deliberately injects:

- kill-switch trip/restart/recovery and safety-ledger corruption;
- stale resilience-watchdog liveness;
- expired delivery-worker leases;
- competing SQLite writer contention.

Each drill must demonstrate fail-closed behavior. The production database is never used as the drill target.

Operator commands:

```text
scorpion-prod-audit --db <existing-db>
scorpion-chaos-drill --workspace <dir> --component <name> --operator <id>
```

Both emit structured JSON and non-zero exit status when the control plane is not rollout-safe.

## 6. Hot-path latency hardening

The Discord gateway no longer performs the parser/reducer/normalized SQLite transition synchronously on the event loop. `Pipeline` executes blocking transition work on a worker thread while an explicit lock preserves one-at-a-time deterministic state mutation.

Durable receipt writes are also moved off the event loop behind a serialized write lock. The resilience watchdog's DB/profile scans run off-loop as well. Diagnostic ingress heartbeats are coalesced instead of issuing an extra SQLite write for every queue-state observation, while a one-second liveness heartbeat remains traffic-independent.

If normalized processing halts, the gateway can continue durable capture in `capture_only` mode rather than filling a dead in-memory queue. Pending durable raw revisions remain recoverable on restart.

These changes remove avoidable event-loop head-of-line blocking without parallelizing the deterministic reducer or weakening the lossless raw-event boundary.

## 7. Schema-group completeness

Schema contract v7 knows the rollout, rollback, safety-integrity, training/shadow, and production-promotion tables. Advanced safety, deployment, and promotion features are checked as groups: if any member of a feature group exists, missing companion tables are an incompatibility.

This prevents a partially migrated production control plane from passing certification simply because each missing table was individually considered optional.

## 8. Authority boundary

v0.23 preserves asymmetric authority:

- automation may capture evidence, detect bottlenecks/drift, retrain, validate, shadow, audit, halt, quarantine, trip NO_TRADE, and run isolated drills;
- production rollout activation remains an explicit operator action backed by an unexpired evidence-bound authorization;
- rollback application remains an explicit operator action and is verified while NO_TRADE remains latched;
- resuming execution after rollback requires a separate explicit operator action;
- live sizing remains validation of an operator-selected risk fraction, not autonomous personalized sizing;
- no component here submits unattended securities/options orders.

The intended competitive advantage is not uncontrolled autonomy. It is a faster adaptive research and operational system with narrower state transitions, stronger provenance, lower event-loop contention, explicit recovery semantics, and machine-detectable conflicts before they become trading incidents.
