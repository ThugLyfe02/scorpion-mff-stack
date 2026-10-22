"""Evidence, not vibes: what a record of trades can and cannot prove.

`expectancy.py` answers "what did the record average". That is a description. A desk
needs a *decision*: is the average distinguishable from zero, how much of it is
selection, is it still there, and how many trades would it take to know?

This module is deliberately conservative about all four:

1. **Expectancy gets an interval.** A win-rate Wilson interval is not an interval on
   money. Options P&L is right-skewed (one 1,200% runner pays for many scratches), so
   the mean is dominated by the tail and the win rate can look fine while expectancy is
   negative. The interval here is a bootstrap of the mean, with BCa correction for skew
   and bias, and an optional moving-block bootstrap for serial dependence.
2. **"Is it positive?" gets a posterior, not a p-value alone.** A Bayesian bootstrap
   puts a distribution on the mean without assuming normality; `p_up` is the posterior
   probability that the mean is above zero. It is reported beside the frequentist
   interval because the two answer different questions.
3. **Channels are shrunk with a data-estimated prior.** The fixed ten-pseudo-trade
   shrinkage in `expectancy.py` is a reasonable default; here the prior strength is
   estimated from the between-channel spread, so a channel is pulled toward the pooled
   mean exactly as much as the cross-section says it should be.
4. **The best channel is adjusted for being the best.** With four channels, the winner
   is selected on noise. A Benjamini-Hochberg q-value is reported for every channel,
   and certification requires the q-value, not the raw p.

Then the questions that make it operational:

* **Has it changed?** A single-change-point scan with a permutation p-value, not a
  hand-drawn line on an equity curve.
* **How much evidence is enough?** A power calculation: the n needed to distinguish the
  observed effect from zero at 80% power.
* **May we size on it?** Only live fills count. The historical corpus is the hone's own
  winners-heavy call record; it is labelled `CORPUS_CALIBRATION` and can never produce
  an allocation. That is a hard gate, not a footnote.

Pure standard library. Deterministic given a seed. No forecast, no model of the market:
this measures the record it is given and says when the record is too thin to measure.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

Z95 = 1.959963984540054
MIN_TRADES = 20
CERTIFY_MIN_TRADES = 30
DEFAULT_DRAWS = 4000
DEFAULT_SEED = 7
OPTION_MULTIPLIER = 100.0

SOURCE_LIVE = "LIVE_FILLS"
SOURCE_CORPUS = "CORPUS_CALIBRATION"
SOURCE_SYNTHETIC = "SYNTHETIC"
_SOURCES = {SOURCE_LIVE, SOURCE_CORPUS, SOURCE_SYNTHETIC}

STATUS_INSUFFICIENT = "INSUFFICIENT"
STATUS_NEGATIVE = "NEGATIVE"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"
STATUS_PROVISIONAL = "PROVISIONAL"
STATUS_CERTIFIED = "CERTIFIED"
STATUS_DEGRADED = "DEGRADED"
STATUS_CALIBRATION = "CALIBRATION_ONLY"


# ── distributions ────────────────────────────────────────────────────────────
def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def normal_ppf(p: float) -> float:
    """Inverse standard normal, Acklam's rational approximation (~1.15e-9)."""
    if not 0.0 < p < 1.0:
        if p <= 0.0:
            return -math.inf
        return math.inf
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def variance(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def quantile(sorted_xs: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile on an already-sorted sequence."""
    if not sorted_xs:
        return 0.0
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = (len(sorted_xs) - 1) * min(1.0, max(0.0, q))
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_xs) - 1)
    frac = pos - lo
    return sorted_xs[lo] * (1 - frac) + sorted_xs[hi] * frac


def _resample(xs: Sequence[float], rng: random.Random) -> list[float]:
    n = len(xs)
    return [xs[rng.randrange(n)] for _ in range(n)]


# ── the record ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Trade:
    """One closed trade, in dollars. `channel` is the source that opened it."""

    pnl_usd: float
    ts: float = 0.0
    channel: str = "unknown"
    ticker: str = ""
    qty: int = 1
    entry_price: float = 0.0
    exit_price: float = 0.0

    @property
    def pnl_r(self) -> float:
        """P&L in units of the premium paid. -1R is a total loss; losses are floored."""
        cost = self.entry_price * self.qty * OPTION_MULTIPLIER
        return self.pnl_usd / cost if cost > 0 else 0.0


@dataclass
class Bootstrap:
    mean: float
    low: float
    high: float
    p_value: float          # one-sided bootstrap test of mean <= 0
    draws: int = 0
    method: str = "percentile"

    def to_dict(self) -> dict:
        return {"mean": round(self.mean, 4), "ci_low": round(self.low, 4),
                "ci_high": round(self.high, 4), "p_value": round(self.p_value, 6),
                "draws": self.draws, "method": self.method}


def bootstrap_mean(xs: Sequence[float], *, draws: int = DEFAULT_DRAWS,
                   alpha: float = 0.05, seed: int = DEFAULT_SEED,
                   bca: bool = True) -> Bootstrap:
    """Bootstrap the mean of a P&L series.

    Percentile intervals under-cover on skewed data; BCa corrects for bias and
    acceleration (skew). Below three observations neither is meaningful, so the
    interval collapses to the observed point and the p-value stays honest.
    """
    if not xs:
        return Bootstrap(0.0, 0.0, 0.0, 1.0, 0, "empty")
    observed = mean(xs)
    values = list(xs)
    if len(values) < 3:
        return Bootstrap(observed, observed, observed, 1.0, 0, "insufficient")

    rng = random.Random(seed)
    boots = sorted(mean(_resample(values, rng)) for _ in range(max(200, draws)))
    b = len(boots)
    p_value = (1 + sum(1 for x in boots if x <= 0.0)) / (b + 1)

    lo_q, hi_q = alpha / 2.0, 1.0 - alpha / 2.0
    method = "percentile"
    if bca:
        # Bias correction: how often the bootstrap sits below the observed mean.
        below = sum(1 for x in boots if x < observed)
        prop = min(1 - 1e-6, max(1e-6, (below + 0.5) / b))
        z0 = normal_ppf(prop)
        # Acceleration from the jackknife third moment.
        jack = [mean(values[:i] + values[i + 1:]) for i in range(len(values))]
        jack_mean = mean(jack)
        num = sum((j - jack_mean) ** 3 for j in jack)
        den = 6.0 * (sum((j - jack_mean) ** 2 for j in jack) ** 1.5)
        accel = num / den if den else 0.0

        def adjust(q: float) -> float:
            z = normal_ppf(q)
            shifted = z0 + (z0 + z) / (1.0 - accel * (z0 + z))
            return normal_cdf(shifted)

        lo_q, hi_q = adjust(lo_q), adjust(hi_q)
        method = "bca"

    low, high = quantile(boots, lo_q), quantile(boots, hi_q)
    if low > high:                    # extreme acceleration can cross the quantiles
        low, high = high, low
    return Bootstrap(observed, low, high, p_value, b, method)


def bayesian_p_up(xs: Sequence[float], *, draws: int = DEFAULT_DRAWS,
                  seed: int = DEFAULT_SEED) -> float:
    """Posterior P(mean > 0) under a Bayesian bootstrap (Dirichlet(1) weights).

    Distribution-free: no normal likelihood, so a 1,200% runner does not turn into a
    phantom precision. It answers "given these trades and no other assumption, how
    much posterior mass sits above zero?"
    """
    if not xs:
        return 0.5
    rng = random.Random(seed)
    n = len(xs)
    above = 0
    for _ in range(max(200, draws)):
        weights = [rng.expovariate(1.0) for _ in range(n)]
        total = sum(weights)
        if sum(w * x for w, x in zip(weights, xs)) / total > 0.0:
            above += 1
    return above / max(200, draws)


@dataclass
class Summary:
    n: int
    mean: float
    median: float
    stdev: float
    win_rate: float
    profit_factor: float
    best: float
    worst: float
    cvar_95: float           # mean of the worst 5% (expected shortfall)

    def to_dict(self) -> dict:
        return {"n": self.n, "mean_usd": round(self.mean, 2),
                "median_usd": round(self.median, 2), "stdev_usd": round(self.stdev, 2),
                "win_rate": round(self.win_rate, 4),
                "profit_factor": (round(self.profit_factor, 4)
                                  if math.isfinite(self.profit_factor) else None),
                "best_usd": round(self.best, 2), "worst_usd": round(self.worst, 2),
                "cvar_95_usd": round(self.cvar_95, 2)}


def summarise(xs: Sequence[float]) -> Summary:
    if not xs:
        return Summary(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    ordered = sorted(xs)
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x < 0]
    gross_loss = -sum(losses)
    tail = max(1, math.ceil(0.05 * len(xs)))
    return Summary(
        n=len(xs), mean=mean(xs), median=quantile(ordered, 0.5),
        stdev=math.sqrt(variance(xs)), win_rate=len(wins) / len(xs),
        profit_factor=(sum(wins) / gross_loss) if gross_loss else float("inf"),
        best=max(xs), worst=min(xs), cvar_95=mean(ordered[:tail]))


# ── channels and selection ───────────────────────────────────────────────────
@dataclass
class ChannelEvidence:
    channel: str
    n: int
    mean: float
    stdev: float
    ci_low: float
    ci_high: float
    p_value: float
    q_value: float = 1.0
    posterior_up: float = 0.5
    shrunk_mean: float = 0.0
    shrink_weight: float = 1.0
    status: str = STATUS_INSUFFICIENT

    def to_dict(self) -> dict:
        return {"channel": self.channel, "n": self.n,
                "mean_usd": round(self.mean, 2),
                "stdev_usd": round(self.stdev, 2),
                "ci_low_usd": round(self.ci_low, 2),
                "ci_high_usd": round(self.ci_high, 2),
                "p_value": round(self.p_value, 6),
                "q_value": round(self.q_value, 6),
                "posterior_p_up": round(self.posterior_up, 4),
                "shrunk_mean_usd": round(self.shrunk_mean, 2),
                "shrink_weight": round(self.shrink_weight, 4),
                "status": self.status}


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """BH q-values, monotone and capped at 1. Preserves input order."""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    q = [1.0] * m
    running = 1.0
    for rank, index in enumerate(reversed(order), start=1):
        position = m - rank + 1
        value = min(1.0, p_values[index] * m / position)
        running = min(running, value)
        q[index] = running
    return q


def hierarchical_shrink(groups: Mapping[str, Sequence[float]]) -> dict[str, tuple[float, float]]:
    """Empirical-Bayes channel means: (shrunk_mean, weight_on_own_mean).

    tau^2 = between-channel variance of the means, net of sampling variance. When the
    channels look alike (tau^2 = 0) every channel is pooled; when they genuinely differ
    each keeps most of its own mean. The fixed ten-trade prior in `expectancy.py` is a
    special case of this with a hand-set tau^2.
    """
    usable = {k: list(v) for k, v in groups.items() if v}
    if not usable:
        return {}
    pooled = mean([x for xs in usable.values() for x in xs])
    if len(usable) == 1:
        only = next(iter(usable))
        return {only: (mean(usable[only]), 1.0)}

    stats = []
    for channel, xs in usable.items():
        stats.append((channel, len(xs), mean(xs), variance(xs)))
    # Sampling variance of each channel mean, and the observed spread of the means.
    sampling = mean([var / n for _, n, _, var in stats if n > 0])
    observed_spread = variance([m for _, _, m, _ in stats])
    tau2 = max(0.0, observed_spread - sampling)
    within = max(1e-12, sampling)
    prior_strength = within / tau2 if tau2 > 1e-12 else math.inf

    out: dict[str, tuple[float, float]] = {}
    for channel, n, m, _ in stats:
        if math.isinf(prior_strength):
            out[channel] = (pooled, 0.0)
            continue
        weight = n / (n + prior_strength)
        out[channel] = (weight * m + (1 - weight) * pooled, weight)
    return out


# ── change and power ─────────────────────────────────────────────────────────
@dataclass
class ChangePoint:
    index: int
    before_mean: float
    after_mean: float
    t_stat: float
    p_value: float
    detected: bool

    def to_dict(self) -> dict:
        return {"index": self.index, "before_mean_usd": round(self.before_mean, 2),
                "after_mean_usd": round(self.after_mean, 2),
                "t_stat": round(self.t_stat, 3), "p_value": round(self.p_value, 4),
                "detected": self.detected}


def change_point(xs: Sequence[float], *, min_segment: int = 8,
                 permutations: int = 300, seed: int = DEFAULT_SEED) -> ChangePoint:
    """Largest single mean shift, with a permutation p-value.

    The scan is O(n^2) and the p-value is a permutation test, so a seasonal-looking
    equity curve cannot manufacture a break. `detected` requires p < 0.05 and a real
    minimum of observations on both sides.
    """
    n = len(xs)
    if n < 2 * min_segment:
        return ChangePoint(0, mean(xs), mean(xs), 0.0, 1.0, False)

    def best_split(values: Sequence[float]) -> tuple[int, float, float, float]:
        best = (min_segment, 0.0, 0.0, 0.0)
        for split in range(min_segment, len(values) - min_segment + 1):
            left, right = values[:split], values[split:]
            se = math.sqrt(variance(left) / len(left) + variance(right) / len(right))
            t = (mean(left) - mean(right)) / se if se > 0 else 0.0
            if abs(t) > abs(best[3]):
                best = (split, mean(left), mean(right), t)
        return best

    split, before, after, observed_t = best_split(xs)
    rng = random.Random(seed)
    extreme = 0
    shuffled = list(xs)
    for _ in range(max(50, permutations)):
        rng.shuffle(shuffled)
        if abs(best_split(shuffled)[3]) >= abs(observed_t):
            extreme += 1
    p_value = (1 + extreme) / (max(50, permutations) + 1)
    return ChangePoint(split, before, after, observed_t, p_value,
                       detected=p_value < 0.05)


def required_n(effect: float, stdev: float, *, alpha: float = 0.05,
               power: float = 0.80) -> int | None:
    """Trades needed to detect `effect` at this dispersion (one-sided normal)."""
    if effect <= 0 or stdev <= 0:
        return None
    z_alpha = normal_ppf(1 - alpha)
    z_power = normal_ppf(power)
    return max(2, math.ceil(((z_alpha + z_power) * stdev / effect) ** 2))


# ── the verdict ──────────────────────────────────────────────────────────────
def classify(*, n: int, p_up: float, ci_low: float, q_value: float,
             change: ChangePoint | None = None) -> str:
    if n < MIN_TRADES:
        return STATUS_INSUFFICIENT
    if change is not None and change.detected and change.after_mean < change.before_mean \
            and change.after_mean <= 0:
        return STATUS_DEGRADED
    if p_up >= 0.99 and ci_low > 0 and n >= CERTIFY_MIN_TRADES and q_value <= 0.10:
        return STATUS_CERTIFIED
    if p_up >= 0.95 and ci_low > 0:
        return STATUS_PROVISIONAL
    if p_up <= 0.05:
        return STATUS_NEGATIVE
    return STATUS_INCONCLUSIVE


@dataclass
class EvidenceReport:
    source: str
    trades: list[Trade]
    summary: Summary
    overall: Bootstrap
    posterior_up: float
    channels: list[ChannelEvidence]
    change: ChangePoint | None
    required_n: int | None
    status: str
    allocation: dict[str, float] | None
    allocation_blocked_reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def sizing_eligible(self) -> bool:
        return self.allocation is not None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "status": self.status,
            "summary": self.summary.to_dict(),
            "overall": self.overall.to_dict(),
            "posterior_p_up": round(self.posterior_up, 4),
            "channels": [c.to_dict() for c in self.channels],
            "change_point": self.change.to_dict() if self.change else None,
            "required_n": self.required_n,
            "allocation": ({k: round(v, 4) for k, v in self.allocation.items()}
                           if self.allocation else None),
            "allocation_blocked_reason": self.allocation_blocked_reason,
            "sizing_eligible": self.sizing_eligible,
            "notes": list(self.notes),
        }


def _allocation(channels: Sequence[ChannelEvidence]) -> dict[str, float] | None:
    """Risk-parity-ish weights on posterior edge per unit of variance.

    Research allocation, not an order: capped at 50% per channel so one lucky
    channel cannot become the book. Non-positive posterior means get zero.
    """
    scores: dict[str, float] = {}
    for c in channels:
        # A channel with five trades can look 100% posterior-positive. That is not
        # evidence, and it does not get a weight just because the pooled record
        # cleared the overall gate.
        if c.status not in (STATUS_PROVISIONAL, STATUS_CERTIFIED):
            continue
        if c.posterior_up <= 0.5 or c.shrunk_mean <= 0 or c.stdev <= 0:
            continue
        scores[c.channel] = (c.shrunk_mean / (c.stdev ** 2)) * (c.posterior_up - 0.5) * 2
    if not scores:
        return None
    total = sum(scores.values())
    raw = {k: v / total for k, v in scores.items()}
    capped = {k: min(0.5, v) for k, v in raw.items()}
    # Redistribute the excess proportionally until no cap binds or nothing is left.
    for _ in range(8):
        excess = 1.0 - sum(capped.values())
        if excess <= 1e-9:
            break
        room = {k: 0.5 - v for k, v in capped.items() if v < 0.5}
        room_total = sum(room.values())
        if room_total <= 1e-9:
            break
        for k, capacity in room.items():
            capped[k] += excess * capacity / room_total
    total = sum(capped.values())
    # Do NOT renormalise: a cap at 50% means the rest is cash, not a bigger bet on
    # whatever channel happens to be left.
    return {k: v for k, v in capped.items() if v > 0} if total > 0 else None


def build_report(trades: Iterable[Trade], *, source: str = SOURCE_LIVE,
                 draws: int = DEFAULT_DRAWS, seed: int = DEFAULT_SEED,
                 permutations: int = 300) -> EvidenceReport:
    if source not in _SOURCES:
        raise ValueError(f"unknown source {source!r}")
    rows = sorted(trades, key=lambda t: t.ts)
    xs = [t.pnl_usd for t in rows]
    summary = summarise(xs)
    overall = bootstrap_mean(xs, draws=draws, seed=seed)
    p_up = bayesian_p_up(xs, draws=draws, seed=seed + 1)
    change = change_point(xs, permutations=permutations, seed=seed + 2) if len(xs) >= 16 else None
    needed = required_n(abs(summary.mean), summary.stdev)

    grouped: dict[str, list[float]] = {}
    for t in rows:
        grouped.setdefault(t.channel, []).append(t.pnl_usd)
    shrunk = hierarchical_shrink(grouped)

    channels: list[ChannelEvidence] = []
    for channel, values in sorted(grouped.items()):
        boot = bootstrap_mean(values, draws=max(500, draws // 2), seed=seed + 3)
        # Channel-level intervals use the same family; p is the bootstrap one-sided test.
        post = bayesian_p_up(values, draws=max(500, draws // 2), seed=seed + 4)
        shrunk_mean, weight = shrunk.get(channel, (mean(values), 1.0))
        channels.append(ChannelEvidence(
            channel=channel, n=len(values), mean=mean(values),
            stdev=math.sqrt(variance(values)), ci_low=boot.low, ci_high=boot.high,
            p_value=boot.p_value, posterior_up=post,
            shrunk_mean=shrunk_mean, shrink_weight=weight))
    for channel, q in zip(channels, benjamini_hochberg([c.p_value for c in channels])):
        channel.q_value = q
    for channel in channels:
        channel.status = classify(n=channel.n, p_up=channel.posterior_up,
                                  ci_low=channel.ci_low, q_value=channel.q_value)
        if source != SOURCE_LIVE and channel.status in (STATUS_PROVISIONAL,
                                                        STATUS_CERTIFIED):
            channel.status = STATUS_CALIBRATION

    status = classify(n=summary.n, p_up=p_up, ci_low=overall.low,
                      q_value=0.0, change=change)
    if source != SOURCE_LIVE and status in (STATUS_PROVISIONAL, STATUS_CERTIFIED):
        # A winners-heavy call record can look statistically decisive. It is still
        # not evidence about the next call: selection happened before the record
        # began. Say so in the headline, not only in a footnote.
        status = STATUS_CALIBRATION

    allocation = None
    blocked = ""
    if source != SOURCE_LIVE:
        blocked = ("source is not live fills: the corpus is the hone's own "
                   "winners-heavy record, so it cannot size")
    elif status not in (STATUS_PROVISIONAL, STATUS_CERTIFIED):
        blocked = f"status {status}: no allocation until the edge is credible"
    else:
        allocation = _allocation(channels)
        if allocation is None:
            blocked = "no channel has a positive posterior edge"

    notes = []
    if source == SOURCE_CORPUS:
        notes.append("calibration only: the corpus records calls the hone chose to "
                     "post, so it cannot price the calls it did not post")
    if summary.n and summary.mean > 0 and summary.median < summary.mean / 2:
        notes.append("mean is far above the median: the average is carried by a tail, "
                     "so the interval and CVaR matter more than the point")
    if needed is not None and needed > summary.n:
        notes.append(f"power: ~{needed} trades needed to detect this effect at 80%; "
                     f"the record has {summary.n}")

    return EvidenceReport(source=source, trades=rows, summary=summary, overall=overall,
                          posterior_up=p_up, channels=channels, change=change,
                          required_n=needed, status=status, allocation=allocation,
                          allocation_blocked_reason=blocked, notes=notes)


# ── inputs: live fills and the historical corpus ─────────────────────────────
def trades_from_fills(records: Iterable[Mapping[str, object]] | Iterable[object]) -> tuple[list[Trade], dict]:
    """Rebuild round trips from the append-only fill log, average-cost like the engine.

    Only accepted rows move the book. A sell without a holding, or a sell larger than
    the holding, is an anomaly: it is counted and skipped, never guessed into P&L.
    Reconciliation belongs in `tca fills`; this function refuses to invent it.
    """
    book: dict[str, dict[str, float | int | str]] = {}
    trades: list[Trade] = []
    anomalies = {"sell_without_position": 0, "oversell": 0, "unpriced": 0}

    def as_int(value: object) -> int:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return 0

    def as_float(value: object) -> float:
        try:
            text = str(value)
            return float(text)
        except (TypeError, ValueError):
            return 0.0

    for row in records:
        if isinstance(row, Mapping):
            event = str(row.get("event") or "")
            key = str(row.get("contract_key") or "")
            qty = as_int(row.get("qty"))
            priced = row.get("price") is not None
            price = as_float(row.get("price"))
            ts = as_float(row.get("ts"))
            note = str(row.get("note") or "")
            accepted = bool(row.get("accepted", True))
        else:
            event = str(getattr(row, "event", ""))
            key = str(getattr(row, "contract_key", ""))
            qty = int(getattr(row, "qty", 0) or 0)
            priced = getattr(row, "price", None) is not None
            price = float(getattr(row, "price", 0.0) or 0.0)
            ts = float(getattr(row, "ts", 0.0) or 0.0)
            note = str(getattr(row, "note", "") or "")
            accepted = bool(getattr(row, "accepted", True))
        if not accepted or not key:
            continue
        if event == "buy" and qty > 0 and price > 0:
            held = book.get(key)
            if held:
                total = int(held["qty"]) + qty
                held["avg"] = (float(held["avg"]) * int(held["qty"]) + price * qty) / total
                held["qty"] = total
            else:
                book[key] = {"qty": qty, "avg": price, "channel": note or "unknown"}
        elif event == "sell" and qty > 0:
            held = book.get(key)
            if not held:
                anomalies["sell_without_position"] += 1
                continue
            if qty > int(held["qty"]):
                anomalies["oversell"] += 1
                qty = int(held["qty"])
            avg = float(held["avg"])
            if not priced or price < 0 or avg <= 0:
                anomalies["unpriced"] += 1
                continue
            trades.append(Trade(pnl_usd=(price - avg) * qty * OPTION_MULTIPLIER, ts=ts,
                                channel=str(held["channel"]), ticker=key.split("|")[0],
                                qty=qty, entry_price=avg, exit_price=price))
            held["qty"] = int(held["qty"]) - qty
            if int(held["qty"]) <= 0:
                del book[key]
    open_premium = sum(float(v["avg"]) * int(v["qty"]) * OPTION_MULTIPLIER
                       for v in book.values())
    return trades, {"anomalies": anomalies, "open_positions": len(book),
                    "open_premium_usd": round(open_premium, 2)}


def load_fills(path: str | Path) -> tuple[list[Trade], dict]:
    p = Path(path)
    if not p.exists():
        return [], {"anomalies": {}, "open_positions": 0, "open_premium_usd": 0.0,
                    "missing": str(p)}
    records = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return trades_from_fills(records)


def load_corpus(path: str | Path) -> tuple[list[Trade], dict]:
    """The historical alert corpus: one contract per row, exit as a percent return."""
    p = Path(path)
    rows = json.loads(p.read_text(encoding="utf-8"))
    trades: list[Trade] = []
    skipped = 0
    for row in rows:
        pct = row.get("exit_pct")
        bid = row.get("entry_bid")
        if pct is None or bid is None:
            skipped += 1
            continue
        try:
            pnl = float(bid) * float(pct) / 100.0 * OPTION_MULTIPLIER
            stamp = f"{row.get('date', '1970-01-01')} {row.get('time_et', '00:00')}"
            ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            skipped += 1
            continue
        trades.append(Trade(pnl_usd=pnl, ts=ts, channel=str(row.get("channel") or "unknown"),
                            ticker=str(row.get("ticker") or ""), qty=1,
                            entry_price=float(bid),
                            exit_price=float(bid) * (1 + float(pct) / 100.0)))
    return trades, {"skipped_rows": skipped, "total_rows": len(rows)}


# ── rendering ────────────────────────────────────────────────────────────────
def to_text(report: EvidenceReport) -> str:
    s = report.summary
    o = report.overall
    lines = [
        f"evidence | {report.source} | {report.status} | n={s.n}",
        (f"  expectancy ${s.mean:,.2f}/trade  95% {o.method} CI "
         f"[${o.low:,.2f}, ${o.high:,.2f}]  p(mean<=0)={o.p_value:.3f}"),
        (f"  posterior P(edge>0)={report.posterior_up:.1%}  "
         f"median ${s.median:,.2f}  stdev ${s.stdev:,.2f}  "
         f"profit factor {s.profit_factor:.2f}  CVaR95 ${s.cvar_95:,.2f}"),
    ]
    if report.change is not None:
        c = report.change
        lines.append(f"  change point at trade {c.index}: ${c.before_mean:,.2f} -> "
                     f"${c.after_mean:,.2f} (t={c.t_stat:+.2f}, p={c.p_value:.3f}"
                     f"{', detected' if c.detected else ''})")
    if report.required_n is not None:
        lines.append(f"  power: ~{report.required_n} trades for 80% power at this effect")
    if report.channels:
        lines.append("")
        lines.append("  channel                    n     mean$   95% CI                 "
                     "P(up)   shrunk$   q      status")
        for c in report.channels:
            lines.append(
                f"  {c.channel:<24}{c.n:>3}  {c.mean:>9,.2f}  "
                f"[{c.ci_low:>8,.2f},{c.ci_high:>9,.2f}]  "
                f"{c.posterior_up:>5.1%}  {c.shrunk_mean:>9,.2f}  "
                f"{c.q_value:>5.2f}  {c.status}")
    if report.allocation:
        lines.append("")
        lines.append("  research allocation (not live size): "
                     + ", ".join(f"{k} {v:.0%}" for k, v in report.allocation.items()))
    elif report.allocation_blocked_reason:
        lines.append("")
        lines.append(f"  allocation withheld: {report.allocation_blocked_reason}")
    for note in report.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def to_markdown(report: EvidenceReport) -> str:
    s, o = report.summary, report.overall
    lines = [
        "# Evidence report",
        "",
        (f"Source: **{report.source}** · Status: **{report.status}** · "
         f"{s.n} closed trades."),
        "",
        "| metric | value |",
        "|---|---|",
        f"| expectancy | ${s.mean:,.2f}/trade |",
        f"| 95% {o.method} CI | ${o.low:,.2f} to ${o.high:,.2f} |",
        f"| bootstrap p(mean ≤ 0) | {o.p_value:.3f} |",
        f"| posterior P(edge > 0) | {report.posterior_up:.1%} |",
        f"| median | ${s.median:,.2f} |",
        f"| stdev | ${s.stdev:,.2f} |",
        f"| profit factor | {s.profit_factor:.2f} |",
        f"| CVaR 95% | ${s.cvar_95:,.2f} |",
        f"| best / worst | ${s.best:,.2f} / ${s.worst:,.2f} |",
        "",
    ]
    if report.change is not None:
        c = report.change
        lines += ["## Stability", "",
                  (f"Best single change point at trade **{c.index}**: "
                   f"${c.before_mean:,.2f} → ${c.after_mean:,.2f} "
                   f"(t={c.t_stat:+.2f}, permutation p={c.p_value:.3f})."),
                  ""]
    lines += ["## Channels", "",
              "| channel | n | mean | 95% CI | P(up) | shrunk | q | status |",
              "|---|---|---|---|---|---|---|---|"]
    for c in report.channels:
        lines.append(f"| {c.channel} | {c.n} | ${c.mean:,.2f} | "
                     f"${c.ci_low:,.2f}–${c.ci_high:,.2f} | {c.posterior_up:.1%} | "
                     f"${c.shrunk_mean:,.2f} | {c.q_value:.3f} | {c.status} |")
    lines += ["", "## Allocation", ""]
    if report.allocation:
        lines.append("Research weights (not live size, capped at 50% per channel):")
        lines.append("")
        for channel, weight in report.allocation.items():
            lines.append(f"- {channel}: **{weight:.1%}**")
    else:
        lines.append(f"Withheld: {report.allocation_blocked_reason}.")
    lines += ["", "## Limits", ""]
    if report.source != SOURCE_LIVE:
        lines.append("- This is not live evidence. The corpus is the hone's own record "
                     "of calls and is winners-heavy; it cannot price the calls it did "
                     "not post.")
    if report.required_n is not None and report.required_n > s.n:
        lines.append(f"- The record is underpowered: about {report.required_n} trades "
                     f"are needed to detect this effect at 80% power.")
    for note in report.notes:
        lines.append(f"- {note}")
    lines += ["", "_generated by `python -m scorpion_mff.cli evidence report`_", ""]
    return "\n".join(lines)
