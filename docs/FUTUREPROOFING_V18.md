# Scorpion v0.18 — selection-safe qualification

v0.18 hardens the points where model/parser quality can look stronger than the evidence actually supports. The focus is not another model family. It is preventing threshold search, incomplete labels, and narrow evaluation subsets from manufacturing apparent quality.

## 1. Multiplicity-adjusted selective prediction

A selective classifier commonly searches many confidence thresholds and keeps the widest-coverage threshold whose observed error bound passes. Using an ordinary pointwise Wilson interval at every searched threshold understates uncertainty because the threshold itself was selected after looking at the same outcomes.

`selective.py` now supports two valid qualification paths.

### Same-sample threshold search

When only one labeled sample is available:

- every distinct confidence threshold inspected is counted;
- the normal-equivalent Wilson critical value is Bonferroni-adjusted across the full threshold family;
- `simultaneous_error_upper_95` is calculated for every risk/coverage point;
- policy selection uses the simultaneous bound, not the attractive pointwise bound;
- the resulting `SelectivePolicy` records the number of thresholds searched and familywise alpha.

Adding more candidate thresholds therefore cannot make the claimed risk guarantee easier to satisfy.

### Independent holdout certification

When `validation_observations` is supplied:

1. the discovery sample selects a threshold;
2. that threshold is frozen;
3. the untouched validation sample evaluates the threshold exactly once;
4. accepted count, coverage, and Wilson risk bound reported by the final policy come from validation evidence.

This is preferred when enough labels exist because it separates threshold discovery from the risk claim.

## 2. Actionable contract truth is mandatory

Parser qualification previously allowed an actionable kind label such as `ENTRY` to exist without corresponding contract truth. That can reward a parser for saying “ENTRY” while identifying the wrong or fabricated option contract.

`run_parser_tournament()` now measures:

- actionable labeled samples;
- actionable contract-labeled samples;
- actionable contract-label coverage;
- missing actionable contract labels;
- contract-label errors among covered actionable samples.

An actionable expected kind with an absent, blank, or `None` contract label is incomplete supervision and is a qualification failure. For non-actionable examples, an explicit `None` contract remains valid truth meaning “no contract should be produced.”

Default required actionable contract coverage is 100%.

## 3. Evaluation-set coverage is now a gate

A candidate can also look excellent if only the easiest fraction of the tournament corpus is labeled. v0.18 therefore adds:

- `min_label_coverage`, default 80%;
- `min_distinct_labeled_kinds`, default 2;
- `label_coverage` and `distinct_labeled_kinds` to every `CandidateScore`.

This does not claim that two classes are sufficient for every production decision. It prevents obviously degenerate one-class or sparsely labeled evaluation sets from silently qualifying under default policy. Research workflows can require broader class coverage through `TournamentCriteria`.

## 4. Why this matters to trading performance

These gates remove three common sources of false edge:

1. **threshold shopping** — repeatedly checking confidence cutoffs until one appears safe;
2. **instrument-blind accuracy** — rewarding correct action type while contract identity is unknown;
3. **easy-subset evaluation** — measuring only the examples that were convenient to label.

All three can produce apparently excellent offline metrics while making live decisions worse.

## 5. Composition with v0.17

v0.18 sits upstream of the v0.17 economic-safety gates:

```text
complete / representative supervision
        ↓
selection-safe confidence threshold
        ↓
parser / model challenger qualification
        ↓
chronological OOS evidence
        ↓
conditional regime safety
        ↓
cost + turnover adjusted value
        ↓
operator review
```

No layer in this chain grants deployment or brokerage authority.

## 6. Authority boundary

The changes remain evaluation and governance infrastructure. They do not:

- submit unattended securities or options orders;
- select personalized live position size;
- activate a challenger automatically;
- activate rollback automatically;
- modify locked MFF execution semantics.
