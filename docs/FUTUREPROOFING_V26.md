# Futureproofing v0.26 — activation epoch seal

v0.26 closes the production seam between a successfully prepared rollout and the later human activation decision.

## Invariant

A candidate may enter `ACTIVE_GUARDED` only when all of the following still hold inside the same `BEGIN IMMEDIATE` transaction that precedes release mutation:

1. the PREPARED rollout still owns exactly one consumed readiness capability;
2. the capability has not expired;
3. the candidate, component, dossier, authorization, evidence bundle and predecessor bindings are unchanged;
4. the exact bottleneck-policy material used to issue readiness is intact and canonical;
5. safety generation/head/chain are unchanged;
6. runtime-halt epoch, heartbeat semantics, pending raw/delivery counts, release identity and pre-existing rollout lineage match the readiness epoch;
7. a fresh bottleneck audit recomputed under the exact persisted readiness policy remains rollout-safe and matches the activation semantic fingerprint;
8. the caller-provided audit describes the same activation epoch;
9. the normalized operator snapshot still matches readiness, with only the expected target `PREPARED` rollout fields ignored;
10. the legacy deployment checks still pass before any release-state mutation.

Any readiness-epoch failure causes the risk-increasing transition to abort before release mutation. The stale PREPARED rollout is then cancelled by the readiness gate so the deployment lane is released and a new attempt must begin with fresh readiness evidence. Production activation remains a human action.

## Why the activation fingerprints are separate

A byte-for-byte replay of the original readiness snapshot would be wrong. Preparation itself legitimately creates one `PREPARED` rollout row, heartbeat timestamps advance normally, and SQLite file/WAL sizes can move without a semantic production change.

v0.26 therefore separates evidence-instance identity from activation semantics:

- `control_state_hash` remains the exact pre-PREPARED control binding;
- `activation_control_hash` normalizes only the rollout being activated while preserving release, safety, halt, heartbeat metadata, backlog and every other pre-existing rollout row;
- `bottleneck_state_hash` remains the complete observed readiness state;
- `activation_bottleneck_hash` excludes expected storage/time drift while retaining actionable pressure, queue, latency, conflict, liveness, finding and readiness semantics;
- `operator_state_hash` remains the complete operator-visible readiness state;
- `activation_operator_state_hash` normalizes only the target component's rollout id/state fields.

This permits harmless clock progress but rejects a system that changed and merely returned to a superficially healthy final state.

## Exact policy replay

The consumed readiness capability now persists canonical bottleneck-policy JSON plus its SHA-256 identity. Activation reconstructs and validates that policy and reruns the production bottleneck audit under it.

A caller therefore cannot prepare under strict thresholds and later activate using a looser audit policy.

## Core architecture

The proven deployment/rollback engine remains the core implementation. v0.26 adds one protected pre-mutation transaction hook. The legacy core's default hook is a no-op; the guarded production state machine overrides it with activation-epoch verification.

This avoids duplicating or rewriting rollback behavior while making the risk-increasing activation boundary stronger.

## Adversarial proofs

`tests/test_activation_epoch_v26.py` covers:

- harmless heartbeat timestamp progression succeeds when semantics are unchanged;
- safety NORMAL → changed → NORMAL churn is rejected even when the final visible binding matches;
- runtime halt → clear churn is rejected even when the final visible halt state is clear;
- a looser caller audit cannot bypass the exact readiness policy;
- persisted policy tampering invalidates activation;
- healthy but different heartbeat/queue semantics require fresh readiness;
- every failed activation leaves the incumbent active, candidate inactive, and stale rollout cancelled.

## Authority boundary

v0.26 does not add unattended live order submission, autonomous production promotion, autonomous rollback activation, or personalized live sizing. It strengthens evidence, invariants and fail-closed behavior around an operator-controlled activation decision.
