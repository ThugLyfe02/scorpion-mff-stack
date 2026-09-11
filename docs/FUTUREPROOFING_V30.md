# Futureproofing v0.30 — accepted truth lineage and realized learning yield

v0.30 closes two semantic gaps in the compounding research loop:

1. labels become immutable, revision-aware accepted truth rather than mutable training metadata;
2. research allocation can be calibrated by what prior HUMAN_REVIEW / DEEP_SHADOW work actually produced.

The layer remains research/shadow-only. It does not submit orders, choose live risk, activate releases, or bypass existing production gates.

## 1. Accepted-label truth is a versioned object

`accepted_label_truth.py` adds a dedicated append-only truth ledger.

Every accepted label revision binds:

- event identity;
- exact source-message revision identity;
- accepted `EventKind`;
- confidence;
- acceptance method;
- immutable evidence IDs;
- accepting reviewer/operator;
- reason;
- predecessor accepted-label revision;
- UTC timestamp.

Writes use an explicit compare-and-swap precondition against the current accepted revision. Two reviewers therefore cannot silently race and overwrite each other.

The data plane is:

```text
annotations / adjudication / consensus
    -> accepted truth revision
    -> per-event revision chain
    -> global accepted-truth event chain
    -> integrity checkpoint
    -> materialized current head
```

Revision rows and ledger events are protected by SQLite append-only triggers. Verification independently recomputes revision identities, per-event parentage, global event hashes, event/revision coverage, materialized heads and the integrity checkpoint.

The DB-local hash chain is a corruption/audit mechanism, not a claim that a privileged database attacker cannot rewrite the entire database plus checkpoint.

## 2. Historical truth remains reconstructable

`build_accepted_truth_snapshot()` can reconstruct truth at the current head or at an exact historical global ledger sequence.

A snapshot includes:

- the ledger prefix sequence;
- prefix head hash;
- prefix chain hash;
- each selected event's exact accepted revision;
- source revision;
- label;
- confidence;
- a deterministic snapshot hash.

A later correction therefore creates a new truth snapshot without destroying the exact label state used by an older experiment.

## 3. Truth lineage binds the complete learning chain

`truth_lineage.py` provides the strict research path:

```text
accepted truth snapshot
    -> truth-bound hard-example curriculum
    -> truth-bound training dataset
    -> truth-bound contextual OOF fit
    -> truth-bound untouched holdout evaluation
```

Each stage receives a deterministic lineage hash containing:

- artifact hash;
- exact truth snapshot hash;
- exact event/revision binding set;
- parent lineage hash;
- event count.

The strict curriculum requires `label_source_id` to be the accepted revision ID, not a free-form `consensus` string.

The strict training fingerprint requires every training sample ID to resolve to accepted truth and the training target to equal the accepted label.

The strict OOF/holdout wrappers reject truth disagreement before calling the existing contextual ensemble machinery.

Thus a label correction does not silently leave an old curriculum, dataset, OOF fit or holdout report looking current. Rebinding against the corrected snapshot produces a different lineage identity or fails if the old artifact contains stale truth.

## 4. Realized learning yield is persisted as evidence

`realized_learning_yield.py` adds a separate append-only ledger for realized research outcomes.

Each outcome binds:

- original learning-allocation hash;
- event and research action;
- research segment;
- cost units;
- original expected utility;
- accepted truth revision and snapshot when a usable label resulted;
- curriculum identity;
- paired evaluation identity;
- attribution method;
- paired OOF log-loss and Brier gain;
- false-action / wrong-action deltas;
- realization timestamp.

Attribution methods are deliberately explicit:

- `LABEL_ONLY`
- `PAIRED_OOF_ABLATION`
- `CURRICULUM_BUCKET_ABLATION`

The system does not infer causal credit from a raw before/after production metric.

The yield ledger is append-only, hash chained and checkpoint verified before calibration.

## 5. Sparse yield evidence is partially pooled

Realized utility is converted into a bounded realized/predicted yield ratio.

Calibration estimates:

- global yield;
- action-level yield;
- action × segment yield.

Action estimates shrink toward the global prior. Segment estimates shrink toward their action prior. Small segments therefore cannot produce an extreme multiplier from one lucky example.

A lower-confidence ratio, not the raw sample mean, becomes the utility multiplier. Multipliers are additionally bounded by policy.

If the ledger is invalid, calibration status is `BLOCKED` and every multiplier falls back to `1.0`.

If evidence is insufficient, calibration is `INSUFFICIENT` and likewise cannot steer research allocation.

## 6. v0.28 becomes empirically self-correcting

`information_value.py` now accepts an optional structural yield calibration.

Base information value is still computed from:

- epistemic learnability;
- model disagreement;
- residual hotspots;
- label scarcity;
- coverage deficit;
- novelty;
- actionable disagreement;
- aleatoric discount.

Only after those semantics are computed may realized-yield calibration rescale the utility of:

- `LIGHT_SHADOW`
- `DEEP_SHADOW`
- `HUMAN_REVIEW`

The exact knapsack then optimizes calibrated expected utility under the same fixed research budget.

The allocation hash binds the calibration hash and each decision's base utility, multiplier and calibrated utility.

This means future allocation can learn, for example, that HUMAN_REVIEW has been highly productive in one slice while DEEP_SHADOW has been wasteful there, without granting either action any live execution authority.

## 7. Compounding loop after v0.30

```text
source revision
  -> annotations / consensus
  -> accepted truth revision
  -> accepted truth snapshot
  -> uncertainty + residual structure
  -> expected information value
  -> realized-yield-calibrated research allocation
  -> review / shadow work
  -> accepted truth revision
  -> truth-bound hard-example curriculum
  -> truth-bound training dataset
  -> purged OOF predictions
  -> contextual specialization with shrinkage
  -> truth-bound untouched holdout
  -> realized learning outcome
  -> conservative yield calibration
  -> next allocation
```

The result is a research system whose expensive observations can improve not only the next model, but also the policy for deciding which future observations are worth paying for.
