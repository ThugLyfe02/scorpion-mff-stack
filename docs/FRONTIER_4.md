# Frontier, fourth pass: the world-class and underrated

Three passes so far pulled from the familiar anglophone canon. This one goes looking elsewhere - Paris,
London, Rome - for techniques that are standard in the places that take execution seriously and
comparatively rare in retail-adjacent code. Each is cheap to implement, each invalidates something we
currently assume, and one of them **replaces a whole class of problem we have been fighting badly**.

---

## 15. The square-root law of market impact  →  `impact.py`

**What it is.** Bouchaud and co-authors (CFM / Imperial): the average price change over a metaorder of
size Q, for daily volume V and volatility σ, scales as

    I ≈ Y · σ · sqrt(Q / V)      with Y ≈ 0.5-1

Not linear in size. Square root. It has been replicated across equities, futures, options and crypto, in
different countries and decades, and it is arguably the most robust empirical law in market
microstructure.

**Why it is underrated outside a handful of desks.** Everyone learns Kyle's linear lambda because it is
tractable and fits in a regression. The square-root law is *empirically* better and *practically* more
useful, but it does not come out of a simple closed-form model, so it is taught less and used less.

**What it changes for us.** Our sizing thinks in premium dollars. The square-root law says the marginal
cost of doubling a clip is only √2, not 2× - which means a 40-contract exit costs roughly 6× a 1-contract
exit in impact, not 40×. Two consequences, both actionable:

* our current caution about size may be **too** cautious, which is the opposite of the intuition the
  linear model gives;
* the cost is a *power law*, so a bad day (high σ, low V) inflates it non-linearly - which is precisely
  when a halt rule should bind.

**Implementation notes.** Estimate Y from our own executions once the venue lands: regress realised
impact against σ·sqrt(Q/V). Until then, ship the formula with Y as a stated parameter and refuse to
pretend it is measured.

**Acceptance test.** Impact per contract falls as clip size rises (concave, not linear) on the fitted
curve; doubling Q increases predicted impact by sqrt(2) within tolerance; a low-volume, high-volatility
session predicts materially more impact than a calm one.

Sources: Bouchaud's own write-up of the law (2024), CFM/Imperial arXiv:1811.05230 on the linear-to-
square-root crossover, Bouchaud's FIPS 2018 slides on metaorder impact.

---

## 16. Conformal prediction: intervals with a guarantee  →  `conformal.py`

**What it is.** Vovk and co-authors (London): given any point predictor and a calibration set, conformal
prediction produces intervals with **finite-sample, distribution-free coverage** - the true value falls
inside with probability at least 1-α, no normality assumption, no model assumption.

**Why this is the sleeper.** Every uncertainty figure in this branch is either a standard error (assumes
a distribution we do not have) or a bootstrap (assumes exchangeability we cannot verify). Conformal
gives a guarantee that survives non-normality, fat tails and small samples - the three things our data
actually has.

**What it changes.** Expected R stops being "1.212 ± something" and becomes "1.212, with an 80% interval
of [-0.4, +2.9] on 40 trades". That is the form a sizing decision can use, because the lower bound is
what determines whether a clip survives a bad draw.

**Implementation notes.** Split conformal is trivial and adequate: calibrate on part of the corpus,
produce intervals for the rest, report empirical coverage versus nominal. Refuse when the calibration
set is smaller than 1/α observations, because the guarantee does not hold below that.

**Acceptance test.** Empirical coverage on held-out trades is at least the nominal level within
binomial error; the method reports the calibration size; an under-sized calibration set is refused
rather than silently producing a tight interval.

Sources: vertoxquant's applied write-up; the `neural-trader-predictor` conformal crate for an
implementation reference; Vovk et al. as the underlying theory.

---

## 17. Prediction with expert advice - the answer to our selection problem  →  `experts.py`

**What it is.** Cesa-Bianchi, Lugosi, Herbster and others (Rome, London): instead of picking the best
strategy in hindsight, run all of them and combine their outputs with weights updated by their realised
loss. The guarantee is a **regret bound** - your cumulative loss is within O(sqrt(T log K)) of the best
single expert in hindsight - and the "tracking the best expert" variant keeps that bound even when the
best expert *changes over time*.

