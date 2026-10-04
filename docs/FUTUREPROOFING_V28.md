# Futureproofing v0.28 — information-efficient learning and hard-example curriculum

v0.28 turns several existing Scorpion intelligence signals into a closed research loop for spending scarce labels and shadow-model compute where they have the highest expected effect on future error.

## Why this layer exists

Raw uncertainty is not synonymous with learning value. An event can be uncertain because the model lacks knowledge (epistemic uncertainty), or because the source itself is intrinsically ambiguous (aleatoric uncertainty). Treating both cases equally wastes review and compute budget and can contaminate training with inherently noisy examples.

Scorpion already measures ensemble uncertainty, residual failure hotspots, novelty, review diversity and inference cost. v0.28 composes those surfaces without changing deterministic runtime semantics.

## Expected information value

`information_value.py` evaluates each research opportunity using:

- epistemic learnability (`epistemic × (1 - aleatoric)`);
- ensemble variation/disagreement;
- FDR-controlled residual-hotspot priority;
- label scarcity;
- slice coverage deficit;
- novelty;
- actionable disagreement;
- an explicit aleatoric ambiguity discount.

High-aleatoric / low-epistemic cases are deferred instead of repeatedly consuming training budget. This does not mean ambiguous examples disappear from safety review; it means they are not mistaken for high-value model-learning examples.

The resulting research actions are strictly:

- `LIGHT_SHADOW`
- `DEEP_SHADOW`
- `HUMAN_REVIEW`
- `DEFER`

There is no execution or brokerage action in this module.

## Exact budget allocation

The allocator solves a deterministic multiple-choice knapsack over all candidate events and the permitted research actions. It therefore selects the globally best portfolio of review/inference work under a fixed compute/review budget rather than greedily taking the locally highest score.

The output is fingerprinted from the exact policy, budget and selected actions so repeated research runs are reproducible.

## Hard-example curriculum

`hard_example_curriculum.py` converts accepted, high-confidence labeled examples into a deterministic research-training curriculum.

It enforces:

- frozen holdout exclusion;
- minimum label confidence;
- minimum information value;
- maximum aleatoric uncertainty;
- class-share ceilings;
- slice-share ceilings;
- minimum label breadth when evidence is available;
- exact label-source lineage;
- source-dataset fingerprint binding;
- deterministic curriculum fingerprinting.

The curriculum intentionally favors hard-but-learnable examples while preventing one noisy class, source channel, parser rule or residual hotspot from monopolizing training.

## Compounding loop

The intended research loop is:

```text
live/shadow observations
  -> uncertainty decomposition
  -> residual hotspot evidence
  -> information-value assessment
  -> exact review/inference budget allocation
  -> human/consensus labels
  -> immutable hard-example curriculum
  -> arbitrary contracted trainer
  -> purged/frozen OOS validation
  -> existing statistical/economic/capacity gates
  -> shadow only
```

The untouched holdout remains outside curriculum construction and model selection. Production promotion remains separately evidence-bound and operator-controlled.

## Strategic advantage

The system now separates three questions that weaker learning loops usually collapse together:

1. **Is this example uncertain?**
2. **Is the uncertainty actually learnable?**
3. **Is resolving it worth more than the other opportunities competing for the same human/compute budget?**

That separation should improve label efficiency, shadow-compute efficiency and challenger quality while reducing the risk of overfitting to intrinsically ambiguous edge cases.
