# Scorpion v0.22 — formal promotion evidence, kill-switch drills, sizing stress, and adaptive control integrity

v0.22 hardens the boundaries between adaptive research, human-controlled production promotion, and
operator-selected live risk. It does not add unattended brokerage authority or automatic live
position sizing.

## 1. Formal promotion evidence schema

`promotion_evidence_schema.py` turns the v0.21 production evidence object into a typed, versioned,
cryptographically bound evidence DAG. The canonical bundle requires exactly one record for each of
11 evidence classes:

- retraining plan;
- training run;
- shadow release;
- independent promotion decision;
- live paired canary;
- liquidity/capacity report;
- safety latch state;
- runtime certification;
- drift state;
- runtime-policy identity;
- research manifest.

Every evidence envelope binds a subject ID, schema version, producer, observation time, optional
expiry, payload SHA-256, and parent evidence IDs. Validation rejects missing/duplicate evidence
kinds, schema mismatches, missing parents, lineage cycles, future/stale/expired evidence, duplicate
evidence IDs, and a changed bundle hash.

The bundle also carries the existing v0.21 `production_evidence_fingerprint`. The formalized
authorization path therefore verifies both the detailed evidence DAG and the exact production
evidence snapshot before delegating to the already hardened dual-control authorization ledger.

## 2. Fail-closed safety latch hardening

An unseen component no longer defaults to `NORMAL`. Missing safety state is now explicitly:

`NO_TRADE + initialized=False + uninitialized_component_fail_closed`.

An operator-authenticated clear event is required to initialize a component into normal operation.
The safety state exposes `initialized`, and `execution_allowed` requires both initialization and
`NORMAL` mode.

Safety reasons are normalized before event IDs are computed so persisted truncation cannot create
false integrity failures.

The safety ledger now maintains an integrity checkpoint containing event count, head event ID, and
a rolling hash of the ordered safety-event IDs. `verify_integrity()` recomputes every event ID,
recomputes the rolling chain, checks the checkpoint, and verifies that the materialized safety
state equals the latest event. This detects event mutation, deletion, state/history divergence,
and missing initialization evidence.

## 3. Isolated kill-switch and recovery drills

`kill_switch_drills.py` runs against a dedicated drill database, never a production database. A
single drill proves:

1. cold start fails closed;
2. explicit operator initialization is required;
3. an automatic trip blocks execution;
4. a fresh process/restart preserves `NO_TRADE`;
5. safety history verifies before recovery;
6. an operator recovery event restores `NORMAL`;
7. a second restart preserves the recovered state;
8. deliberate safety-event corruption is detected.

The drill returns a durable structured report rather than relying on manual observation.

## 4. Live sizing identity and headroom fixes

The v0.21 sizing request carried a release ID while the live risk snapshot did not. v0.22 adds the
active release identity to `LiveRiskState` and requires an exact request/release match by default.
A sizing decision can therefore no longer be computed for stale model release A against the risk
state of active release B.

The sizing ceiling now also reserves portfolio headroom. Instead of merely checking whether current
gross and cluster exposure are below their limits, the guard calculates:

```
gross_headroom   = max_gross_exposure - current_gross_exposure
cluster_headroom = max_cluster_exposure - current_cluster_exposure
portfolio_headroom = min(gross_headroom, cluster_headroom)

hard_ceiling = min(
    configured risk ceiling,
    research sizing ceiling,
    impact-adjusted capacity ceiling,
    portfolio_headroom,
)
```

The proposed risk therefore cannot be the increment that pushes the portfolio through a gross or
cluster exposure limit.

## 5. Sizing stress envelope

`sizing_stress.py` applies deterministic single-factor and compound shocks to the exact live sizing
guard. The default matrix stresses:

- quote age;
- decision latency;
- capacity collapse;
- daily loss;
- drawdown;
- gross exposure;
- cluster exposure;
- quote-consensus loss;
- runtime-certification loss;
- canary degradation;
- drift reappearance;
- compound microstructure degradation;
- compound portfolio degradation.

The stress report enforces a monotonicity invariant: worsening conditions may reduce the hard risk
ceiling or block the request, but must never increase the ceiling. A request that is already blocked
must never become permitted after a worsening shock.

## 6. End-to-end adaptive control audit

`adaptive_control_audit.py` checks the seams between modules rather than trusting each component in
isolation. It verifies:

- formal evidence bundle -> current production evidence binding;
- production dossier -> training run, shadow release, and artifact binding;
- sizing request -> active release binding;
- sizing decision -> request binding;
- training -> shadow -> canary -> dossier -> sizing temporal ordering;
- risk snapshot and sizing-decision freshness;
- production authorization -> exact dossier and expiry;
- production and live-risk safety latches both allow execution;
- final sizing decision is still permitted.

Failures are partitioned into identity, temporal, and authority classes so operators can distinguish
provenance conflicts from stale evidence and permission failures.

## 7. Gap audit closed in v0.22

This pass closes several concrete gaps that were not merely theoretical:

- missing safety state previously failed open as `NORMAL`;
- safety-event deletion was not independently detectable;
- long safety reasons could hash differently from their persisted representation;
- sizing request release identity was not checked against the active release;
- proposed risk was not subtracted from remaining gross/cluster exposure headroom;
- evidence objects were hashed as one aggregate but did not expose a formal typed evidence DAG;
- sizing tests covered individual failures but did not enforce global monotonicity under compound
  degradation;
- module-level correctness did not prove end-to-end identity/time/authority continuity.

## 8. Authority boundary

The system may autonomously detect, retrain, validate, shadow, stress, audit, quarantine, and trip
`NO_TRADE`. It may calculate whether an operator-selected risk request is inside hard limits.
Production activation, clearing `NO_TRADE`, and the live risk amount remain explicit human-controlled
actions. No v0.22 component submits unattended securities/options orders or chooses a personalized
live position size.
