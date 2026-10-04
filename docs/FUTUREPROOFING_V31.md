# Futureproofing v0.31 — causal research allocation, sequence intelligence, and replication-gated breakthroughs

v0.31 moves Scorpion's compounding intelligence loop from observational learning-yield calibration toward causal, support-aware research optimization. It remains entirely upstream of live brokerage authority.

## Why this layer exists

Once an adaptive allocator starts choosing which examples receive HUMAN_REVIEW, DEEP_SHADOW, hotspot targeting, curriculum inclusion or contextual specialization, the allocator changes its own future training data. Naively learning from that history creates a self-confirming loop: preferred actions are observed more often, alternatives lose support, delayed outcomes disappear from evaluation, and apparent success can become a consequence of the policy that generated the data rather than the intervention itself.

v0.31 seals those seams before they can compound.

## 1. Research-only randomized assignment ledger

`research_experimentation.py` records explicit research interventions as treatment bundles. Bundles can represent overlapping actions rather than pretending HUMAN_REVIEW + DEEP_SHADOW is the same treatment as either action alone.

Every assignment binds:

- experiment episode and step index;
- event identity;
- current regime and context fingerprint;
- upstream allocation hash;
- reward-contract hash;
- complete supported treatment distribution;
- chosen treatment and logged propensity;
- random draw;
- predecessor assignment for multi-step research paths;
- assignment time and outcome-maturity time.

The default policy requires at least two supported treatments and enforces a minimum propensity floor. This prevents positivity collapse from silently making counterfactual estimation impossible.

Assignments and outcomes are append-only and globally hash chained with an integrity checkpoint. Outcomes cannot be recorded before their configured maturity horizon and must bind immutable downstream evidence.

## 2. Doubly robust counterfactual learning efficiency

`counterfactual_learning_efficiency.py` requires cross-fitted outcome predictions for every supported treatment and combines them with logged propensities using a doubly robust estimator.

It explicitly fails or withholds qualification when:

- the experiment ledger is invalid;
- treatment support exceeds configured complexity;
- logged propensities fall below the causal support floor;
- cross-fitted treatment predictions are incomplete;
- too few assignments have matured;
- mature outcomes remain unresolved at an unacceptable rate;
- treatment-specific effective sample size is too small.

Evidence is time-decayed so obsolete regimes do not dominate current decisions. Current-regime values are partially pooled back toward global evidence. Simultaneous confidence is adjusted across the treatment universe instead of reporting pointwise optimistic lower bounds.

The report also estimates supported prior-treatment -> current-treatment synergy from multi-step episodes. Unsupported transitions are never hallucinated as causal evidence.

## 3. Causal calibration plugs back into v0.28

`counterfactual_allocator_calibration.py` converts qualified causal effects relative to an explicit CONTROL arm into bounded calibration multipliers implementing the same protocol already consumed by `information_value.py`.

The intelligence ladder can therefore evolve without replacing the allocator:

```text
hand-designed information value
    -> realized observational yield
    -> randomized causal treatment evidence
    -> bounded causal multipliers
    -> exact multiple-choice research knapsack
```

No causal evidence means no causal multiplier. No CONTROL arm means no calibration.

## 4. Evidence-bound learning path planner

`learning_path_planner.py` uses only a qualified counterfactual report. It performs deterministic beam search over research-only intervention sequences under explicit step and budget ceilings.

Each step uses:

- simultaneous conservative treatment value;
- supported pairwise transition synergy when available;
- a tightly capped uncertainty bonus for purposeful research exploration;
- an explicit penalty for unsupported transitions.

The planner never executes a research action and cannot mutate runtime state. It emits a fingerprinted research path candidate that can feed existing challenger/evolution machinery.

## 5. Replication-gated breakthrough detection

`research_breakthrough.py` deliberately makes the word "breakthrough" expensive.

A candidate requires independent replication across configurable numbers of:

- experiments;
- datasets;
- time blocks;
- regimes.

Every replication must be selection-adjusted when required, retain a positive simultaneous lower bound, clear the median effect-size floor, and remain within the configured zero/low safety-regression budget.

A safety regression or unadjusted selection search blocks the breakthrough claim regardless of headline effect size. Only replicated `BREAKTHROUGH_CANDIDATE` reports may be persisted to the append-only breakthrough registry.

## Hidden seams sealed in v0.31

1. **Adaptive confounding** — the allocator changes the data it later learns from. Explicit assignment propensities create recoverable counterfactual support.
2. **Positivity collapse** — endless exploitation eventually removes evidence for alternatives. A propensity floor preserves research support.
3. **Treatment interference** — overlapping research actions violate single-action assumptions. Treatment bundles model the overlap as the treatment.
4. **Delayed-feedback censoring** — fast-resolving successes can dominate slow outcomes. Maturity horizons and minimum resolution rates expose the bias.
5. **Outcome-model leakage** — doubly robust correction requires externally cross-fitted predictions, not in-sample outcome models.
6. **Regime staleness** — evidence decays in time and current-regime estimates shrink toward global priors when support is thin.
7. **Sparse sequence overconfidence** — pairwise synergy is emitted only after sequence-specific assignment/ESS support.
8. **Repeated-search false discovery** — treatment lower bounds are simultaneous across the supported universe and breakthrough evidence must be selection-adjusted.
9. **Self-reinforcing research policy** — CONTROL arms and bounded causal multipliers prevent observational popularity from becoming proof of value.
10. **Breakthrough theater** — independent replication and safety firewalls separate genuinely repeatable research gains from one attractive run.

## Compounding research loop

```text
immutable accepted truth
  -> v0.28 information value
  -> v0.30 realized yield
  -> v0.31 supported research randomization
  -> mature evidence + cross-fitted outcome model
  -> doubly robust causal treatment values
  -> regime-aware / time-decayed shrinkage
  -> causal allocator calibration
  -> exact fixed-budget allocation
  -> evidence-bound research path
  -> challenger / OOF / untouched holdout
  -> independent replication
  -> BREAKTHROUGH_CANDIDATE registry
  -> new research hypotheses, never automatic production authority
```

## Authority boundary

v0.31 can autonomously improve research selection, preserve exploration support, estimate counterfactual learning efficiency, plan research-only intervention sequences, and surface replicated breakthrough candidates.

It does **not**:

- submit securities/options orders;
- activate a production model or policy;
- activate a live rollback;
- choose personalized live sizing;
- turn a breakthrough candidate into production authority;
- self-modify production code or live strategy semantics.

The system is allowed to become more aggressive at learning. It is not allowed to silently acquire more authority while doing so.
