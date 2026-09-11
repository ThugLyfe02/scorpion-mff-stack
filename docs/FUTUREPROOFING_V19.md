# Futureproofing v0.19 — execution capacity, purged OOS evidence, and drift retraining

v0.19 tightens the parts of the research loop that can make a strategy look scalable or adaptive when it is not. The implementation remains outside unattended live-order authority.

## 1. Capacity is now impact-adjusted, not just depth-compatible

`liquidity_capacity.py` previously answered a useful but incomplete question: whether a larger requested clip could fit inside haircutted displayed depth for every lifecycle leg.

The v0.19 frontier also charges each supported lifecycle an adverse incremental price-impact penalty:

- requested quantity is scaled by the candidate clip multiplier;
- displayed depth is conservatively haircutted;
- side participation is measured against the remaining usable depth;
- excessive participation makes the lifecycle unsupported;
- incremental impact grows with quoted spread and a concave participation power law;
- entry and exit impact are normalized to scaled entry premium so contract multiplier cancels;
- historical return is reduced by that impact before the scenario can pass;
- robust capacity requires sufficient lifecycle coverage, positive stressed mean return, sufficient stressed positive-trade ratio, and bounded mean impact.

This is intentionally a stress model, not a claim that displayed size predicts exact fills. Its job is to stop raw backtest return from being reused unchanged at larger size.

The report now exposes, per scenario and frontier point:

- raw mean return;
- impact-adjusted mean return;
- stressed positive-trade ratio;
- mean modeled impact fraction;
- maximum side participation;
- maximum clip multiplier robust across every configured depth haircut.

## 2. Walk-forward validation now proves the purge and confidence bound

`walk_forward.py` already used chronological folds and removed training trades whose close crossed the pre-test embargo. v0.19 makes that contract explicit and harder to accidentally weaken.

Each fold records:

- training samples retained;
- overlapping training lifecycles purged;
- stale samples removed by an optional rolling training lookback;
- actual train/test embargo gap;
- whether any temporal label overlap survived the purge.

The pooled OOS gate no longer treats every trade as statistically independent. OOS trades are aggregated to daily means and evaluated with a deterministic circular block bootstrap. Qualification therefore requires both positive realized OOS mean and a configurable conservative lower bound, plus minimum unique OOS days and fold stability.

This directly reduces confidence inflation from clustered intraday options signals.

## 3. Drift-triggered challenger retraining is corroborated and durable

A drift signal should create a research opportunity, not an uncontrolled retraining loop.

`drift_retraining.py` adds a durable challenger-retraining planner with four states:

- `NO_ACTION`
- `ACCUMULATING_EVIDENCE`
- `COOLDOWN`
- `READY_RESEARCH_CHALLENGER`

Evidence can include:

- Page-Hinkley degradation;
- Bayesian degradation changepoint probability;
- fill-calibration decay;
- robust residual hotspots.

Normal retraining requires multiple independent confirmations. An extremely high-confidence Bayesian degradation changepoint can bypass the ordinary confirmation count, but cannot bypass the retraining-storm ceiling.

Before a plan can become ready it must also have enough post-drift evidence. The planner then freezes a split contract:

- newest post-drift observations are reserved for untouched validation;
- older post-drift observations form the adaptation set;
- a bounded pre-drift anchor is retained to reduce catastrophic forgetting;
- a time embargo must be applied between train and validation materialization;
- repeated retraining plans are throttled by cooldown and a rolling storm budget.

Ready plans are persisted idempotently in `challenger_retraining_plans`. The plan identity binds the parent release, exact dataset fingerprint, drift boundary, evidence, split sizes, and retraining policy.

The durable plan is a trainer input. It does not itself fit, promote, size, or deploy a model.

## 4. Governance can require capacity evidence

`PromotionEvidence` now accepts `liquidity_capacity` and an explicit `require_capacity_gate` switch. A supplied non-robust capacity report blocks promotion review. When the gate is required, missing capacity evidence also blocks.

Walk-forward remains a mandatory promotion gate, so the stronger purge and block-bootstrap contract is inherited automatically.

## 5. Intended research loop

```text
new market + execution evidence
        ↓
quality / calibration / drift detectors
        ↓
corroborated degradation?
        ├─ no  → keep collecting evidence
        └─ yes
             ↓
post-drift sample sufficiency
             ↓
chronological train / purge / untouched validation plan
             ↓
research challenger only
             ↓
purged walk-forward + nested temporal selection
             ↓
conditional regime safety
             ↓
cost-adjusted value
             ↓
impact-adjusted liquidity/capacity frontier
             ↓
operator promotion review boundary
```

## 6. Authority boundary

v0.19 does not add:

- unattended live securities/options submission;
- autonomous production promotion;
- autonomous rollback activation;
- personalized live position sizing;
- automatic expansion of research-only strategy buckets.

The new automation is limited to evidence evaluation, immutable research planning, and durable challenger-retraining coordination.
