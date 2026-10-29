"""What the record actually says: expectancy, confidence, drawdown, streaks.

The desk's question is not "did we make money". It is:

    which channels deserve the next dollar, how sure are we, and how bad does a
    bad run get before the policy has to intervene?

Four things here, all deterministic and all inspectable:

1. **Per-channel expectancy with an interval.** A win rate from 7 trades is not a win
   rate, it is an anecdote. Every rate carries a Wilson interval, which behaves at small
   n where the textbook normal interval produces nonsense.
2. **Shrinkage toward the pooled mean.** With `PRIOR_TRADES` pseudo-trades of pooled
   performance mixed in, a channel with 7 trades cannot outrank one with 41 on noise.
   This is the difference between ranking channels and ranking luck.
3. **Drawdown, in dollars and in R**, from the chronological equity path - plus the
   longest losing run.
4. **A runs test.** If losses cluster more than independence predicts, a loss-streak
   rule is doing real work; if they do not, a streak rule is superstition with a
   stopwatch. The test says which.

Nothing here is a forecast. It is the record, measured.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

PRIOR_TRADES = 10.0        # pseudo-trades of pooled performance mixed into each channel
Z95 = 1.959963984540054


@dataclass
class Outcome:
    """One closed trade. `pnl_usd` is realised, net of nothing - the fill log is gross."""

    channel: str
    pnl_usd: float
    ts: float = 0.0
    ticker: str = ""


@dataclass
class Stats:
    n: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    win_rate: float = 0.0
    ci_low: float = 0.0
    ci_high: float = 0.0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    expectancy_usd: float = 0.0
    stdev_usd: float = 0.0
    profit_factor: float = 0.0
    gross_win_usd: float = 0.0
    gross_loss_usd: float = 0.0
    risk_unit_usd: float = 0.0
    adjusted_expectancy_usd: float | None = None   # set by by_channel(); shrunk view

    @property
    def adjusted(self) -> float:
        """Expectancy pulled toward the pooled mean - the number ranking uses."""
        if self.adjusted_expectancy_usd is None:
            return self.expectancy_usd
        return self.adjusted_expectancy_usd

    @property
    def expectancy_r(self) -> float:
        return self.expectancy_usd / self.risk_unit_usd if self.risk_unit_usd else 0.0

    @property
    def per_trade_score(self) -> float:
        """Mean over dispersion: a per-trade signal-to-noise, not an annualised Sharpe.

        Named a score on purpose. Calling it Sharpe would imply a sampling frequency
        and a risk-free rate this data cannot support.
        """
        return self.expectancy_usd / self.stdev_usd if self.stdev_usd else 0.0

    @property
    def reliable(self) -> bool:
        """n >= PRIOR_TRADES and the interval excludes zero win rate."""
        return self.n >= PRIOR_TRADES and self.ci_low > 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"n": self.n, "wins": self.wins, "losses": self.losses, "flats": self.flats,
                "win_rate": round(self.win_rate, 4), "ci_low": round(self.ci_low, 4),
                "ci_high": round(self.ci_high, 4), "avg_win_usd": round(self.avg_win_usd, 2),
                "avg_loss_usd": round(self.avg_loss_usd, 2),
                "expectancy_usd": round(self.expectancy_usd, 2),
                "expectancy_r": round(self.expectancy_r, 4),
                "stdev_usd": round(self.stdev_usd, 2),
                "profit_factor": round(self.profit_factor, 4),
                "per_trade_score": round(self.per_trade_score, 4),
                "reliable": self.reliable}


def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval - sane at n=0, n=1 and p=0/1, unlike the normal one."""
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def summarise(pnls: Sequence[float], *, risk_unit_usd: float = 0.0) -> Stats:
    n = len(pnls)
    if n == 0:
        return Stats(risk_unit_usd=risk_unit_usd)
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    flats = n - len(wins) - len(losses)
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    mean = sum(pnls) / n
    var = sum((x - mean) ** 2 for x in pnls) / (n - 1) if n > 1 else 0.0
    lo, hi = wilson_interval(len(wins), n)
    return Stats(
        n=n, wins=len(wins), losses=len(losses), flats=flats,
        win_rate=len(wins) / n, ci_low=lo, ci_high=hi,
        avg_win_usd=(gross_win / len(wins)) if wins else 0.0,
        avg_loss_usd=(-gross_loss / len(losses)) if losses else 0.0,
        expectancy_usd=mean, stdev_usd=math.sqrt(var),
        profit_factor=(gross_win / gross_loss) if gross_loss else float("inf"),
        gross_win_usd=gross_win, gross_loss_usd=gross_loss,
        risk_unit_usd=risk_unit_usd,
    )

