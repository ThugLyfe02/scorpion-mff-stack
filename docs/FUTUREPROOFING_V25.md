# Scorpion v0.25 — non-bypassable readiness-bound production preparation

v0.25 closes the remaining gap between production readiness and production state mutation. A readiness report is no longer merely something an operator is expected to inspect before deployment. The canonical `DeploymentStateMachine.prepare()` transition consumes a short-lived, candidate-bound readiness capability, and the SQLite control plane rejects a `PREPARED` row that does not carry a matching unconsumed capability.

This layer does not grant automated production authority. It makes the human-controlled production boundary narrower, fresher, and harder to misuse.

## 1. Two identities: evidence instance and production state

v0.24 correctly hashed operator snapshots and bottleneck reports, but their hashes included observation timestamps. Comparing those hashes later would therefore reject a healthy system simply because it was observed twice.

v0.25 separates two concepts:

- **evidence-instance identity** keeps the original timestamped snapshot/report hashes for provenance;
- **stable state identity** hashes the observed production state without the observation timestamp.

The readiness capability binds both. The timestamped identity proves which observation produced the certificate; the stable identity proves whether the rollout-relevant state is still the same when preparation is attempted.

## 2. Candidate-bound rollout readiness capability

`issue_rollout_readiness_certificate()` first runs the full production-readiness evaluation and then binds the result to one exact deployment attempt:

- component;
- candidate release ID;
- expected active predecessor;
- candidate artifact hash;
- candidate policy fingerprint;
- candidate research-manifest hash;
- production dossier ID, evidence hash and policy hash;
- production authorization ID and expiry;
- formal promotion-evidence validation fingerprint;
- formal evidence-bundle hash;
- schema-contract version;
- host hot-path benchmark ID;
- isolated chaos-drill ID when required;
- operator evidence-instance hash and stable state hash;
- bottleneck evidence-instance hash and stable state hash;
- safety event generation/head/chain;
- DB-critical control-state hash;
- exact bottleneck policy used to evaluate the host.

The capability expires at the earliest of the host readiness TTL, dossier expiry, or human production authorization expiry.

## 3. Control-state epoch

A second state binding is intentionally smaller and suitable for verification while the deployment database is write-locked. It binds:

- exact active release set;
- safety mode and source release;
- safety materialized update generation;
- safety event count, head event ID and chain hash;
- runtime halt payload and generation;
- semantic liveness metadata for required heartbeats;
- durable raw backlog count;
- pending delivery count;
- nonterminal rollout IDs/states/generations.

Heartbeat timestamps are deliberately not part of this epoch. Freshness is re-evaluated immediately before the transaction by the operator/bottleneck audit, while the epoch binds the semantic liveness state. Otherwise a routine healthy heartbeat tick could invalidate a capability between two instructions without any actual production-state change.

## 4. Preparation is now a capability-consuming transaction

The public deployment path is:

```text
candidate release
  + production dossier
  + human authorization
  + formal promotion evidence
        ↓
full production readiness
        ↓
candidate-bound rollout readiness capability
        ↓
re-read operator state + bottleneck state
        ↓
BEGIN IMMEDIATE
        ↓
re-read candidate / predecessor / safety generation / control epoch
        ↓
register unconsumed capability
        ↓
INSERT PREPARED
        ↓
SQLite trigger verifies + consumes capability atomically
        ↓
COMMIT
```

If any comparison fails, no rollout is prepared.

## 5. SQLite-level bypass resistance

The migration adds `production_readiness_consumptions` and readiness identity columns to `deployment_rollouts`.

A `BEFORE INSERT` trigger rejects any `PREPARED` rollout unless the row references a matching, unexpired, unconsumed readiness capability with the same component, candidate, dossier, authorization, evidence bundle and control-state hash.

An `AFTER INSERT` trigger consumes that capability in the same transaction.

This is important because Python method discipline alone is not a sufficient production boundary. Even a caller that reaches the legacy preparation implementation through reflection cannot insert a PREPARED rollout after the v0.25 schema is installed without satisfying the database capability contract.

A consumed capability remains spent after cancellation or rollout expiry. The operator must generate fresh readiness evidence for another attempt.

## 6. State change means fresh readiness

Preparation is rejected when any bound identity changes, including:

- certificate integrity or expiry;
- candidate release identity;
- candidate artifact, policy, or research manifest;
- dossier/evidence/policy identity;
- human authorization identity or expiry;
- evidence-bundle or evidence-validation identity;
- schema contract;
- active predecessor;
- safety generation/head/chain;
- safety predecessor binding;
- runtime halt state;
- semantic ingress liveness state;
- pending raw/delivery control state;
- nonterminal rollout state;
- operator production state;
- bottleneck production state.

A particularly important case is safety churn. If safety transitions away from the predecessor and later returns to the same visible `NORMAL → predecessor` state, the event generation/head/chain have changed. The old readiness capability is therefore invalid even though a superficial current-state comparison would look identical.

## 7. Activation freshness

`PREPARED` is not an indefinite permission to activate. The consumed readiness capability remains attached to the rollout, and guarded activation refuses to proceed after that capability expires. A still-PREPARED rollout is journaled as `EXPIRED`, leaving the candidate inactive and requiring a fresh readiness cycle.

The pre-existing activation checks remain in force as well: fresh bottleneck evidence, exact predecessor CAS, authorization expiry, candidate state, safety integrity, and post-activation safety provenance rebinding.

## 8. Proven rollback engine retained

The v0.23 rollout/rollback implementation is retained as a private core and the public deployment state machine subclasses it. Risk-decreasing halt/rollback/recovery behavior therefore stays behavior-identical while the risk-increasing PREPARED transition receives the new readiness contract.

The public module re-exports the existing rollout/rollback types so callers do not need a parallel deployment API.

## 9. Adversarial proofs

v0.25 adds explicit tests for:

- successful single-use readiness consumption;
- replay after cancellation;
- direct invocation of the legacy preparation implementation against the SQLite trigger;
- certificate tampering;
- candidate substitution;
- formal evidence-bundle substitution;
- safety-generation churn that returns to the same visible state;
- healthy-but-material bottleneck/liveness state changes;
- active-predecessor substitution;
- readiness expiry before preparation;
- readiness expiry after PREPARED but before activation.

The expected response to each case is rejection before a risk-increasing production transition.

## 10. Schema contract v8

Schema contract `2026.09.v8` requires readiness identity columns whenever the deployment-rollout feature group is present and adds `production_readiness_consumptions` to that group. Partial v0.25 rollout migrations therefore fail schema certification.

## 11. Authority boundary

v0.25 does not change Scorpion's production authority model:

- automation may measure, benchmark, validate, retrain, shadow, audit, reject, quarantine, halt and trip NO_TRADE;
- readiness may determine whether a deployment is eligible to be presented to an operator;
- production activation remains an explicit operator action;
- rollback application and post-recovery resume remain explicit operator actions;
- live sizing remains validation of an operator-selected risk amount;
- no component here submits unattended securities/options orders.

The engineering objective is controlled speed: make stale or internally inconsistent production decisions impossible to reuse while keeping the safe research, shadow, evidence, and fail-closed loops highly automated.
