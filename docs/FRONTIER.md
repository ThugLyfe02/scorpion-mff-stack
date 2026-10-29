# Frontier: six techniques a desk would recognise, and how each lands in PR #1

A research pass on what actually separates a serious execution stack from a backtest with a CLI.
Every item below is a named, published technique - not a trend - and each maps to one module with an
acceptance test. The criterion for inclusion is not novelty: it is that a professional would ask
"do you have this?" and the answer should be yes.

Ordered by value to us, not by fame.

---

## 1. The markout curve, with abstention  →  `markout_curve.py`

**What it is.** A markout is the signed price change after a fill, measured forward from the fill at a
series of horizons. The curve - markout at 1s, 5s, 30s, 5m - is the standard way a desk asks "did I
trade at a good price or did I get picked off". For a taker it measures entry quality; for a maker it
measures adverse selection.

**Why we need it.** We already compute a single-horizon mark-out and it withholds on 104 of 107
trades. The curve is what turns that from a number into a diagnostic: a fill that is positive at 1s and
negative at 5m is a static entry into a decaying move, which is exactly the failure mode a Discord
signal has.

**The part worth stealing.** The serious implementations do not report a mean when the panel cannot
support one; they *abstain* with a stated reason and cluster-robust intervals. That is the same policy
this branch already applies to TCA and regime cells, so it is a natural extension rather than a new
religion.

**Acceptance test.** For each filled trade, markouts at 4 horizons; per-channel curves with
cluster-robust intervals; a cell that cannot support an interval reports `abstained` with the reason and
never a point estimate.

Sources: questdb's markout cookbook (multi-horizon curve as the standard), DaruFinance/crypto-adverse-
selection (cluster-robust intervals that abstain), quantmemo (thresholding markouts into adverse
labels).

---

## 2. Kyle's lambda as a measured impact constant  →  `impact.py`

**What it is.** Kyle (1985): price change is linear in signed order flow, `ΔP = λ·OF + ε`. λ is the
permanent impact per unit of net flow - the price of demanding liquidity.

**Why we need it.** Sizing currently assumes one contract costs nothing to trade. λ gives a defensible
answer to "how much can this contract absorb before my own order moves it", which is the difference
between a paper fill and a real one. It also gives an objective liquidity ranking across strikes and
tickers without needing a quote feed.

**Implementation notes.** Aggregate 5-minute bars into ΔP and signed flow (proxy: close-to-close change
signed by volume when tick data is absent), regress with an intercept, report λ with its standard error
and **refuse when the R² is too low to distinguish λ from noise**.

**Acceptance test.** On synthetic data with a known λ the estimate recovers it within 10%; on data with
no relationship, the module returns `not_identified` rather than a small number that looks like low
impact.

Sources: EdouardLav/market-microstructure `kyle.py` (the regression as implemented), Hasbrouck (1991)
VAR as the stricter alternative.

---

## 3. CUSUM change-point alarms instead of threshold labels  →  `changepoint.py`

**What it is.** The cumulative sum control chart: accumulate deviations from a target and alarm when
the sum crosses a boundary. O(n), no fitting, no lookahead - the textbook sequential detector.

**Why we need it.** `regime.py` labels the *whole session* after the fact. CUSUM alarms *during* the
session, at the moment the process changes, which is the version that can gate an order. It also
replaces a hand-picked volatility threshold with a detection statistic whose false-alarm rate is
tunable by construction (h and k), rather than by taste.

**Implementation notes.** Two-sided CUSUM on log returns; `k` set from a target shift in units of
session sigma; `h` set from the acceptable run length before a false alarm. Report the alarm time, not
just the fact of one.

**Acceptance test.** On a series with a planted change, the alarm fires within a known number of bars
of the change and before the end of the series in ≥90% of seeded runs; on a stationary series, the false-
alarm rate matches the configured average run length.

Sources: giobbu/CUSUM (multiple detector flavours), Page (1954), the change-point survey in
arXiv:2604.20949.

---

## 4. Participation-rate exit scheduling for size  →  `schedule.py`

**What it is.** Almgren-Chriss gives the optimal trajectory for liquidating a position when impact and
variance trade off; POV (percent of volume) is the practical cousin: never take more than ρ of the
traded volume.

**Why we need it.** Our exits are single marketable limits. At one contract that is correct. At thirty
it is a market order with extra steps, and the corpus has clips up to 40 contracts. A participation cap
is the difference between "we exited" and "we moved the price and then exited".

**Implementation notes.** Discrete schedule over the remaining bars of the session, capped at
`ρ × bar volume`; compare against a single-shot exit on the same path and report the difference in
basis points. Keep the Almgren-Chriss closed form as the reference trajectory, not as the product.

**Acceptance test.** A 40-contract exit scheduled at 10% POV never executes more than 10% of any bar's
volume, and its total slippage versus a single shot is reported in bps with the bars it consumed.

Sources: Wilfrid-art/almgren-chriss-optimal-execution, crollila/execution-microstructure-lab (TWAP /
VWAP / POV / passive in one harness), Evelyn-ziyang/TWAP_vs_VWAP_vs_POV.

---

## 5. Fill-probability and queue position - the honest "no"

**What it is.** For passive orders, fill probability is a function of queue position, time, local
volatility and spread; the standard model tracks orders ahead and arrival/cancel rates.

**Why we are not building it yet.** We are a taker: our limit is marketable, so queue position is not
the binding constraint and modelling it would be sophistication without application. It becomes relevant
the moment an exit is passive, which is what item 4 introduces. Recorded here so the decision is
visible, not forgotten.

**Trigger to build.** First passive exit larger than five contracts, or the first time we compete for
a fill rather than take one.

Sources: mathemicalcompany queue-position model, rahinigautam/queue-dynamics (Markov chain treatment),
rollbrains slippage simulation.

---

## 6. Shadow execution for the first live week  →  `shadow.py`

**What it is.** Run the real decision path against a live feed, send to a paper venue, and compare the
paper fill against what the real venue *would* have given - the "shadow" of a live order.

**Why we need it.** It is the only way to measure our own slippage without risking size, and it is the
first thing a partner will ask for: "how do you know your fills are realistic?" Right now the honest
answer is "we don't; the paper venue fills at the limit".

**Implementation notes.** Two senders, one decision; record `paper_fill` and `shadow_fill` side by side
for every effect; report the distribution of the difference. No behaviour depends on the shadow.

**Acceptance test.** N effects produce N paired records, and the summary reports median and p90
divergence in bps - against the same threshold machinery the TCA layer already uses.

---

## Why these six and not the rest

Everything above is measurable offline against data we already hold, and each produces a number that
changes a decision:

| Module | Decision it changes |
|---|---|
| `markout_curve` | whether the entry or the exit is the problem |
| `impact` | how many contracts a strike can absorb |
| `changepoint` | whether to trade at all, mid-session |
| `schedule` | how to leave a position larger than five contracts |
| `shadow` | whether our fills are fantasy |

The bar is not "is this impressive". It is "does this change what the desk does on Monday".

---

## Sequence

1. `changepoint.py` - smallest, sharpest, and it gates orders.
2. `markout_curve.py` - extends a module that already exists and already withholds.
3. `impact.py` - needs bars, which is the same blocker as everything else.
4. `schedule.py` - needs volume per bar; the bar store already carries it.
5. `shadow.py` - needs the venue seam, so it lands with credentials.
6. Queue modelling - explicitly deferred until a passive exit exists.