def shrink(observed: float, pooled: float, n: int, prior: float = PRIOR_TRADES) -> float:
    """Empirical-Bayes style pull toward the pooled mean, by sample size."""
    if n <= 0:
        return pooled
    weight = n / (n + prior)
    return weight * observed + (1 - weight) * pooled


def by_channel(outcomes: Iterable[Outcome], *, risk_unit_usd: float = 0.0,
               prior: float = PRIOR_TRADES) -> dict[str, Stats]:
    """Per-channel stats, with `adjusted_expectancy_usd` shrunk toward the pooled mean."""
    grouped: dict[str, list[float]] = {}
    for o in outcomes:
        grouped.setdefault(o.channel, []).append(o.pnl_usd)
    pooled = summarise([x for xs in grouped.values() for x in xs],
                       risk_unit_usd=risk_unit_usd).expectancy_usd
    out: dict[str, Stats] = {}
    for channel, pnls in grouped.items():
        stats = summarise(pnls, risk_unit_usd=risk_unit_usd)
        stats.adjusted_expectancy_usd = shrink(stats.expectancy_usd, pooled, stats.n, prior)
        out[channel] = stats
    return out


# `adjusted_expectancy_usd` stays a field on Stats (None until by_channel fills it), so
# summarise() keeps reporting raw measurements while the ranking can use the shrunk view.


def allocation(outcomes: Iterable[Outcome], *, risk_unit_usd: float = 0.0,
               prior: float = PRIOR_TRADES) -> list[tuple[str, float, Stats]]:
    """Rank channels by shrunk expectancy per unit of dispersion.

    The score is `adjusted expectancy / stdev`, so a channel that makes $600 a trade
    with $200 swings outranks one that makes $700 with $2,000 swings. Ties break on n,
    because more evidence is better evidence.
    """
    table = by_channel(outcomes, risk_unit_usd=risk_unit_usd, prior=prior)
    ranked = []
    for channel, stats in table.items():
        score = stats.adjusted / stats.stdev_usd if stats.stdev_usd else 0.0
        ranked.append((channel, score, stats))
    return sorted(ranked, key=lambda row: (-row[1], -row[2].n))


@dataclass
class Drawdown:
    max_drawdown_usd: float = 0.0
    max_drawdown_r: float = 0.0
    peak_usd: float = 0.0
    trough_usd: float = 0.0
    longest_losing_run: int = 0
    equity_curve: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"max_drawdown_usd": round(self.max_drawdown_usd, 2),
                "max_drawdown_r": round(self.max_drawdown_r, 4),
                "peak_usd": round(self.peak_usd, 2),
                "longest_losing_run": self.longest_losing_run}


def drawdown(pnls: Sequence[float], *, risk_unit_usd: float = 0.0) -> Drawdown:
    """Peak-to-trough on the realised path, in dollars and R."""
    equity = 0.0
    peak = 0.0
    worst = 0.0
    peak_at = 0.0
    trough_at = 0.0
    run = worst_run = 0
    curve: list[float] = []
    for pnl in pnls:
        equity += pnl
        curve.append(round(equity, 2))
        if equity > peak:
            peak = equity
        drop = peak - equity
        if drop > worst:
            worst, peak_at, trough_at = drop, peak, equity
        if pnl < 0:
            run += 1
            worst_run = max(worst_run, run)
        else:
            run = 0
    return Drawdown(max_drawdown_usd=worst,
                    max_drawdown_r=(worst / risk_unit_usd) if risk_unit_usd else 0.0,
                    peak_usd=peak_at, trough_usd=trough_at,
                    longest_losing_run=worst_run, equity_curve=curve)


