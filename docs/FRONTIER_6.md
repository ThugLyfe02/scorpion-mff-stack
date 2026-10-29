# Frontier, sixth pass: four frameworks that are genuinely deep

The previous five passes found techniques that improve the system. These four are different in kind -
each is a *framework* that changes what the system is able to represent, and each is rare in trading
code not because it is impractical but because it requires knowing it exists.

All four are buildable from data we already hold. None needs the chain snapshot.

---

## 24. Rough volatility - the Hurst exponent is not 0.5  →  `rough.py`

**What it is.** Gatheral, Jaisson & Rosenbaum (2018): log-volatility behaves like fractional Brownian
motion with Hurst exponent **H ≈ 0.1**, not 0.5. Volatility is far rougher than any diffusion model
assumes, and that single fact explains the volatility smile's shape and skew's persistence better than
the models that preceded it.

**Why it is a hidden gem.** It is one of very few results that is simultaneously (a) empirically robust
across markets, (b) theoretically surprising, and (c) actionable, and it is almost entirely absent from
retail and semi-pro code because it requires knowing fractional calculus exists.

**What it changes for us.** Every volatility estimate in this branch implicitly assumes a memory length.
If H ≈ 0.1, then volatility mean-reverts *much* faster than a GARCH or an EWMA assumes, which means our
estimates are systematically stale - and a stale volatility estimate is a mispriced option, a wrong
barrier width, and a wrong sizing multiplier, all at once. Estimating H from our own bar data is a
ten-line calculation (regress log |r| on log horizon) and it would let us *choose* the memory length
instead of inheriting one.

**Acceptance test.** Estimated H on our bar corpus is reported with a confidence interval; the module
refuses when the sample is too short to distinguish H = 0.1 from H = 0.5, which is most likely for the
six bar sets we hold.

Sources: Gatheral, Jaisson & Rosenbaum, "Volatility is rough" (arXiv:1410.3394); Bayer, Friz & Gatheral
on pricing under rough volatility.

---

## 25. Peaks-over-threshold - the tail is not the worst observation  →  `tails.py`

**What it is.** Extreme value theory: fit a generalised Pareto distribution to the exceedances over a
high threshold. The Pickands-Balkema-de Haan theorem says the tail converges to that family regardless
of the underlying distribution - so the tail can be *estimated*, not extrapolated from a maximum.

**Why it matters here specifically.** `ruin.py` found the corpus's worst trade is **−0.39R** and
concluded there are no total losses. That is not a finding about the world; it is a finding that the
sample is small and does not contain a −100% option expiry. A long 0DTE position's commonest terminal
outcome is −1.00R, and the distribution that generates it has a tail our 107 trades have not sampled.
EVT gives the first honest estimate of how bad the unsampled part is, with a shape parameter that tells
us whether the tail is bounded or heavy.

**What it changes.** `p_down` becomes a function of an estimated tail rather than of the observed one.
The 78.1% breakeven haircut may survive - or may be an artefact of seven months without a −100% trade.

**Acceptance test.** Fitted shape parameter reported with standard error; the fitted tail is compared
against the empirical maximum, and where the fit predicts losses beyond −1.00R the module says so
explicitly rather than clipping at the corpus boundary.

Sources: McNeil, Frey & Embrechts, *Quantitative Risk Management*, ch. 7; Pickands (1975);
Balkema & de Haan (1974).

---

## 26. Hawkes processes - alerts are not independent  →  `hawkes.py`

**What it is.** A self-exciting point process (Hawkes 1971): each event raises the intensity of future
events, which then decays. The intensity is

    lambda(t) = mu + sum over past events of alpha * exp(-beta * (t - t_i))

**Why it matters for this desk and no other.** Our input is a Discord channel, and Discord channels fire
in bursts: one alert begets three more. That means the arrival rate carries information the individual
alert does not - a burst is a regime signal, and a burst is also exactly when our two-slot concurrency
limit binds and we start refusing trades. So bursts change both what we take and what we think.

**What it changes.** Three concrete things: (1) a burst-intensity feature that can gate or upweight an
entry; (2) an honest measurement of how often the concurrency cap binds *because* of clustering rather
than because of opportunity; (3) a branching ratio alpha/beta < 1 test - above 1 the process explodes,
and a channel whose alerts are self-perpetuating should be weighted differently from one that reacts to
the market.

