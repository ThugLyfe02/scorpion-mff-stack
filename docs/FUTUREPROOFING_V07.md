# Scorpion v0.7 — Causal Reliability, Guarded Intelligence, and Runtime Certification

This layer composes PRs #1–#4 rather than changing Ryan's locked strategy semantics.

Its purpose is to eliminate failure classes that become important only after the basic system is already fast, deterministic, measurable, and research-capable:

- clock corruption that can invalidate causal replay;
- duplicate downstream instructions after retries/reconnects;
- attractive research results that depend on one favorable execution assumption;
- a newly promoted parser/policy degrading after activation;
- evidence scattered across tables and difficult to reconstruct under pressure;
- deployment confidence based only on unit tests rather than the real runtime database.

It still does not implement unattended live securities/options order submission.

## 1. Causal clock discipline

`temporal_guard.py` treats time as evidence rather than incidental metadata.

The live system now has deterministic checks for:

- Discord source timestamp materially in the future relative to receive time;
- abnormally high source→receive latency;
- edit timestamps preceding message creation;
- per-author/per-channel source timestamp regression in receive order;
- market quote timestamps preceding the modeled execution decision timestamp;
- quotes arriving outside the configured causal freshness window.

This matters because an apparently excellent backtest can become invalid if machine clock skew allows a quote that was actually observed before the decision boundary to be interpreted as post-decision evidence.

### Operator surface

```bash
scorpion-time --db scorpion.db
```

The output includes:

- messages examined;
- future-source count;
- high receive-lag count;
- edit-order errors;
- source timestamp regressions;
- receive-lag p95;
- a critical flag.

`scorpion-ops` also includes the same temporal report in the consolidated operations snapshot.

No temporal anomaly automatically creates a trade. The purpose is to detect when timing evidence itself cannot be trusted.

## 2. Durable exactly-once logical delivery

`delivery.py` introduces a durable execution-intent delivery ledger.

The problem it solves is distinct from parser idempotency.

Even when the source event is only processed once, a downstream worker can still experience:

1. prepare instruction;
2. send instruction;
3. network/process failure before acknowledgement is recorded;
4. restart;
5. resend the same logical instruction as if it were new.

The delivery ledger gives every prepared instruction a deterministic ID derived from:

- source event;
- effect kind;
- contract;
- position generation;
- quantity;
- limit price;
- runtime policy fingerprint.

A retry therefore maps to the same logical delivery ID.

### Delivery states

```text
PREPARED
   ↓
LEASED
   ├── ACKNOWLEDGED
   ├── REJECTED
   └── lease expires → LEASED by another worker

PREPARED / LEASED → EXPIRED
```

### Lease semantics

Only one worker can hold an unexpired delivery lease.

If that worker disappears, another worker can reacquire the *same* delivery after the lease expires. It does not mint a new logical instruction.

Terminal deliveries cannot be reacquired.

This is deliberately provider-neutral and does not call a brokerage. It is the reliability contract a future reviewed broker adapter can consume.

## 3. Guarded release registry and rollback planning

`release_guard.py` introduces immutable component-release lineage for parsers, policies, classifiers, and similar software artifacts.

Each release is bound to:

- component name;
- artifact hash;
- runtime policy fingerprint;
- research manifest hash;
- prior release identity.

### Release states

```text
CANDIDATE
   ↓ explicit operator activation after governance gates
ACTIVE
   ↓ next release activated
SUPERSEDED

ACTIVE
   ↓ degradation evidence
QUARANTINED
```

Automated monitoring may quarantine an active software release.

It may also identify the previous known release as the rollback candidate.

It does **not** automatically activate that prior release.

That is intentional: autonomous containment is allowed, but increasing/change-of-authority remains an operator decision.

### Why quarantine first

If an active parser suddenly begins escalating non-actions into actions, the safe response is not to continue using it while an automated rollback system makes another unreviewed choice.

The system can quarantine the suspect release immediately and force the affected path back through review while an operator chooses the rollback target.

## 4. Paired champion/challenger canaries

`tournament.py` answers whether a candidate was strong historically.

`canary.py` answers a different question:

> Is the challenger behaving safely on the same stream the current champion is seeing now?

Each paired observation records:

- champion correctness;
- challenger correctness;
- champion actionability;
- challenger actionability;
- contract divergence;
- champion latency;
- challenger latency.

The canary computes:

- champion accuracy;
- challenger accuracy;
- paired accuracy delta;
- a 95% lower confidence bound on the paired delta;
- action escalations;
- contract divergences;
- latency ratio.

### Canary states

```text
OBSERVING
READY_FOR_OPERATOR_REVIEW
QUARANTINE
```

Dangerous behavior is asymmetric.

A challenger that turns a non-actionable champion interpretation into an actionable interpretation can be quarantined even if its overall accuracy is higher.

The challenger never gets execution authority from the canary.

## 5. Promotion governance now accepts online evidence

`governance.py` can now include optional v0.7 evidence:

- paired canary report;
- multidimensional execution uncertainty report;
- runtime certification status.

When supplied, these gates are enforced in addition to the existing requirements:

- tournament qualification;
- empirical selective-risk policy;
- purged walk-forward survival;
- research manifest;
- no active online drift;
- complete historical source data;
- quote coverage;
- displayed-depth coverage.

The maximum output remains:

```text
READY_FOR_OPERATOR_REVIEW
```

It never activates a component itself.

## 6. Multidimensional execution uncertainty envelope

A single backtest profile is a point estimate.

