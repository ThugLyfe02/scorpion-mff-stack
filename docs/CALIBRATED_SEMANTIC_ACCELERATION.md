# Calibrated Semantic Acceleration (v0.3)

This layer is stacked on top of the deterministic control plane and precision-intelligence PRs.
It does not change Ryan's locked MFF strategy semantics and does not add autonomous live brokerage submission.

## Why this layer exists

Accuracy is not just parser coverage. A production interpretation system needs to know:

- whether its confidence scores are empirically trustworthy;
- whether new wording is outside the historical distribution;
- whether multiple independent shadow models agree;
- which human reviews will teach the system the most;
- whether a parsed option contract exists in the known instrument universe;
- whether the quote used for review is stale, crossed, abnormally wide, or materially dislocated;
- whether a candidate parser changes downstream state/effects, not merely labels;
- and whether durability guarantees are costing avoidable hot-path latency.

## Empirical calibration

`calibration.py` scores parser rules against adjudicated examples. Each rule receives:

- sample count;
- empirical accuracy;
- 95% Wilson lower confidence bound;
- mean stated confidence;
- Brier score;
- calibration gap.

A rule can be `UNCALIBRATED`, `PROVISIONAL`, `TRUSTED`, or `DEGRADED`. This is advisory evidence;
it does not bypass deterministic parsing or human authorization.

## Novelty / out-of-distribution detection

`NoveltyIndex` combines normalized tokens and character trigrams. New wording is scored against known exemplars.
The layer also exposes Jensen-Shannon token drift for rolling corpus comparison.

High novelty does not invent a parse. It raises the value of review and corpus expansion.

## Shadow ensemble

`ensemble.py` combines observation-only `ShadowPrediction` objects with optional reliability weights.
It reports consensus, entropy, top-two margin, and actionable/non-actionable disagreement.

No ensemble output mutates the money-path state machine.

## Active-learning queue

`active_learning.py` ranks human review candidates using transparent components:

- parser uncertainty;
- association uncertainty;
- wording novelty;
- ensemble entropy;
- actionable model disagreement;
- uncalibrated rules;
- contract semantic review;
- quote semantic review.

This focuses human labeling effort on examples with the highest expected learning/risk value.

## Contract and quote semantic checks

`semantic.py` adds a provider-neutral contract catalog protocol plus deterministic quote-quality validation.
It can catch syntactically valid but semantically impossible contract parses and stale/crossed/wide/dislocated quotes.

These checks are evidence gates around review, not autonomous trading instructions.

## Semantic replay diff

`semantic_replay.py` compares final state fingerprints, effect signatures, and position states between two event streams.
A parser/model candidate therefore cannot hide behind label-level accuracy if it changes downstream behavior.

## Two-phase durability with atomic normalized commit

Phase 1 remains a synchronous durable raw Discord receipt. This guarantees crash recovery evidence.

Phase 2 is now one SQLite transaction containing:

1. normalized signal insert;
2. proposed effects;
3. decision audit;
4. raw processing `DONE` marker;
5. pipeline heartbeat.

The in-memory book state is assigned only after this transaction commits. If the transaction fails, the raw revision remains pending and in-memory state does not advance.

This removes several independent FULL-sync transactions from the hot path while strengthening state/database consistency.