**Acceptance test.** Fitted branching ratio per channel with a standard error; the model's predicted
inter-arrival distribution is compared against the observed one, and the fit is refused where the
branching ratio is not significantly below one.

Sources: Hawkes (1971); Bacry, Mastromatteo & Muzy, "Hawkes processes in finance" (arXiv:1502.04592).

---

## 27. Optimal-transport drift detection - knowing when the world moved  →  `drift.py`

**What it is.** The Wasserstein distance between two empirical distributions, computed by solving a
transport problem (or approximated by its one-dimensional closed form). Unlike a KS test it respects the
*magnitude* of the shift, not just whether the distributions differ, which is what a sizing decision
needs.

**Why it is the missing guard on everything built in `experts.py`.** The allocator learns from recent
trades. Nothing in it knows whether those trades still resemble the corpus it was calibrated on. A
weight that drifts because the evidence changed is adaptation; a weight that drifts because the *world*
changed is decay, and they look identical in the weight path. Wasserstein distance between the current
window of trade outcomes and the corpus distribution is the cheapest honest separation of the two - and
above a threshold the correct response is not to reweight, it is to stop and demand new calibration.

**What it changes.** `experts.py` gains a precondition: blend only while the window is in-distribution;
otherwise report drift and refuse to allocate. That is the difference between a self-evolving system and
one that evolves itself off a cliff.

**Acceptance test.** On resampled in-distribution windows the distance stays below the threshold; on a
synthetically shifted window it exceeds it and the guard refuses; the threshold is reported with the
false-positive rate measured on in-distribution data, not asserted.

Sources: Villani, *Optimal Transport: Old and New*; Peyré & Cuturi, "Computational Optimal Transport"
(arXiv:1803.00567).

---

## 28. Path signatures - the shape of a move, as a finite feature  →  `signature.py`

**What it is.** Rough path theory (Lyons, 1998): the iterated integrals of a path form a *signature* that
characterises it uniquely up to tree-like equivalence, truncated at level N to a finite feature vector.
The first level is net displacement; the second is signed area (lead-lag); the third captures curvature.

**Why this is the deepest item in the whole series.** It is the only construction here that turns a
*path* into a finite set of features with a mathematical guarantee about what is and is not lost, and it
does so without any model of the process. Every feature we use today - return, range, MFE, time in trade
- is a projection of the path that discards its shape. A signature keeps the shape.

**What it would change.** Entry quality becomes a function of how the price arrived, not just where it
went: a move built on three sequential pushes with shrinking participation signs differently under a
signature than a single spike. This is the correct feature set for any classifier of "is this alert
tradeable", and it composes with `decay.py` (the signature of the first five minutes predicts the
horizon return) rather than replacing it.

**Honest cost.** A level-3 signature of a 2-dimensional path is 14 features; level 4 is 30; they grow
fast, and with 107 trades any of them will overfit instantly. This is listed as research with a hard
gate: it may not ship a model until the corpus is large enough, and `deflated.py`/`pbo.py` measure
exactly that.

**Acceptance test.** Signature levels 1-3 computed on a known path match hand-calculated values (net
displacement, signed area) exactly; the feature count formula is asserted; the module refuses to fit
anything on a corpus below the stated minimum.

Sources: Lyons (1998), "Differential equations driven by rough signals"; Chevyrev & Kormilitzin, "A
primer on the signature method in machine learning" (arXiv:1603.03788).

---

## What to build, and in what order

| Item | Depends on | Why this rank |
|---|---|---|
| `rough.py` | bars | cheapest, and it invalidates every volatility estimate we hold |
| `drift.py` | trade outcomes | it is the safety precondition for `experts.py`, which now allocates |
| `tails.py` | trade outcomes | re-tests the 78.1% breakeven against an estimated tail rather than an observed one |
| `hawkes.py` | alert timestamps | explains a refusal we currently attribute to policy |
| `signature.py` | bars | deepest, and the only one that must wait for the corpus to grow |

The ordering rule is the one from the roadmap, applied again: **the two that audit existing claims come
before the two that add capability**, and the one that needs more data comes last rather than first.

One correction this pass forces on the branch: `ruin.py`'s "no total losses" is a statement about a
sample, not about the instrument. A long option's terminal distribution has mass at −100% and our
seven months have not sampled it. That sentence should not appear in a report again without the EVT
tail beside it.
