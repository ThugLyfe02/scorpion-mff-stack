# Futureproofing v0.29 — purged-OOF contextual ensemble specialization

v0.29 adds a research-only ensemble layer that allows models to specialize by predeclared causal context only when out-of-fold evidence and an untouched holdout justify the specialization.

## Problem

A single global ensemble can hide complementary model strengths. One model may consistently parse high-volatility messages better while another is stronger in a quieter source regime. Naively learning per-slice weights is dangerous, however: small slices overfit quickly, post-hoc slice discovery creates selection bias, and a context rule that looks strong on discovery data can increase false actionable predictions on future data.

## Fit contract

`contextual_ensemble.py` consumes already out-of-fold model probabilities. It requires:

- unique event IDs;
- explicit non-empty fold IDs;
- multiple OOF folds;
- one complete model set across every event;
- one identical probability-label support;
- finite normalized probabilities;
- predeclared context dimensions;
- minimum sample and fold support before a context can specialize.

Global model weights are learned from mean OOF log loss. Local context weights are learned from the same OOF prediction surface and then shrunk toward global weights.

## Hierarchical shrinkage and movement cap

For each context, local evidence receives support-dependent shrinkage:

`support / (support + prior_strength)`

The effective shrinkage is then capped so no model's contextual weight can move farther than `maximum_context_weight_shift` from its global weight.

This gives Scorpion useful specialization where evidence is deep while forcing sparse or unstable slices back toward the global ensemble.

Contexts below minimum sample/fold support never receive local weights. Unknown contexts deterministically fall back to the global ensemble.

## Frozen holdout firewall

A contextual fit is not considered successful because its OOF slice scores look attractive. `evaluate_contextual_ensemble()` requires a completely disjoint frozen holdout and compares contextual versus global weighting on the exact same events.

The gate measures:

- paired log-loss improvement;
- chronological daily improvement;
- circular block-bootstrap lower bound;
- accuracy regression;
- Brier-score regression;
- actionable false-positive regression;
- contextual usage rate.

Qualification is only `READY_FOR_SHADOW_RESEARCH`. Any holdout overlap is rejected before evaluation. Any required holdout improvement/safety condition that fails returns `BLOCKED`.

## Compounding path

The intended intelligence loop is now:

```text
uncertainty decomposition
  -> information-value allocation (v0.28)
  -> high-quality labels
  -> holdout-safe hard-example curriculum (v0.28)
  -> arbitrary contracted challengers
  -> purged OOF predictions
  -> globally regularized contextual specialization (v0.29)
  -> untouched chronological holdout firewall
  -> existing shadow / statistical / economic gates
```

This lets Scorpion capture real complementary model skill without creating a post-hoc regime router or allowing contextual weights to acquire production authority.

## Authority boundary

The contextual ensemble is research/shadow evidence only. It cannot submit orders, select live position size, promote itself, activate a release, or change deterministic runtime strategy semantics.
