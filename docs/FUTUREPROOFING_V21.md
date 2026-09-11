# Scorpion v0.21 — production evidence control, live sizing safety, and adversarial fail-closed proofs

v0.21 extends the adaptive path without granting autonomous securities execution authority.
The design goal is to make the transition from research challenger to human-controlled production
activation machine-checkable, provenance-bound, time-bounded, and fail-closed.

## 1. End-to-end adaptive identity chain

The production gate now binds one exact chain:

```
drift/change-point evidence
  -> retraining plan + dataset fingerprint
  -> exact immutable trainer dataset
  -> purged train/validation split
  -> contracted arbitrary fitter
  -> serialized artifact digest
  -> active shadow release
  -> live paired canary
  -> impact-adjusted capacity evidence
  -> independent promotion evidence
  -> runtime certification + safety latch
  -> immutable production dossier
  -> distinct model/risk approvals
  -> operator-controlled production activation
```

A mismatch anywhere in this chain blocks the production dossier.

The auto-trainer now verifies that `ChallengerRetrainingPlan.dataset_fingerprint` exactly matches
the dataset actually supplied to the universal trainer. A drift plan therefore cannot silently be
reused against a newer, mutated, reordered-in-content, or otherwise different training corpus.

## 2. Validation failures are durable evidence

Model fitting and model validation are separate failure domains.

Prediction failures, metric implementation failures, non-finite metric values, duplicate metric
names, and non-finite thresholds no longer create ambiguous successful runs. Validation-time
exceptions are converted into durable `VALIDATION_FAILED` training records while retaining the
artifact digest for forensic analysis.

The system may inspect a failed artifact in research; it cannot advance it into shadow.

## 3. Production promotion dossier

`production_gate.py` introduces a dedicated production evidence layer after shadow qualification.
A dossier requires all of the following:

- drift retraining plan is research-ready;
- retraining-plan dataset identity equals the trainer dataset identity;
- training run is `SHADOW_READY`;
- active shadow release points to the exact training run and artifact hash;
- independent governance result is `READY_FOR_OPERATOR_REVIEW`;
- paired live shadow canary is mature and ready;
- canary pair depth meets the configured production minimum;
- liquidity/capacity frontier is robust at the required clip multiplier;
- runtime certification is current and passing;
- component safety latch is `NORMAL`;
- no active drift is present at the production boundary;
- training artifact is not older than the configured maximum age;
- shadow has completed the configured soak period;
- canary, capacity, and runtime-certification evidence are fresh;
- canary evidence post-dates activation of the shadow release it evaluates.

Every field above is hashed into one `evidence_hash`. Production policy thresholds are separately
hashed into `policy_hash`. The immutable dossier ID binds both.

## 4. Anti-TOCTOU production authorization

Approvals apply to one exact evidence snapshot, not merely to a model name.

If any bound field changes after review — artifact, data, split, shadow release, canary, capacity,
safety latch, drift state, runtime certification, research manifest, policy fingerprint, or
production policy — authorization fails and a new dossier is required.

Dossiers also expire after a configurable TTL.

The durable approval ledger requires separate approval roles. The default production policy uses:

- `MODEL_REVIEWER`
- `RISK_REVIEWER`

One operator cannot satisfy both roles. Approval rows are unique by dossier/role and
dossier/operator.

Passing the gate yields `AUTHORIZED_FOR_OPERATOR_ACTIVATION`. It does not activate a release by
itself.

## 5. Live sizing safety envelope

`live_sizing_safety.py` is intentionally a validator, not a position-size selector.

An operator first supplies a requested risk fraction. The guard then computes a hard ceiling from:

```
min(
    configured maximum risk fraction,
    research sizing envelope maximum,
    capacity reference risk fraction * max robust clip multiplier,
)
```

The request is blocked when any of the following is true:

- NO_TRADE safety latch is active;
- research sizing evidence is not ready;
- no research risk simulation survived its ruin/drawdown constraints;
- liquidity capacity is not robust;
- model/policy drift is active;
- quote consensus is unhealthy;
- runtime is not certified;
- live canary is unhealthy;
- daily loss reaches its configured ceiling;
- portfolio drawdown reaches its configured ceiling;
- gross or cluster exposure reaches its configured ceiling;
- quote evidence is too old;
- decision latency is too high;
- the operator sizing request has expired;
- the requested fraction exceeds the hard ceiling.

The result is an immutable decision hash bound to request, risk state, policy, ceiling, and
failures. No dollar allocation or contract quantity is selected by this component.

## 6. Adversarial failure injection

The v0.21 tests deliberately inject faults rather than only exercising happy paths:

- production dossier database payload tampering;
- same-person dual-role approval attempts;
- post-approval drift/evidence mutation;
- stale canary evidence;
- canary evidence that predates shadow activation;
- expired production dossiers;
- NO_TRADE safety state at production and sizing boundaries;
- degraded capacity frontier;
- stale operator sizing requests;
- drift, quote-consensus, runtime-certification, canary, drawdown, daily-loss, quote-age, and
  latency faults;
- retraining-plan dataset fingerprint mismatch;
- validation metric implementation failure;
- non-finite validation metric thresholds.

Every injected safety-relevant failure is expected to block progression rather than degrade into a
warning.

## 7. Authority boundary

v0.21 deliberately preserves the asymmetric authority model:

- automated systems may detect drift, retrain research challengers, validate them, activate shadow
  observation, quarantine degrading components, and trip NO_TRADE;
- automated systems may calculate hard safety ceilings around an operator-provided sizing request;
- automated systems may assemble and verify production dossiers;
- production activation remains an explicit human-controlled decision;
- NO_TRADE clearing remains an explicit operator action;
- no component introduced here submits unattended securities/options orders;
- no component introduced here chooses a personalized live position size.

This is intentional: Scorpion can become increasingly autonomous at learning, testing, rejecting,
shadowing, and protecting without allowing model adaptation itself to silently acquire brokerage
authority.