Even a latency sensitivity curve can still assume one quote freshness window and one resting-limit duration.

`uncertainty_envelope.py` reruns the same archived Discord history across multiple execution assumptions:

- decision latency;
- maximum allowed quote lag;
- resting entry-limit wait;
- depth-supported-only trade inclusion.

For every scenario it records:

- completed trades;
- trades with sufficient evidence for analysis;
- mean/median realized return;
- conservative edge;
- win rate;
- chronological positive-fold ratio;
- drawdown;
- pass/failure reason.

The envelope then exposes:

- number of populated scenarios;
- number passing;
- worst conservative edge;
- worst mean return;
- worst win rate;
- overall robustness result.

The objective is not to find a scenario where the strategy works.

It is to ask whether the evidence remains attractive when execution assumptions become less favorable.

### Compact grid

`compact_execution_scenarios()` is intended for routine research runs and probes a high-information diagonal across progressively worse latency/freshness assumptions.

### Exhaustive grid

`default_execution_scenarios()` is a larger Cartesian grid intended for deeper offline research.

The full grid is intentionally not run on every live event or routine operational query.

## 7. One-command causal trace reconstruction

As Scorpion gained raw journals, signal events, decision audits, Operator Decision Packets, effects, timing traces, approvals, and integrity records, debugging a single event became a database-join problem.

`causal_trace.py` reconstructs that chain in one object.

```bash
scorpion-trace <event_id> --db scorpion.db
```

The trace includes:

- normalized signal row + payload;
- latest matching immutable raw Discord revision + processing status;
- parser/association decision audit;
- immutable Operator Decision Packet;
- all proposed effects and durable statuses;
- stage-level latency trace;
- cryptographic integrity record + canonical payload;
- approval records, when present;
- a completeness flag and explicit list of missing evidence surfaces.

This is especially useful for incidents where the question is:

> Why did Scorpion interpret this Discord message this way, what did it propose, what policy was active, how long did each stage take, and can the evidence still be cryptographically verified?

## 8. Runtime certification against the real database

Unit tests are necessary but they are not a deployment proof.

`certification.py` runs invariants against the actual runtime database.

```bash
scorpion-certify --db scorpion.db
```

It checks:

### Schema contract

Required tables/columns for this version must exist.

### Source-bound evidence integrity

The cryptographic ledger must reconstruct against the real raw/signal/audit/effect/packet evidence.

### Duplicate-event replay invariance

The normalized signal stream is replayed with every event duplicated.

The final deterministic state fingerprint must be unchanged.

### Input-order replay invariance

The input enumeration order is reversed.

Because canonical replay sorts by source/receive/event identity, the resulting state fingerprint must still match.

### Temporal integrity

The persisted raw-message clock evidence must pass the temporal guard.

### Optional backup recovery proof

```bash
scorpion-certify \
  --db scorpion.db \
  --backup backups/scorpion-certification.db
```

The backup must independently satisfy:

- SQLite integrity check;
- foreign-key consistency;
- evidence verification;
- identical signal count;
- identical deterministic state fingerprint;
- identical cryptographic integrity head.

A failed certification exits non-zero so this command can become a deployment/checklist gate.

## 9. Existing v0.6 guarantees remain intact

v0.7 is additive to the previous composition layer, including:

- deterministic research manifests;
- logical Discord dataset fingerprints;
- quote-tape fingerprints;
- runtime policy fingerprints bound into decision packets and integrity evidence;
- policy-explicit replay;
- displayed bid/ask depth support;
- partial-depth historical fills;
- partial exits excluded from completed-trade sizing evidence;
- purged expanding walk-forward validation;
- empirical risk/coverage selection;
- Page-Hinkley drift detection;
- multi-window SLO burn rates;
- operational-mode hysteresis;
- replay-verified SQLite backups;
- schema compatibility checks;
- conservative parser tournaments;
- champion/challenger governance.

## 10. Failure hierarchy

The intended control hierarchy is now:

```text
raw evidence
   ↓
causal time validation
   ↓
deterministic interpretation
   ↓
semantic / sequence / source / resilience validation
   ↓
policy-bound Operator Decision Packet
   ↓
atomic effect + audit + integrity persistence
   ↓
code-only quote/limit preparation
   ↓
deterministic delivery identity
   ↓
durable lease / acknowledgement boundary
   ↓
paper execution OR explicit human-authorized integration boundary
```

Parallel research path:

```text
exhaustive Discord archive
   +
timestamped bid/ask/depth evidence
   +
exact code + policy + dataset provenance
   ↓
exact-execution forensics
   ↓
FDR-controlled discovery
   ↓
purged walk-forward
   ↓
execution uncertainty envelope
   ↓
risk simulation
   ↓
paired live shadow canary
   ↓
promotion governance
   ↓
READY_FOR_OPERATOR_REVIEW
```

## 11. Autonomy boundary

The system may autonomously:

- ingest;
- deduplicate;
- persist;
- recover;
- verify;
- detect timing anomalies;
- detect semantic drift;
- detect source-behavior shifts;
- profile;
- replay;
- research;
- quarantine a suspect software release;
- block strategy/system progression;
- lease/retry a deterministic downstream instruction;
- expire stale instructions;
- produce a rollback candidate;
- produce a promotion candidate;
- certify or fail certification.

The system does not autonomously:

- promote a parser/model/strategy into production;
- activate a rollback release;
- convert a research-only ETF/lotto bucket into execution eligibility;
- submit unattended live securities/options orders.

Those boundaries are intentional engineering controls, not missing features.