def runs_test(pnls: Sequence[float]) -> dict[str, Any]:
    """Wald-Wolfowitz: do wins and losses cluster more than chance would produce?

    z < -1.96 means the sequence is streaky (losses arrive in bunches) - a loss-streak
    rule is then doing real work. z near 0 means winning and losing are independent,
    and a streak rule is theatre.
    """
    signs = [1 if x > 0 else -1 for x in pnls if x != 0]
    n = len(signs)
    if n < 2:
        return {"runs": 0, "expected": 0.0, "z": 0.0, "streaky": False, "n": n}
    n1 = signs.count(1)
    n2 = signs.count(-1)
    runs = 1 + sum(1 for a, b in zip(signs, signs[1:], strict=False) if a != b)
    expected = 1 + (2 * n1 * n2) / n
    var = (2 * n1 * n2 * (2 * n1 * n2 - n)) / (n * n * (n - 1)) if n > 1 else 0.0
    z = (runs - expected) / math.sqrt(var) if var > 0 else 0.0
    return {"runs": runs, "expected": round(expected, 2), "z": round(z, 3),
            "streaky": z < -Z95, "n": n, "wins": n1, "losses": n2}


def streak_odds(loss_rate: float, length: int, n_trades: int) -> float:
    """Expected number of `length`-loss runs inside `n_trades` at that loss rate.

    An approximation (overlapping windows), stated as such: it answers "how often should
    we expect to see three in a row", which is what a streak rule needs to be calibrated
    against rather than feared.
    """
    if not 0 < length <= n_trades or loss_rate <= 0:
        return 0.0
    return max(0, n_trades - length + 1) * (loss_rate ** length)


