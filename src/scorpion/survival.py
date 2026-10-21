"""Survival: how long a position actually lives, with censoring handled honestly.

A holding time is not a number you can average. Some positions exit; others are still open when the
session ends, and their true duration is unknown - it is *at least* what was observed. Dropping them
biases the average down (you only measure the quick exits); treating them as completed biases it up
(you invent an exit that never happened). Both are common, and both make the exit policy look better
or worse than it is.

This module estimates the distribution with censoring, the way the problem actually is:

* each observation is `(minutes, censored)`, censored meaning "still open when we stopped";
* the Kaplan-Meier product-limit estimator gives the survival curve, so the median holding time is a
  statement about the data rather than about the survivors;
* the hazard is reported per time bucket, because the interesting question is not "how long on
  average" but "does the risk of exit rise or fall with time" - a falling hazard means the trade
  either works immediately or never does, which is a different exit policy than a flat one;
* comparisons refuse below a floor of observations, in the same style as the TCA and mark-out
  layers, because a 4-sample median is a rumour with a decimal point.

The unit that matters for a 0DTE desk is minutes, and the clock is the one the ledger already
normalised - the corpus's `time_et` column is Pacific, proved by measurement.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .sessions import session_for

MIN_SAMPLE = 5


class SurvivalError(ValueError):
    """A sample that cannot support the estimate, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Observation:
    """One position's holding time. `censored` means it had not exited when observation stopped."""

    minutes: float
    censored: bool = False
    channel: str = ""
    identity: str = ""

    def __post_init__(self) -> None:
        if self.minutes < 0:
            raise SurvivalError("negative_duration", f"{self.minutes} for {self.identity}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "minutes": round(self.minutes, 2), "censored": self.censored,
            "channel": self.channel, "identity": self.identity,
        }


@dataclass(frozen=True, slots=True)
class Curve:
    """A Kaplan-Meier curve: survival probability after each observed exit time."""

    points: tuple[tuple[float, float], ...]      # (minutes, survival probability)
    n: int
    events: int
    censored: int

    @property
    def censoring_rate(self) -> float:
        return round(100.0 * self.censored / self.n, 1) if self.n else 0.0

    def survival_at(self, minutes: float) -> float:
        """Step function: the probability of still being open after `minutes`."""
        value = 1.0
        for point_minutes, point_survival in self.points:
            if point_minutes <= minutes:
                value = point_survival
            else:
                break
        return value

    def median(self) -> float | None:
        """The smallest time whose survival is at or below 0.5, or None if it never is."""
        for minutes, survival in self.points:
            if survival <= 0.5:
                return minutes
        return None

    def quantile(self, q: float) -> float | None:
        if not 0.0 < q < 1.0:
            raise SurvivalError("bad_quantile", str(q))
        target = 1.0 - q
        for minutes, survival in self.points:
            if survival <= target:
                return minutes
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n, "events": self.events, "censored": self.censored,
            "censoring_rate_pct": self.censoring_rate,
            "median_minutes": self.median(),
            "p25_minutes": self.quantile(0.25),
            "p75_minutes": self.quantile(0.75),
            "curve": [[round(m, 2), round(s, 4)] for m, s in self.points],
        }


def kaplan_meier(observations: Sequence[Observation]) -> Curve:
    """Product-limit estimate. Events and censoring at the same time are ordered events-first."""
    if not observations:
        return Curve(points=(), n=0, events=0, censored=0)

    ordered = sorted(observations, key=lambda o: (o.minutes, o.censored))
    at_risk = len(ordered)
    survival = 1.0
    points: list[tuple[float, float]] = []
    events = 0
    censored = 0

    index = 0
    while index < len(ordered):
        moment = ordered[index].minutes
        deaths = 0
        losses = 0
        while index < len(ordered) and ordered[index].minutes == moment:
            if ordered[index].censored:
                losses += 1
            else:
                deaths += 1
            index += 1

        if deaths and at_risk:
            survival *= 1.0 - deaths / at_risk
            points.append((moment, survival))
            events += deaths
        censored += losses
        at_risk -= deaths + losses

    return Curve(points=tuple(points), n=len(ordered), events=events, censored=censored)


def hazard_table(
    observations: Sequence[Observation], *, buckets: Sequence[float] = (5, 15, 30, 60, 120),
) -> list[dict[str, Any]]:
    """Conditional exit probability per bucket: P(exit in bucket | survived to its start)."""
    if not observations:
        return []
    edges = [0.0, *buckets]
    rows: list[dict[str, Any]] = []
    for index in range(len(edges) - 1):
        start, end = edges[index], edges[index + 1]
        at_risk = sum(1 for o in observations if o.minutes >= start)
        exits = sum(1 for o in observations if start <= o.minutes < end and not o.censored)
        if at_risk == 0:
            continue
        rows.append({
            "bucket": f"{start:g}-{end:g}m",
            "start": start, "end": end,
            "at_risk": at_risk, "exits": exits,
            "hazard_pct": round(100.0 * exits / at_risk, 2),
        })
    return rows


