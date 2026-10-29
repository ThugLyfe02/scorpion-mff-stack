# The tournament: does a rule survive the next unseen block?

Attribution answers "what did this rule cost on the corpus?" That is a necessary number and a
biased one: the rule was chosen after the whole path was visible. The tournament asks the harder
question a desk actually faces before changing a config:

> if I select the best variant on what I knew yesterday, does it still win on what happens
> tomorrow?

The answer is not a P&L. It is a verdict, the folds that produced it, the selection audit, and
the refusals that would have made a weaker system print a number anyway.

## Run it

```bash
scorpion tournament --folds 4 --bootstrap-draws 500
scorpion tournament --all-variants --json
```

Each variant is a full replay through the production executor (parser, risk, authorization,
outbox, paper fill, settlement, applied log). Folds then partition those same trades by entry
day, so the cost is one replay per variant, not one replay per fold.

## The method, in order

1. **Chronological folds.** The days are split into `folds + 1` contiguous blocks. The first
   block trains only. Each later block is a test set and every earlier day is training.
2. **Purge.** Training days within `purge_days` of the test start are removed, because a trade
   entered just before the boundary can settle after it.
3. **Embargo.** Test days within `embargo_days` of the boundary are removed when configured, so
   the first unseen decision is not contaminated by the last training decision.
4. **Selection.** On each fold, the variant with the highest mean realised R on the training
   block is selected. Ties are broken by name, so the result is deterministic.
5. **Out-of-sample score.** The selected variant, unchanged, is applied to the unseen block.
6. **Deflated Sharpe.** The selected out-of-sample series is priced against the best-of-N
   selection effect, using the Bailey/Lopez de Prado deflated Sharpe. With one trial the
   expected maximum is zero; with N trials it is positive, so the observed Sharpe must clear
   that bar.
7. **PBO.** Combinatorially symmetric cross-validation chooses on half the contiguous blocks and
   ranks the choice on the other half. The fraction of splits where the selected variant lands
   below the out-of-sample median is the probability of backtest overfitting.
8. **Paired block bootstrap.** The selected-minus-baseline difference is resampled in short
   blocks, preserving some dependence. The report gives a 95% interval and a one-sided p-value.
9. **Verdict.** `SUPPORTED`, `FRAGILE`, `REJECTED`, `INCUMBENT_HELD`, or `REFUSED`, with named
   reasons. Insufficient trades, insufficient days, or no eligible fold is a refusal, never a
   small-sample result with a decimal point.

A refused trade is scored **0R** (no capital deployed). That is the honest choice for comparing
policies: the policy avoided the position, not the opportunity. Coverage is printed separately so
a variant cannot look good merely by taking fewer trades.

## What the shipped corpus says

Run on 2026-10-27 with the five default challengers and four folds:

```text
verdict INCUMBENT_HELD  edge +0.0000R/trade  oos +63.60R vs baseline +63.60R

fold  test first  selected             train R    test R    base R
   1  2026-05-27  baseline              +1.473    +1.192    +1.192
   2  2026-06-29  baseline              +1.315    +0.899    +0.899
   3  2026-07-29  baseline              +1.189    +0.527    +0.527
   4  2026-08-19  baseline              +1.014    +0.458    +0.458

deflated Sharpe 1.000 (SR +0.433, expected max +0.138, trials 6)
PBO 0.000 over 70 splits  paired edge +0.0000R  95% [+0.0000, +0.0000]  p=1.0000
reasons incumbent_selected_every_fold
```

That is the useful result: **the incumbent beat every challenger in every fold**. The pipe test,
the wider gate, removing the markup, a 10% worse exit, and 20% entry slippage were all worse on
the unseen block. The table does not prove the baseline is optimal; it proves those five changes
did not earn promotion on this corpus. The edge is exactly zero because the selected path is the baseline itself.

## What it refuses

| Situation | Verdict | Why |
|---|---|---|
| fewer than `min_total_trades` | `REFUSED` | the corpus cannot support a comparison |
| no fold has enough train or test trades | `REFUSED` | selecting on nothing is not a test |
| out-of-sample edge ≤ 0 | `REJECTED` | the challenger did not beat the incumbent |
| positive edge, deflated Sharpe below 0.95 | `FRAGILE` | the selection effect can explain it |
| positive edge, PBO above 0.50 | `FRAGILE` | the winner does not survive the split audit |
| positive edge, paired p-value ≥ 0.05 | `FRAGILE` | the difference is not distinguishable from zero |
| every fold selects the baseline with zero edge | `INCUMBENT_HELD` | the safe conclusion is "do not change" |

The exit code is 0 for `SUPPORTED` and `INCUMBENT_HELD`, 1 for the rest. A cron that treats
`FRAGILE` as success would promote a rule the data does not support.

## Acceptance

- `tests/test_tournament.py` pins fold ordering, purge, DSR response to trial count, PBO in both
  directions, the paired bootstrap, the supported and incumbent-held verdicts, refusal on a thin
  corpus, and refusal on an incomplete replay.
- The pure statistics are tested without a database; the production wiring is tested with a
  monkeypatched replay so the fold logic is not hidden behind a slow corpus run.
- `docs/ROADMAP_20K.md` phase 2.2 is the attribution baseline this builds on. The next honest
  step is not another variant; it is bars for the 97 contract-days whose absence blocks the
  measurement this tournament would most like to price.

## Caveats, stated where they can be read

- Folds are chronological, but the corpus is small and not a random sample of future days.
- A refused trade is scored 0R; that measures capital avoidance, not opportunity cost.
- Deflated Sharpe assumes the trial count is the variants in this run. A larger search needs a
  larger trial count, and the report says the number it used.
- The bootstrap preserves short blocks of dependence, not regime changes.
- PBO is a selection diagnostic, never a guarantee of future performance.
