# Futureproofing v0.20 — Contracted Auto-Training and Fail-Closed Adaptive Model Lifecycle

v0.20 turns Scorpion's drift-retraining plan into a concrete, model-agnostic training substrate without widening live securities authority.

## 1. Universal trainable-model contract

`trainable_model.py` defines the minimum contract every adaptive model must satisfy before the system can train it:

- explicit model identity and trainer version
- explicit task type
- versioned feature schema with strict field names and data types
- deterministic feature ordering
- unique sample identity and timezone-aware observation time
- finite targets and positive sample weights
- declared determinism level
- a typed `fit(examples, seed)` boundary
- a fitted-model prediction interface
- a serialized artifact with loader identity, version, media type, bytes, and SHA-256 identity

`CallableTrainableModel` is the surgical adapter for arbitrary fitting implementations. Existing Python, sklearn, PyTorch, TensorFlow, XGBoost, custom optimization, or remote training wrappers can fit behind it without gaining direct runtime or brokerage authority.

The dataset fingerprint binds exact sample IDs, timestamps, feature values, targets, weights, group IDs, and the feature-schema fingerprint. Silent schema drift or mutated training rows therefore create a new research identity.

## 2. Drift plan → exact immutable training split

`auto_trainer.build_training_split` consumes the v0.19 `ChallengerRetrainingPlan` and the actual ordered examples.

The newest post-drift observations are frozen as validation. Training rows inside the configured purge embargo before validation are excluded. The requested bounded post-drift adaptation window is then combined with the bounded pre-drift anchor intended to reduce catastrophic forgetting.

The resulting `TrainingSplitManifest` binds:

- source retraining plan
- full dataset fingerprint
- feature-schema hash
- exact train IDs
- exact validation IDs
- exact pre-drift anchor IDs
- purged IDs
- unused post-drift IDs
- validation start timestamp
- purge cutoff timestamp
- split hash

The validation set is never passed to `fit()`.

## 3. Deterministic auto-trainer

`run_auto_training` executes the arbitrary fitting function only through the universal contract.

The trainer:

1. validates every example against the feature schema;
2. materializes and fingerprints the exact split;
3. checks minimum train and validation depth;
4. fits with an explicit seed;
5. verifies fitted-model identity;
6. exports and size-bounds the artifact;
7. hashes the artifact bytes;
8. optionally re-fits models declaring `EXACT` determinism and requires identical artifact hashes;
9. evaluates only the frozen validation set using explicit validation metrics;
10. persists an immutable run ledger containing dataset/split/artifact identity, metrics, status, and failures.

Possible states are `REJECTED_DATA`, `FIT_FAILED`, `VALIDATION_FAILED`, and `SHADOW_READY`.

`SHADOW_READY` is not production promotion and cannot submit an order.

## 4. Automatic shadow lifecycle

`shadow_lifecycle.py` provides a separate model registry whose active state is `ACTIVE_SHADOW` rather than production `ACTIVE`.

A training run may be automatically activated in shadow only when:

- the auto-trainer marked it `SHADOW_READY`; and
- the existing independent promotion evidence stack reached `READY_FOR_OPERATOR_REVIEW`.

Activating a new shadow candidate supersedes only the previous shadow candidate. The registry has no method that changes the production release registry and no brokerage adapter.

Shadow degradation may automatically quarantine the shadow release.

## 5. Automatic protection and rollback behavior

For production safety, v0.20 deliberately implements rollback as **fail closed first**.

`NoTradeSafetyLatch` is durable and auditable. Automated monitors may trip a component into `NO_TRADE`. Clearing that state requires a non-empty operator identity and reason.

`quarantine_and_trip_no_trade` composes the existing release registry with the safety latch:

1. quarantine the degrading active production component;
2. identify its prior release as a rollback candidate;
3. trip the durable `NO_TRADE` latch;
4. leave the prior release `SUPERSEDED` rather than silently activating it.

This prevents a rollback race from replacing one bad model with another unreviewed model while execution remains enabled.

## 6. Authority boundary

v0.20 intentionally does **not** add:

- unattended live securities/options order submission;
- automatic activation of a newly trained model into live trading;
- automatic activation of a prior production release after quarantine;
- personalized live position sizing or account-specific investment decisions.

Those remain explicit human-control boundaries.

The system may autonomously collect evidence, detect drift, allocate a retraining plan, materialize a leakage-safe split, fit arbitrary models through the contract, validate artifacts, promote qualified artifacts into shadow, quarantine degraded shadow/production components, and fail closed into `NO_TRADE`.

## 7. Why this matters

The useful adaptive loop is now concrete rather than aspirational:

```text
observed degradation
  -> corroborated drift plan
  -> immutable dataset + feature-schema identity
  -> leakage-safe chronological split
  -> arbitrary contracted fitter
  -> serialized artifact identity
  -> independent frozen validation
  -> shadow qualification
  -> automatic shadow canary lifecycle
  -> existing economic / capacity / regime / execution gates
  -> operator-controlled production decision
```

This makes Scorpion better at learning without letting model training silently mutate trading authority.