def hazards_are_falling(rows: Sequence[dict[str, Any]]) -> bool | None:
    """A falling hazard means a trade either works immediately or not at all.

    Reported rather than asserted: with small buckets the estimate is noisy, and a desk that acts on
    a noisy slope is worse off than one that knows it does not know.
    """
    usable = [row for row in rows if row["at_risk"] >= MIN_SAMPLE]
    if len(usable) < 2:
        return None
    first, last = usable[0]["hazard_pct"], usable[-1]["hazard_pct"]
    return bool(last < first)


def from_ledger(ledger: Any, *, assume_held_minutes: float = 60.0) -> list[Observation]:
    """Holding times from the corpus.

    The ledger records an exit PRICE, not an exit time, so a trade with an exit is treated as held
    for `assume_held_minutes`; a trade without one is censored at the session close. Both are
    assumptions and both are reported - inventing a per-trade exit time from a price would be
    fabrication dressed as data.
    """
    observations: list[Observation] = []
    for trade in ledger.trades:
        session = session_for(trade.day)
        if session is None:
            continue
        if trade.exit_price is not None or trade.lifecycle.exit_pct is not None:
            observations.append(Observation(
                minutes=assume_held_minutes, censored=False, channel=str(trade.channel),
                identity=trade.identity,
            ))
        else:
            remaining = float((session.close_utc - trade.ts_utc).total_seconds() / 60)
            observations.append(Observation(
                minutes=max(0.0, remaining), censored=True, channel=str(trade.channel),
                identity=trade.identity,
            ))
    return observations


def by_channel(observations: Sequence[Observation]) -> dict[str, dict[str, Any]]:
    """Per-channel curves, withholding any channel below the sample floor."""
    grouped: dict[str, list[Observation]] = {}
    for observation in observations:
        grouped.setdefault(observation.channel or "unknown", []).append(observation)

    out: dict[str, dict[str, Any]] = {}
    for channel, sample in sorted(grouped.items()):
        if len(sample) < MIN_SAMPLE:
            out[channel] = {"n": len(sample), "withheld": f"below the floor of {MIN_SAMPLE}"}
            continue
        curve = kaplan_meier(sample)
        out[channel] = {
            "n": curve.n, "events": curve.events, "censored": curve.censored,
            "censoring_rate_pct": curve.censoring_rate,
            "median_minutes": curve.median(),
            "hazards": hazard_table(sample),
            "hazard_falling": hazards_are_falling(hazard_table(sample)),
        }
    return out


def compare(a: Sequence[Observation], b: Sequence[Observation], *, label_a: str = "a",
            label_b: str = "b") -> dict[str, Any]:
    """Two groups, with the refusal that matters stated before any difference is reported."""
    if len(a) < MIN_SAMPLE or len(b) < MIN_SAMPLE:
        return {
            "compared": False,
            "reason": f"floor is {MIN_SAMPLE}; got {len(a)} and {len(b)}",
        }
    curve_a, curve_b = kaplan_meier(a), kaplan_meier(b)
    return {
        "compared": True,
        label_a: curve_a.to_dict(),
        label_b: curve_b.to_dict(),
        "median_difference_minutes": (
            (curve_a.median() or math.nan) - (curve_b.median() or math.nan)
        ),
        "note": (
            "medians use the product-limit estimator; if either curve never crosses 0.5 the median "
            "is None and the difference is not reported"
        ),
    }


def report(observations: Sequence[Observation]) -> dict[str, Any]:
    curve = kaplan_meier(observations)
    rows = hazard_table(observations)
    return {
        "observations": len(observations),
        "curve": curve.to_dict(),
        "hazards": rows,
        "hazard_falling": hazards_are_falling(rows),
        "by_channel": by_channel(observations),
        "floor": MIN_SAMPLE,
        "caveats": [
            "the corpus records exit prices, not exit times; observed durations are an assumption "
            "and are reported as one",
    """Minutes from entry to the session close: the censoring bound for an open position."""
            "is a lower bound, not a duration",
            "with fewer than five exits in a channel the curve is withheld rather than printed",
        ],
    }


def render(payload: dict[str, Any]) -> str:
    curve = payload["curve"]
    lines = [
        f"{curve['n']} observations  {curve['events']} exits  {curve['censored']} censored "
        f"({curve['censoring_rate_pct']}%)",
        f"  median {curve['median_minutes']}m   p25 {curve['p25_minutes']}m   "
        f"p75 {curve['p75_minutes']}m",
    ]
    for row in payload["hazards"]:
        lines.append(
            f"  {row['bucket']:>8s}  at risk {row['at_risk']:3d}  exits {row['exits']:3d}  "
            f"hazard {row['hazard_pct']:5.1f}%"
        )
    if payload["hazard_falling"] is not None:
        lines.append(f"  hazard falling: {payload['hazard_falling']}")
    return "\n".join(lines)


def session_remaining_minutes(trade: Any) -> float | None:
    """Minutes from entry to session close: the censoring bound for an open position."""
    session = session_for(trade.day)
    if session is None:
        return None
    remaining = float((session.close_utc - trade.ts_utc).total_seconds() / 60)
    return max(0.0, remaining)


def bucket_of(minutes: float, edges: Sequence[float] = (5, 15, 30, 60, 120)) -> str:
    for edge in edges:
        if minutes < edge:
            return f"<{edge:g}m"
    return f">={edges[-1]:g}m"


def distribution(observations: Iterable[Observation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for observation in observations:
        label = bucket_of(observation.minutes)
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[0]))
