"""Regime: classifying the tape, and pricing the edge inside each regime.

A single mean R across 107 trades averages conditions that do not resemble each other. A 0DTE index
call bought into a calm drift and the same contract bought into a volatile chop are different bets
with the same name, and blending them produces a number that describes neither.

This module labels each session from its own bars, then splits the corpus by label:

* volatility from realised close-to-close variance and average true range, at session scale so the
  thresholds are comparable across contracts at very different prices;
* trend from the slope of a least-squares fit on log closes, scaled by the session's own volatility,
  so a $0.02 drift in a $0.50 contract and a $2 drift in a $50 contract are not confused;
* four regimes from the cross of those axes - the smallest grid that separates "works" from "works
  right now";
* conditional performance with a floor, because a six-trade cell is a story and the report says so;
* a transition matrix, because the useful question is not which regime we are in but how long one
  lasts - a regime that flips every session cannot be traded as a filter.

Everything is computed from bars rather than assumed. Where bars are missing the session is labelled
`unknown` and excluded from conditional statistics rather than folded into a default.

The deliberate omission: no prediction. This labels the past and measures what happened inside it. A
classifier that claimed to forecast the next session would need out-of-sample evidence this corpus
cannot supply, and the honest product is the conditional table, not a signal.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

D = Decimal

MIN_CELL = 5
REGIMES = ("calm_trend", "calm_chop", "volatile_trend", "volatile_chop")


class RegimeError(ValueError):
    """A session that cannot be classified, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Where calm becomes volatile and chop becomes trend. Session scale, not annualised."""

    vol_split: float = 0.35        # realised session volatility
    trend_split: float = 0.25      # |slope| per unit of session volatility
    min_bars: int = 5

    def __post_init__(self) -> None:
        if self.vol_split <= 0 or self.trend_split <= 0:
            raise RegimeError("bad_threshold", "splits must be positive")
        if self.min_bars < 2:
            raise RegimeError("bad_threshold", "min_bars must be at least 2")

    def to_dict(self) -> dict[str, float]:
        return {"vol_split": self.vol_split, "trend_split": self.trend_split,
                "min_bars": float(self.min_bars)}


@dataclass(frozen=True, slots=True)
class Features:
    """What the tape looked like, computed rather than assumed."""

    day: date
    bars: int
    closes: tuple[float, ...]
    realised_vol: float
    atr_pct: float
    slope: float
    trend_strength: float
    total_return: float
    range_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(), "bars": self.bars,
            "realised_vol": round(self.realised_vol, 5),
            "atr_pct": round(self.atr_pct, 5),
            "slope": round(self.slope, 6),
            "trend_strength": round(self.trend_strength, 4),
            "total_return": round(self.total_return, 5),
            "range_pct": round(self.range_pct, 5),
        }


@dataclass(frozen=True, slots=True)
class Classified:
    features: Features
    regime: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**self.features.to_dict(), "regime": self.regime, "reason": self.reason}


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return math.sqrt(sum((v - average) ** 2 for v in values) / (len(values) - 1))


def features_from_closes(day: date, closes: Sequence[float],
                         highs: Sequence[float] | None = None,
                         lows: Sequence[float] | None = None) -> Features:
    """Session features from a close series, with ATR from highs/lows when available."""
    if len(closes) < 2:
        raise RegimeError("too_few_closes", f"{len(closes)} closes on {day.isoformat()}")
    if any(c <= 0 for c in closes):
        raise RegimeError("non_positive_close", day.isoformat())

    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    realised = _stdev(log_returns) * math.sqrt(len(log_returns))

    # Slope of log closes against bar index, normalised by the session's own volatility so a drift
    # is comparable across contracts trading at very different prices.
    n = len(closes)
    xs = list(range(n))
    x_mean = _mean([float(x) for x in xs])
    y = [math.log(c) for c in closes]
    y_mean = _mean(y)
    numerator = sum((x - x_mean) * (yi - y_mean) for x, yi in zip(xs, y, strict=False))
    denominator = sum((x - x_mean) ** 2 for x in xs)
    slope = numerator / denominator if denominator else 0.0
    strength = slope / realised if realised > 0 else 0.0

    if highs is not None and lows is not None and len(highs) == len(lows) == n:
        true_ranges = [
            max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            for i in range(1, n)
        ]
        atr_pct = (_mean(true_ranges) / _mean(closes)) if true_ranges else 0.0
    else:
        atr_pct = (_mean([abs(closes[i] - closes[i - 1]) for i in range(1, n)])
                   / _mean(closes))

    return Features(
        day=day, bars=n, closes=tuple(closes), realised_vol=realised, atr_pct=atr_pct,
        slope=slope, trend_strength=strength,
        total_return=(closes[-1] / closes[0] - 1.0),
        range_pct=((max(closes) - min(closes)) / _mean(closes)),
    )


def classify(features: Features, thresholds: Thresholds | None = None) -> Classified:
    """Label a session on the volatility x trend cross."""
    thresholds = thresholds or Thresholds()
    if features.bars < thresholds.min_bars:
        return Classified(features, "unknown", f"{features.bars} bars < {thresholds.min_bars}")

    volatile = features.realised_vol >= thresholds.vol_split
    trending = abs(features.trend_strength) >= thresholds.trend_split
    label = ("volatile_" if volatile else "calm_") + ("trend" if trending else "chop")
    reason = (
        f"vol {features.realised_vol:.3f} {'>=' if volatile else '<'} {thresholds.vol_split}, "
        f"|trend| {abs(features.trend_strength):.2f} "
        f"{'>=' if trending else '<'} {thresholds.trend_split}"
    )
    return Classified(features, label, reason)


def classify_barset(barset: Any, *, thresholds: Thresholds | None = None) -> Classified:
    """Classify from a bars.BarSet, using its highs and lows for a real ATR."""
    closes = [float(bar.close) for bar in barset.bars]
    highs = [float(bar.high) for bar in barset.bars]
    lows = [float(bar.low) for bar in barset.bars]
    return classify(features_from_closes(barset.day, closes, highs, lows), thresholds)


def classify_sessions(
    barsets: Mapping[str, Any], *, thresholds: Thresholds | None = None,
) -> dict[str, Classified]:
    """Classify every set, marking the unclassifiable as unknown rather than guessing."""
    out: dict[str, Classified] = {}
    for key, barset in barsets.items():
        try:
            out[key] = classify_barset(barset, thresholds=thresholds)
        except RegimeError as exc:
            out[key] = Classified(
                Features(day=barset.day, bars=0, closes=(), realised_vol=0.0, atr_pct=0.0,
                         slope=0.0, trend_strength=0.0, total_return=0.0, range_pct=0.0),
                "unknown", f"{exc.code}: {exc.detail}",
            )
    return out


def conditional_r(
    ledger: Any, classified: Mapping[str, Classified], *, min_cell: int = MIN_CELL,
) -> dict[str, Any]:
    """Mean R per regime, withholding any cell below the floor.

        total_return=(closes[-1] / closes[0] - 1.0),
    - a trade's own key and a bar set's contract key are the same string by construction.
    """
    buckets: dict[str, list[float]] = {}
    unclassified = 0
    for trade in ledger.trades:
        key = f"{trade.ticker}|{trade.side}|{trade.strike}|{trade.day.isoformat()}"
        entry = classified.get(key)
        r_multiple = trade.r_multiple
        if entry is None or entry.regime == "unknown" or r_multiple is None:
            unclassified += 1
            continue
        buckets.setdefault(entry.regime, []).append(float(r_multiple))

    out: dict[str, Any] = {}
    for regime in REGIMES:
        values = buckets.get(regime, [])
        if len(values) < min_cell:
            out[regime] = {"n": len(values), "withheld": f"below the floor of {min_cell}"}
            continue
        wins = [v for v in values if v > 0]
        out[regime] = {
            "n": len(values),
            "mean_r": round(_mean(values), 4),
            "total_r": round(sum(values), 2),
            "win_rate": round(100.0 * len(wins) / len(values), 1),
            "stdev_r": round(_stdev(values), 4),
        }
    return {
        "regimes": out,
        "unclassified_trades": unclassified,
        "classified_trades": sum(len(v) for v in buckets.values()),
        "min_cell": min_cell,
    }


def transition_matrix(labels: Sequence[tuple[date, str]]) -> dict[str, Any]:
    """Day-to-day regime transitions: how long a regime lasts, not what comes next."""
    ordered = sorted(labels)
    states = list(REGIMES) + ["unknown"]
    counts = {state: 0 for state in states}
    transitions = {state: {target: 0 for target in states} for state in states}

    for index in range(len(ordered) - 1):
        current, nxt = ordered[index][1], ordered[index + 1][1]
        counts[current] = counts.get(current, 0) + 1
        transitions.setdefault(current, {t: 0 for t in states})[nxt] = (
            transitions.setdefault(current, {}).get(nxt, 0) + 1
        )

    rows: dict[str, dict[str, float]] = {}
    for state, targets in transitions.items():
        total = sum(targets.values())
        if not total:
            continue
        rows[state] = {
            target: round(100.0 * value / total, 1)
            for target, value in targets.items() if value
        }

    persistence = {
        state: rows.get(state, {}).get(state, 0.0) for state in REGIMES if state in rows
    }
    return {
        "transitions": rows,
        "persistence_pct": persistence,
        "note": (
            "a regime that persists under ~40% of the time is not a filter, it is a coin flip with "
            "a label"
        ),
    }


def persistable(labels: Mapping[str, Classified]) -> list[tuple[date, str]]:
    return sorted((entry.features.day, entry.regime) for entry in labels.values())


def report(
    ledger: Any, barsets: Mapping[str, Any], *, thresholds: Thresholds | None = None,
    min_cell: int = MIN_CELL,
) -> dict[str, Any]:
    classified = classify_sessions(barsets, thresholds=thresholds)
    performance = conditional_r(ledger, classified, min_cell=min_cell)
    matrix = transition_matrix(persistable(classified))
    counts: dict[str, int] = {}
    for entry in classified.values():
        counts[entry.regime] = counts.get(entry.regime, 0) + 1
    return {
        "thresholds": (thresholds or Thresholds()).to_dict(),
        "sessions": len(classified),
        "regime_counts": dict(sorted(counts.items())),
        "performance": performance,
        "transitions": matrix,
        "sessions_detail": {key: entry.to_dict() for key, entry in sorted(classified.items())},
        "caveats": [
            "classification is descriptive: it labels the session that produced the bars, not the "
            "next one",
            "a cell with fewer than five trades is withheld rather than averaged",
            "sessions without bars are 'unknown' and excluded from performance, never defaulted",
        ],
    }


def render(payload: dict[str, Any]) -> str:
    lines = [f"{payload['sessions']} sessions classified  {payload['regime_counts']}"]
    for regime, entry in payload["performance"]["regimes"].items():
        if "withheld" in entry:
            lines.append(f"  {regime:16s} n={entry['n']:3d}  withheld")
        else:
            lines.append(
                f"  {regime:16s} n={entry['n']:3d}  mean {entry['mean_r']:+.3f}R  "
                f"win {entry['win_rate']:5.1f}%  total {entry['total_r']:+.1f}R"
            )
    persistence = payload["transitions"]["persistence_pct"]
    if persistence:
        lines.append("  persistence: " + ", ".join(
            f"{k} {v:.0f}%" for k, v in sorted(persistence.items())
        ))
    return "\n".join(lines)


def from_ledger_days(ledger: Any) -> list[date]:
    """Every distinct trading day in the corpus, for coverage checks against the bar store."""
    return sorted({trade.day for trade in ledger.trades})


def describe_thresholds(thresholds: Thresholds) -> str:
    return (
        f"vol>={thresholds.vol_split} and |trend|>={thresholds.trend_split} "
        f"on at least {thresholds.min_bars} bars"
    )


def summarise(payload: dict[str, Any]) -> dict[str, Any]:
    performance = payload["performance"]["regimes"]
    best = None
    for regime, entry in performance.items():
        if "mean_r" in entry and (best is None or entry["mean_r"] > performance[best]["mean_r"]):
            best = regime
    return {
        "sessions": payload["sessions"],
        "classified_trades": payload["performance"]["classified_trades"],
        "best_regime": best,
        "cells_withheld": sum(1 for e in performance.values() if "withheld" in e),
    }


def to_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """A flat, sortable view for diffing two runs."""
    return [
        {"day": detail["day"], "regime": detail["regime"],
         "realised_vol": detail["realised_vol"], "trend_strength": detail["trend_strength"]}
        for detail in payload["sessions_detail"].values()
    ]


def counts_by_regime(entries: Iterable[Classified]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.regime] = counts.get(entry.regime, 0) + 1
    return dict(sorted(counts.items()))
