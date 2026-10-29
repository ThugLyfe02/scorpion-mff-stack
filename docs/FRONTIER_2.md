# Frontier, second pass: four constants replaced by four solved quantities

The first pass covered execution mechanics. This covers decision theory, where the larger gains are -
not because the ideas are more advanced, but because they replace rules we currently hard-code with
rules we solve for.

---

## 7. Exits as an optimal stopping problem, solved by Longstaff-Schwartz  →  `exit_policy.py`

**What it is.** Longstaff & Schwartz (2001) price early-exercise claims by regressing realised
continuation values on state variables and exercising where intrinsic beats continuation. The same
machinery solves a trading exit: given the paths we observe, what is the exercise boundary?

**Why this is the largest remaining idea.** Every exit in this branch is a rule: sell at 25%, at 90% of
claim, at the close. An optimal-stopping boundary is not a rule - it is the solution to "given the
distribution of forward paths, when is holding worth less than selling". The corpus already contains the
paths and the exits, so LSM can say whether his exits were near-optimal and what boundary our own paths
support.

**The honest risk.** LSM is prone to look-ahead bias when the regression uses the same paths it
evaluates on. The implementation must split train and evaluate and report the OUT-OF-SAMPLE boundary,
or it will find an exit rule that is optimal precisely for the past.

**Acceptance test.** On simulated geometric paths with a known boundary, recover it within tolerance; on
held-out paths the LSM policy beats a fixed-percentage exit or reports that it does not.

Sources: Longstaff & Schwartz via the Michigan optimal-stopping notes and the Oxford Advanced Monte
Carlo module; arXiv:2208.00765 for the LSMC formulation.

---

## 8. Bayesian online change-point detection, run-length posterior  →  `bocpd.py`

**What it is.** Adams & MacKay (2007) maintain a probability distribution over run length - time since
the last change - updated online with each observation.

**Why it beats the CUSUM just built.** CUSUM answers "has it changed" with a threshold. BOCPD answers
"how likely is it that a change happened n bars ago", which is a quantity a position size can be a
function of. It also degrades gracefully: an ambiguous stretch produces a spread-out posterior rather
than a binary silence.

**Acceptance test.** On a planted change the posterior collapses onto the true run length within a few
bars; on stationary data it stays diffuse; the two are separable by a reported statistic, not by eye.

Sources: Adams & MacKay (2007) via the UAI 2020 tutorial; hildensia/bayesian_changepoint_detection
(conjugate Normal-Gamma); Tugbars/BOCPD (fast recursion).

---

## 9. Drawdown-constrained Kelly, not fractional Kelly  →  extend `ruin.py`

**What it is.** Fractional Kelly (half, quarter) is a convention. The drawdown-constrained problem has a
proper treatment: Grossman-Zhou style rules that shrink leverage as drawdown deepens, and Boyd's convex
bound that satisfies a drawdown constraint by construction rather than approximation.

**Why it matters here.** `ruin.py` recommends `kelly / 4` with a caveat. That number is a habit. A
Grossman-Zhou rule is a function of current drawdown, which is the shape a governor should have: the
further underwater, the smaller the bet, with the constraint guaranteed rather than hoped for.

**Acceptance test.** The recommended fraction decreases monotonically with drawdown; simulated P(halt)
under the rule stays below the configured bound across seeds; the rule is compared with flat fractional
Kelly on the same corpus.

Sources: Stanford Boyd risk-constrained Kelly (convex drawdown bound); sergeisukhovmkt/The-Bayesian-
Grossman-Zhou-Rule; arXiv:1710.01787 on Kelly's limitations.

---

## 10. Alpha decay: the half-life of a signal  →  `decay.py`

**What it is.** The information coefficient between a signal and forward returns, measured at several
horizons, and the half-life implied by its decay. Standard practice at systematic funds, and the number
that says whether an edge is a day trade or a hold.

**Why it matters for a Discord desk.** The corpus is human-typed alerts. If IC at +5 minutes is high and
at +60 minutes is zero, every minute of implementation latency is eating the edge - which is what the
entry-slippage experiment measured from the cost side (-31.53R at 20% slippage). Decay measures it from
the signal side, and together they bound how fast we must act.

**Acceptance test.** IC at 5, 15, 30 and 60 minutes with sample sizes reported; a fitted half-life only
when the IC is monotone and above noise at the shortest horizon; otherwise `not_identified`.

Sources: the alpha-decay-reviewer skill pattern (IC decay across forward horizons); arXiv:2605.23905 on
industry-wide alpha half-life.

---

## Revised sequence

1. `decay.py` - smallest, needs only the ledger and bars, and it prices the latency problem already
   measured from the cost side.
2. `bocpd.py` - upgrades CUSUM into a quantity a position size can depend on.
3. `exit_policy.py` - the largest, and the one that must be built carefully because of look-ahead.
4. Drawdown-constrained Kelly - an extension to `ruin.py`, not a new module.

Together these replace four hard-coded constants - exit percentage, change threshold, Kelly divisor,
and "act fast" - with four solved quantities. That is the difference between a system that follows rules
and one that derives them.