**Why this is the most important item in this document.** FRONTIER_3 concluded that our attribution table
is a selection, and that a Deflated Sharpe or a PBO is needed to say whether the winner is real. Both of
those are *corrections to a bad procedure*. This replaces the procedure: you do not select a rule and hope
the selection was not overfit, you run the rules and let the weights follow evidence, with a bound that
holds for any sequence of losses - adversarial, non-stationary, regime-switching, all of it.

For a desk whose edge decays (see item 10) and whose regimes shift (item 8), a method that adapts online
with a guarantee is strictly better than a method that chooses once and is audited afterwards.

**Implementation notes.** Exponential weights (Hedge) over the K variants, loss = negative realised R per
trade, learning rate from the standard sqrt(8 ln K / T) bound; add a fixed share of uniform weight so a
genuinely new regime can recover. Report the weights over time - they are the most honest picture of
which rule the data currently supports.

**Acceptance test.** On a synthetic sequence where the best expert switches halfway, the combined
strategy's regret stays within the bound and its weight mass moves to the new leader within a stated
number of trades; on a stationary sequence the weights converge to the best expert; on pure noise the
weights stay near uniform rather than over-committing.

Sources: Bartlett's Berkeley lecture notes on regret; UVic CSC 482A notes on O(sqrt(T log K)); Herbster's
"tracking the best expert" for the non-stationary variant.

---

## 18. Volume and dollar bars instead of time bars  →  extends `bars.py`

**What it is.** Sample by traded volume (or dollar volume) rather than clock time. Time bars oversample
quiet periods - which is where nothing happens and where autocorrelation is worst - and undersample the
violence.

**Why it matters here more than usual.** Our bars are five-minute. A 0DTE opening thirty minutes is not
the same market as the same contract at noon, and five-minute sampling spends most of its rows on the
quiet part. Volume bars put equal information in each row, which is what every downstream statistic in
this branch quietly assumes.

**The honest caveat.** Dollar bars need volume data, and the shipped bar sets carry volume only
sometimes. Where it is missing, time bars remain, and the module must say which sampling each set uses -
a mixed store is fine as long as the mixing is visible.

**Acceptance test.** Rebuilding a session into volume bars produces rows with more equal return variance
than the equivalent time bars on the same data; the sampling method is recorded per set; a set without
volume is labelled `time` rather than silently treated as volume-sampled.

Sources: smokinaces.ai on volume/dollar bars and the clock problem; mlfinpy `FinancialDataStructure`
(dollar bars, tick bars, volume bars).

---

## 19. Guéant-Lehalle-Fernandez-Tapia - the tractable cousin, still deferred

**What it is.** Paris, 2011: a closed-form (or near-closed-form) solution to the market-making problem
with inventory risk, where Avellaneda-Stoikov needs numerical solution. Same family, far easier to
implement.

**Status: still deferred, for the same reason as item 14** - we take liquidity. Recorded here because it
is the version we would build if we ever post, and because its inventory-skew term is the right shape for
a hedged options book even away from quoting.

Sources: arXiv:1105.3115; quantmemo's treatment of the paper.

---

## What this pass changes

| Item | What it invalidates in the branch today |
|---|---|
| Square-root impact | the implicit linearity of "cost scales with contracts" |
| Conformal intervals | every mean ± stderr in mark-out, decay, attribution |
| Expert advice | the whole select-a-winner-then-audit-it workflow |
| Volume bars | the assumption that a bar is an equal unit of information |

The last two are the ones worth arguing about. Volume bars change what every statistic is computed on;
expert advice changes what a strategy *is* - from a rule that won a backtest to a weighting that
adapts with a bound.

**Sequence.** `experts.py` first, because it subsumes the selection problem that items 11 and 12 were
built to audit, and because it needs nothing we do not already have: the attribution machinery already
produces a per-trade R for each variant, which is exactly the loss sequence the algorithm consumes.
Then `conformal.py`, then the square-root impact parameter once real fills exist, then volume bars when
the bar store carries volume.

One sentence for the reader who skips everything else: **we have been choosing rules and then auditing
the choice, when the literature has a method that never has to choose.**