@dataclass
class DeskReport:
    overall: Stats
    channels: dict[str, Stats]
    ranking: list[tuple[str, float, Stats]]
    dd: Drawdown
    runs: dict[str, Any]
    streak_table: list[tuple[int, float]]
    risk_unit_usd: float

    def to_text(self) -> str:
        streak_verdict = ("streaky: losses cluster" if self.runs["streaky"]
                          else "consistent with independence")
        lines = [
            f"desk report | {self.overall.n} closed trades | 1R = ${self.risk_unit_usd:,.0f}",
            f"  expectancy ${self.overall.expectancy_usd:,.2f}/trade "
            f"({self.overall.expectancy_r:+.3f}R) | win rate {self.overall.win_rate:.1%} "
            f"[{self.overall.ci_low:.1%}-{self.overall.ci_high:.1%}]",
            f"  profit factor {self.overall.profit_factor:.2f} | "
            f"score {self.overall.per_trade_score:+.3f} | max DD ${self.dd.max_drawdown_usd:,.2f} "
            f"({self.dd.max_drawdown_r:.2f}R) | longest losing run {self.dd.longest_losing_run}",
            f"  runs test z={self.runs['z']:+.2f} ({streak_verdict})",
            "",
            "channel                 n    win%  [95% ci]     exp$/trade  adj$/trade  score",
        ]
        for channel, score, stats in self.ranking:
            lines.append(
                f"  {channel:<22}{stats.n:>3}  {stats.win_rate * 100:>5.1f}  "
                f"[{stats.ci_low * 100:>4.1f}-{stats.ci_high * 100:<4.1f}]  "
                f"{stats.expectancy_usd:>10,.2f}  {stats.adjusted:>10,.2f}  {score:+.3f}")
        if self.streak_table:
            lines.append("")
            lines.append("how often a losing run should appear at this loss rate:")
            for length, expected in self.streak_table:
                lines.append(f"  {length} in a row: {expected:.2f} expected over "
                             f"{self.overall.n} trades")
        return "\n".join(lines)


    def to_markdown(self) -> str:
        """The shareable artefact: what the record says, with its uncertainty shown."""
        o = self.overall
        streak_verdict = ("losses cluster more than chance" if self.runs["streaky"]
                          else "consistent with independence")
        lines = [
            "# Desk report — expectancy, drawdown, allocation",
            "",
            f"Source: the executor's own outcomes over the 93-alert corpus "
            f"(**{o.n} closed trades**). 1R = **${self.risk_unit_usd:,.0f}**.",
            "",
            "| metric | value |",
            "|---|---|",
            f"| expectancy | ${o.expectancy_usd:,.2f}/trade ({o.expectancy_r:+.3f}R) |",
            f"| win rate | {o.win_rate:.1%} (95% CI {o.ci_low:.1%}–{o.ci_high:.1%}) |",
            f"| profit factor | {o.profit_factor:.2f} |",
            f"| per-trade score | {o.per_trade_score:+.3f} |",
            f"| average win / loss | ${o.avg_win_usd:,.2f} / ${o.avg_loss_usd:,.2f} |",
            f"| max drawdown | ${self.dd.max_drawdown_usd:,.2f} ({self.dd.max_drawdown_r:.2f}R) |",
            f"| longest losing run | {self.dd.longest_losing_run} |",
            f"| runs test | z = {self.runs['z']:+.2f} ({streak_verdict}) |",
            "",
            "## Channel allocation",
            "",
            "| rank | channel | n | win rate | 95% CI | exp/trade | shrunk exp | score |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for rank, (channel, score, stats) in enumerate(self.ranking, start=1):
            lines.append(
                f"| {rank} | {channel} | {stats.n} | {stats.win_rate:.1%} | "
                f"{stats.ci_low:.0%}–{stats.ci_high:.0%} | ${stats.expectancy_usd:,.2f} | "
                f"${stats.adjusted:,.2f} | {score:+.3f} |")
        lines += [
            "",
            "Shrunk expectancy pulls each channel toward the pooled mean by sample size "
            "(`PRIOR_TRADES` = 10 pseudo-trades), so a 7-trade channel cannot outrank a "
            "41-trade channel on luck. Score = shrunk expectancy ÷ per-trade dispersion.",
            "",
            "## Expectation of losing runs",
            "",
            "| run length | expected occurrences over this record |",
            "|---|---|",
        ]
        for length, expected in self.streak_table:
            lines.append(f"| {length} in a row | {expected:.2f} |")
        lines += [
            "",
            "If the observed longest run is close to what independence predicts, a "
            "loss-streak rule buys discipline, not edge. If it is far above, streaks are "
            "structural and the rule is doing real work.",
            "",
            "_generated by `python -m scorpion_mff.cli desk --md reports/desk-report.md`_",
            "",
        ]
        return "\n".join(lines)


def report(outcomes: Iterable[Outcome], *, risk_unit_usd: float = 0.0,
           lengths: Sequence[int] = (2, 3, 4, 5)) -> DeskReport:
    rows = sorted(outcomes, key=lambda o: o.ts)
    overall = summarise([o.pnl_usd for o in rows], risk_unit_usd=risk_unit_usd)
    channels = by_channel(rows, risk_unit_usd=risk_unit_usd)
    loss_rate = 1 - overall.win_rate if overall.n else 0.0
    return DeskReport(
        overall=overall, channels=channels,
        ranking=allocation(rows, risk_unit_usd=risk_unit_usd),
        dd=drawdown([o.pnl_usd for o in rows], risk_unit_usd=risk_unit_usd),
        runs=runs_test([o.pnl_usd for o in rows]),
        streak_table=[(n, streak_odds(loss_rate, n, overall.n)) for n in lengths],
        risk_unit_usd=risk_unit_usd,
    )
