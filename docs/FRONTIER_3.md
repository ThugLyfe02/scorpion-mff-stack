# Frontier, third pass: the statistics that keep us honest about our own numbers

The first two passes found techniques that make the system better. This one finds techniques that make
our *claims* weaker — which is worth more, because this branch has been quoting numbers without the
corrections a professional would demand.

The uncomfortable starting point: `attribution.py` compares eight variants against one corpus and sorts
them by delta. The top of that table is a **selection**, and reporting it as "the best rule" without a
multiple-testing correction is the exact error the techniques below exist to prevent.

---

## 11. Deflated Sharpe Ratio  →  `deflated.py`

**What it is.** Bailey & Lopez de Prado (2014). A backtest Sharpe is inflated by three things at once:
how many configurations were tried, non-normal returns, and the length of the sample. The DSR gives the
probability that the *true* Sharpe exceeds zero after deflating for all three.

**Why it is the most important item in this document.** Every performance figure this branch reports -
+87.59R on the corpus, the 78.1% breakeven win haircut, the +1.212R mid-premium band - is a maximum over
some set of choices. A maximum over k noisy estimates is biased upward by roughly the spread times
sqrt(2 ln k). With eight attribution variants that correction is not cosmetic.

**Implementation notes.** Needs: the number of independent trials actually run (we know it - it is the
variant count), the variance of the Sharpe estimates across trials, sample length, skew and kurtosis.
Available in the `deflated-sharpe` package; implement in-house so the trial count is explicit rather
than defaulted.

**Acceptance test.** A strategy with a high raw Sharpe selected from many trials returns a low DSR; the
same Sharpe as the only trial returns a high one. Our own premium-band finding must be re-stated with
its DSR, and if the DSR is low the report says so at the top rather than in a footnote.

Sources: Bailey & Lopez de Prado (2014); the `deflated-sharpe` implementation; rollbrains' worked
example of why a 2.5 Sharpe is not evidence on its own.

---

## 12. Probability of Backtest Overfitting via CSCV  →  `pbo.py`

**What it is.** Bailey, Borwein, Lopez de Prado & Zhu. Split the performance matrix of N strategies over
T periods into all combinatorially symmetric train/test halves, rank strategies in-sample, and measure
how often the in-sample winner falls below the out-of-sample median. That frequency *is* the probability
that the selection procedure is overfitting.

**Why we need it.** The attribution matrix is a small performance matrix: 8 variants x 107 trades. CSCV
answers the question the deltas cannot: "if I had chosen on the first half, would I have chosen the same
rule?" If the answer is often no, the ranking is noise and the table should not be read as a ranking.

**Refusal that matters.** With 8 strategies and 107 trades there are C(107, 53) splits, which is
astronomically many; the implementation must sample a bounded number of splits (the standard practice)
and say how many it used, or the number is unreproducible.

**Acceptance test.** On synthetic strategies with no real edge, PBO is high (near 0.5 or above); with one
genuinely dominant strategy, PBO is low. Both directions asserted, not just the first.

- Sources: Bailey et al. (2014) via ScholarWorks; the CRAN `pbo` package; corr.ai's definition
  (in-sample winner below out-of-sample median).

---

## 13. Triple-barrier labels instead of fixed-horizon returns  →  extends `markout.py`

**What it is.** Label each entry by whichever barrier is touched first: an upper profit barrier, a lower
stop barrier, or a vertical time barrier. This is how path-dependent outcomes are labelled for any model
that has to decide *when* to act, and it is the standard precursor to meta-labelling.

**Why we need it.** Every label in this branch is a fixed horizon or a claimed exit. A trade that touched
+50% before collapsing to zero is labelled a loss here, and a trade that never moved is labelled by a
horizon it happened to cross. Triple-barrier labels encode the path, which is exactly what an exit policy
needs to learn from - and it makes the corpus's own "sold at 75%" claims comparable across trades.

**Implementation notes.** Barriers in units of the entry premium, vertical barrier at the session close.
Ties are broken toward the vertical barrier, and the resolution order must be stated because it changes
the label distribution.

**Acceptance test.** A path that touches the upper barrier then falls is labelled upper; one that touches
lower then recovers is labelled lower; the label distribution is reported alongside the claimed outcome
distribution so the disagreement between them is visible.

Sources: mlfinpy `Labelling` docs (reference implementation), quantengines and axlfi write-ups on
triple-barrier and meta-labeling.

---

## 14. Inventory-skewed quoting - the one we should *not* build yet

**What it is.** Avellaneda-Stoikov (2008): a risk-averse market maker quotes a spread around a
reservation price that shifts with inventory, with width proportional to risk aversion times volatility
and time remaining.

**Why it is listed and deferred.** It is beautiful and completely inapplicable: we take liquidity, we do
not post it. Building quote skew now would be the most sophisticated code in the repository and worth
nothing. It becomes relevant only if item 4 of the first pass (POV exit scheduling) turns into passive
exits, at which point the reservation-price formula is the right frame for where to rest an order.

Recording the deferral, with its trigger, is the discipline: a good idea that does not fit today is not
a good idea to build today.

Sources: Seb-L-P/avellaneda-stockson-market-maker, kayasolomon/market-making (formulas pinned to hand
calculations), thibault-charbonnier/market-making-engine (C++ with inventory skew).

---

## What this pass changes about the branch

Two of these are corrections to claims already made, and that is the point:

| Claim in this branch | What it needs |
|---|---|
| "+1.212R in the mid premium band" | a DSR over the 5 bands tested |
| "attribution ranks the variants" | a PBO over the 8-variant matrix |
| "the corpus says this trade lost" | a triple-barrier label, because the path may disagree |

The sequence here is therefore different from the first two passes: `deflated.py` and `pbo.py` are not
new capability, they are **audits of capability already claimed**. They should land before any further
performance number is published, including in the commit messages of this branch.

That is the least glamorous and most professional thing in the whole document.
