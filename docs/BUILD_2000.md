# The next 2,000 lines: what gets built, in what order, and how each is proved

Six modules, 2,000 lines of source plus ~700 of tests. No new abstraction layers. Every line is either
a decision the desk currently makes by convention or a claim it currently makes without evidence.

The selection rule: **a module earns its place only if it changes a number the desk acts on.** Anything
that would produce a report nobody sizes from is excluded, which is why there are six items here and not
sixteen.

---

## 1. `drift.py` - the precondition for the allocator (~330 lines)

**The problem it solves.** `experts.py` now allocates risk by recent evidence. Nothing in it knows whether
the recent evidence came from the same world as the corpus. A weight that moves because the evidence
changed is adaptation; one that moves because the world changed is decay, and the weight path cannot tell
them apart.

**What it does.** Wasserstein-1 distance between the current trade-outcome window and the corpus
distribution, in closed form for the 1-D case (no transport solver needed, so this is exact and fast).
Above a calibrated threshold, `advise()` refuses to allocate and names the drift.

**Lines.** 330 source: distance (60), windowing with overlap correction (70), threshold calibration by
permutation (90), the guard that wraps `experts.advise` (60), report/render (50).

**Acceptance test.** In-distribution resamples stay below threshold; a shifted window exceeds it; the
threshold's false-positive rate is *measured* on resampled in-distribution windows and reported, not
asserted.

**Why it is first.** Everything in `experts.py` is currently unguarded. This is the difference between
an adaptive system and one that adapts itself off a cliff.

---

## 2. `rough.py` - stop inheriting a memory length nobody chose (~300 lines)

**The problem it solves.** Every volatility estimate in the branch assumes how fast volatility
mean-reverts. Gatheral-Jaisson-Rosenbaum put H ≈ 0.1, which is far rougher than EWMA or GARCH assume -
so our estimates are systematically stale, and a stale vol is a wrong barrier, a wrong size, and a wrong
implied move, all at once.

**What it does.** Estimate H by regressing log |r| on log horizon across the bar corpus (the standard
estimator), with a block bootstrap for the confidence interval, and refuse when the sample cannot
distinguish 0.1 from 0.5.

**Lines.** 300 source: estimator (70), block bootstrap (90), roughness-adjusted vol target (80), refusals
and rendering (60).

**Acceptance test.** On synthetic fBm with known H the estimate recovers it within tolerance; on our six
bar sets the module either reports H with an interval or refuses - and with 630 bars it will probably
refuse, which is the correct answer and the module must be allowed to give it.

---

## 3. `tails.py` - the tail is not the worst observation (~320 lines)

**The problem it solves.** `ruin.py` says "no total losses, worst −0.39R" and I let that read as a
property of the instrument. It is a property of a 107-trade sample. A long 0DTE option's commonest
terminal outcome is −1.00R and seven months have not sampled it.

**What it does.** Fit a generalised Pareto to exceedances over a chosen threshold (threshold selection by
mean-residual-life plot), return shape and scale with standard errors, then re-run the ruin arithmetic
against the *fitted* tail instead of the observed one.

**Lines.** 320: GPD fit by probability-weighted moments (110), threshold selection (80), tail-implied loss
distribution (70), ruin re-run and comparison against the empirical result (60).

**Acceptance test.** Shape parameter reported with a standard error; where the fit predicts outcomes
beyond −1.00R the report says so rather than clipping at the corpus boundary; the breakeven haircut is
recomputed and the delta from 78.1% is stated explicitly.

---

## 4. `hawkes.py` - why we refuse trades in bursts (~310 lines)

**The problem it solves.** We attribute refusals to policy. Some of them are the two-slot concurrency cap
binding because alarms cluster, which is a different problem with a different fix.

**What it does.** Maximum-likelihood fit of an exponential-kernel Hawkes process to alert arrival times per
channel: mu, alpha, beta, branching ratio alpha/beta with a standard error, then a simulation of the
queue to measure how often the cap binds *because* of clustering.

**Lines.** 310: log-likelihood (90), optimiser with a closed-form gradient (90), branching ratio and
simulation (80), report (50).

**Acceptance test.** On synthetic Hawkes data the fit recovers the generating parameters; on the corpus,
a branching ratio not significantly below 1 is reported as "clustering not established" rather than
quietly fitted anyway.

---

## 5. `guard.py` - a pre-trade gate that uses what we now know (~380 lines)

**The problem it solves.** The authorization ladder refuses on rules; nothing refuses on *evidence quality*.
We can now measure drift, roughness, tail, and burst intensity, and none of it touches a decision.

**What it does.** A composable pre-trade gate: each check returns pass/refuse with a named reason and the
measured value, so a refusal is auditable and a pass is a recorded fact. Checks: drift state, vol-estimate
staleness, tail-implied position size, burst intensity against the concurrency cap, and a cooldown after
any halt. Fails closed; an unevaluable check is a refusal, not a pass.

**Lines.** 380: check protocol (50), five checks (200), composition and ordering (70), audit record (60).

**Acceptance test.** Every check has a test for pass and refuse; an unevaluable check refuses; a gate whose
checks contradict itself refuses rather than resolving by order.

**Why last.** It consumes the previous four, so building it earlier would mean inventing their interfaces
and getting them wrong.

---

## 6. `wiring.py` - close the seam (~360 lines)

**The problem it solves.** `experts.py` is the most developed module in the branch and **nothing consumes
it.** `attribution` measures variants, the allocator weights them, and the sizing layer still reads a
static table. That is the exact gap that made six attribution variants measure zero before the policy was
threaded through.

**What it does.** One path: run variants -> align -> blend -> guard -> posture -> a versioned
`live/weights.json` carrying the policy fingerprint, the identity count, and the drift state that produced
it. Read back at `arm` time, with a refusal when the file is stale or its fingerprint disagrees with the
running policy.

**Lines.** 360: run-and-collect (110), fingerprint and staleness (80), write/read with schema version (90),
`arm` integration and refusals (80).

**Acceptance test.** The weights file round-trips; a stale file refuses at arm; a fingerprint mismatch
refuses; the recorded identity count matches the blend; an empty variant set refuses rather than writing
uniform weights.

---

## Budget

| Module | Source | Tests | Why |
|---|---|---|---|
| `drift.py` | 330 | 120 | guard on the allocator |
| `rough.py` | 300 | 110 | invalidates every vol estimate |
| `tails.py` | 320 | 120 | re-tests the breakeven claim |
| `hawkes.py` | 310 | 110 | explains a misattributed refusal |
| `guard.py` | 380 | 140 | makes the measurements act |
| `wiring.py` | 360 | 120 | closes the seam |
| **total** | **2,000** | **720** | |

Two modules (~640 lines) can land as one commit; the rest are 300+ each, which clears the floor honestly
rather than by padding. If a module comes in short I will say so rather than inflate it - the failure mode
of every previous line target in this branch.

---

## What is deliberately excluded

- **`signature.py`** - needs 14+ features on 107 trades; ships when the corpus grows, not before.
- **`gex.py` / `vanna`** - blocked on chain data. Building them now means inventing open interest.
- **`impact.py`** - needs real fills to fit the square-root constant; until then the constant is a guess.
- **Anything reporting-only** - a module whose output no decision consumes is a liability that has to be
  maintained, and this branch already has three measurement systems withholding for want of bars.

The exclusion list is the discipline. Every previous plan in this branch failed by adding capability
faster than it added consumption; these six are chosen because five of them change a number the desk acts
on and the sixth connects an allocator that currently does nothing.
