# Frontier, seventh pass: the path signature, built and then gated

The previous six passes added measurements and guards. This one adds the one object the whole
branch has been dancing around: a representation of the *shape* of a move, not just where it
went. It is the deepest item in the series, it is now built, and - because this branch does not
ship a number a decision cannot trust - it ships with a gate that refuses to let it be used on
a corpus that is still too small.

---

## 29. Path signatures - the shape of a move, as a finite feature vector  →  `signature.py`

**What it is.** Rough path theory (Lyons, 1998): the iterated integrals of a path form a
*signature* that characterises the path up to tree-like equivalence, truncated at level N to a
finite feature vector.

* Level 1 is net displacement - the same information as a return.
* Level 2 adds the signed (Levy) area - the lead-lag relationship, the fact that two moves
  happened in sequence rather than at once.
* Level 3 adds curvature - whether the path bent once, twice, or wound back on itself.

Every feature used elsewhere in this branch (return, range, MFE, time in trade) is a projection
that discards shape. A signature keeps it, with a mathematical guarantee about what is and is
not lost at the chosen truncation, and with no model of the process underneath.

**Why it was deferred and why it is not deferred now.** FRONTIER_6 listed it last with the honest
cost: 14 features on 107 trades overfits instantly. That cost has not changed - so the build is
the computation plus the gate, not the computation plus a classifier. The computation had to
exist before anything could consume it; the gate is the part that stops it being consumed badly.

**What was built, exactly.**

`signature(path, level=N)` computes the level-N signature of a piecewise-linear path *exactly*.
A segment with increment Δ contributes the tensor exponential `exp(Δ) = Σ Δ^{⊗k}/k!`, and
segments compose by the truncated concatenation product. No quadrature, no approximation, no
random number - the values of a hand-drawn path come out of a closed form a person can check
with a pencil, and the tests do exactly that.

`lead_lag(series)` is the canonical two-dimensional path of a univariate price series: the lead
coordinate moves first, the lag coordinate catches up, so successive moves are `(Δ, 0)` then
`(0, Δ)`. Its Levy area is **half the realized quadratic variation** of the series - a quantity
the branch has never been able to name from its own bars before.

`gate_fit(samples, dim, level)` is the honest gate. It computes the non-constant feature count,
applies a floor of 30 samples plus ten per feature, and refuses with a named reason below it.
That is a floor, not a proof - the docstring says the deflation and PBO machinery still applies.

**Acceptance, run for real.**

```
scorpion signature                       # the unit square, level 3
  signature dim=2 level=3 features=15 (non-constant 14)
  level 1 (net displacement): 0: 0, 1: 0
  levy area: 1
  fit gate: REFUSED - 14 non-constant signature features need >= 140 independent
            samples before any fit; the corpus has 107
```

```
scorpion signature --path series.json --level 2   # [0, 2, 5, 1] via lead-lag
  signature dim=2 level=2 features=7 (non-constant 6)
  level 1 (net displacement): 0: 1, 1: 1
  levy area: 14.5
  fit gate: permitted - 6 non-constant features on 107 samples: fit permitted
```

The pinned tests assert the exact values, not just that the code runs:

* a single segment `(0,0)->(2,3)` reproduces the tensor exponential `2^3/6`, `2^2·3/6`,
  `2·3^2/6`, `3^3/6` at level 3, to the last decimal;
* level 1 is net displacement whatever the route, and level 2 sees the corner that displacement
  cannot: the L-shaped path carries `S_01 = a·b` where the straight path carries `a·b/2`;
* the Levy area of an arbitrary polygonal path equals the signed area of that polygon, checked
  against an independent oracle, clockwise negative and counter-clockwise positive;
* the unit square is `+1` one way and `-1` the other;
* the lead-lag path alternates lead-then-lag by construction, and its Levy area equals half the
  realized quadratic variation - pinned, not assumed.

## What it does not claim

* A signature identifies a path only up to tree-like equivalence. A spike and a smooth ramp
  with the same iterated integrals are indistinguishable at the chosen truncation, and the
  module says so in its own caveats rather than discovering it later in a report.
* The fit gate is a necessary condition, not a sufficient one. A permitted level-2 fit on 107
  trades still needs `deflated`/PBO machinery before any coefficient leaves the module.
* No model ships here. The module computes and refuses; it does not classify "is this alert
  tradeable". That is the next consumer, and it is gated by the same feature-count arithmetic
  this module now owns.

## Why this is the seventh pass and not a detour

The roadmap's rule was "data before analysis", and the branch has spent three passes building
the data plane and the guards around it. This is the first module that turns a *path* into a
finite set of features with a guarantee, and the first that can name the lead-lag relationship
from the bars we already hold. It is the correct feature set for the eventual "is this alert
tradeable" classifier, and it now exists in a form where the answer to "can we fit it yet" is a
function of the corpus, not of enthusiasm.
