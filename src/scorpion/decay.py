"""Alpha decay: how fast the edge in an alert rots, measured from forward returns.

A Discord alert has a shelf life. If the move is over in five minutes, an executor acting in ninety
seconds captures most of it and one acting in ten minutes captures none. That curve decides how much
engineering the hot path is worth, and here it has been inferred only from the cost side, where 20%
entry slippage cost 31.53R.

This measures it from the signal side: for each entry, the forward return to the same contract at a
set of horizons, averaged across trades.

    decay_curve   mean forward return at 5, 15, 30 and 60 minutes, with n and standard error
    half_life     the time for that mean to halve, fitted only when the path is monotone and the
                  shortest horizon is above noise; otherwise `not_identified`

The refusals are the substance: below five measurable trades nothing is fitted, a non-monotone path
is reported as such, and a shortest-horizon mean inside one standard error returns `not_identified`
rather than a large half-life.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

MIN_TRADES = 5
DEFAULT_HORIZONS = (5, 15, 30, 60)


class DecayError(ValueError):
    """A decay measurement that cannot be made, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Point:
    minutes: int
    n: int
    mean_return: float
    stderr: float

    @property
    def above_noise(self) -> bool:
        return self.n >= MIN_TRADES and abs(self.mean_return) > self.stderr

    def to_dict(self) -> dict[str, Any]:
        return {
            "minutes": self.minutes, "n": self.n,
            "mean_return": round(self.mean_return, 5),
            "stderr": round(self.stderr, 5),
            "above_noise": self.above_noise,
        }


def forward_return(entry: float, mark: float) -> float:
    if entry <= 0:
        raise DecayError("bad_entry", str(entry))
    return mark / entry - 1.0


def marks_after(barset: Any, entry_ts: Any, horizons: Sequence[int]) -> dict[int, float]:
    """The close of the last bar at or before entry + horizon, for each horizon."""
    out: dict[int, float] = {}
    if not barset.bars:
        return out
    last_bar = barset.bars[-1].ts_utc
    for minutes in horizons:
        cutoff = entry_ts + timedelta(minutes=minutes)
        if cutoff > last_bar:
            # a horizon past the last bar has no observation; clamping to the final bar would
            # report a 600-minute mark from a 10-minute-old price
            continue
        chosen = None
        for bar in barset.bars:
            if bar.ts_utc <= cutoff:
                chosen = bar
            else:
                break
        if chosen is not None and chosen.ts_utc >= entry_ts:
            out[minutes] = float(chosen.close)
    return out


def curve(
    trades: Sequence[tuple[float, Mapping[int, float]]],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    floor: int = MIN_TRADES,
) -> list[Point]:
    """Mean forward return per horizon from (entry price, {horizon: mark}) pairs."""
    points: list[Point] = []
    for minutes in horizons:
        returns = [
            forward_return(entry, marks[minutes])
            for entry, marks in trades
            if minutes in marks and entry > 0
        ]
        if len(returns) < floor:
            points.append(Point(minutes, len(returns), 0.0, 0.0))
            continue
        mean = sum(returns) / len(returns)
        variance = (
            sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
            if len(returns) > 1 else 0.0
        )
        points.append(Point(minutes, len(returns), mean, math.sqrt(variance / len(returns))))
    return points


def is_monotone_decreasing(points: Sequence[Point]) -> bool:
    usable = [p for p in points if p.n >= MIN_TRADES]
    return all(a.mean_return >= b.mean_return for a, b in zip(usable, usable[1:], strict=False))


def half_life(points: Sequence[Point]) -> dict[str, Any]:
    """Fit t such that the mean return halves. Refuses anything it cannot support."""
    usable = [p for p in points if p.n >= MIN_TRADES]
    if len(usable) < 3:
        return {"identified": False, "reason": f"need 3 horizons with >= {MIN_TRADES} trades"}
    first = usable[0]
    if not first.above_noise:
        return {"identified": False, "reason": "shortest horizon is inside one standard error"}
    if first.mean_return <= 0:
        return {"identified": False, "reason": "shortest horizon is not positive"}
    if not is_monotone_decreasing(usable):
        return {"identified": False, "reason": "path is not monotone, so a half-life is undefined"}

    target = first.mean_return / 2.0
    for previous, current in zip(usable, usable[1:], strict=False):
        if current.mean_return <= target:
            span = previous.mean_return - current.mean_return
            if span <= 0:
                return {"identified": False, "reason": "no decay between adjacent horizons"}
            fraction = (previous.mean_return - target) / span
            minutes = previous.minutes + fraction * (current.minutes - previous.minutes)
            return {
                "identified": True,
                "half_life_minutes": round(minutes, 1),
                "from_minutes": previous.minutes,
                "to_minutes": current.minutes,
            }
    return {
        "identified": False,
        "reason": "return never halved inside the measured horizons",
        "last_mean_return": round(usable[-1].mean_return, 5),
    }


def report(points: Sequence[Point]) -> dict[str, Any]:
    fitted = half_life(points)
    return {
        "points": [p.to_dict() for p in points],
        "half_life": fitted,
        "monotone": is_monotone_decreasing(points) if points else None,
        "floor": MIN_TRADES,
        "caveats": [
            "forward returns use the last bar at or before the horizon, not an interpolated mark",
            "a path that is not monotone has no half-life; the refusal is the finding",
            "measured from the alert price, so it measures the signal's decay and not our fill",
        ],
    }


def render(payload: Mapping[str, Any]) -> str:
    lines = ["horizon   n   mean return   stderr"]
    for point in payload["points"]:
        lines.append(
            f"{point['minutes']:>5d}m {point['n']:>3d}  {point['mean_return']:+.4f}      "
            f"{point['stderr']:.4f}"
        )
    fitted = payload["half_life"]
    if fitted.get("identified"):
        lines.append(f"half-life {fitted['half_life_minutes']}m")
    else:
        lines.append(f"no half-life: {fitted.get('reason')}")
    return "\n".join(lines)


def from_ledger(ledger: Any, barsets: Mapping[str, Any],
                horizons: Sequence[int] = DEFAULT_HORIZONS) -> list[Point]:
    """Build the curve from the corpus, using only trades whose bars cover the entry."""
    pairs: list[tuple[float, dict[int, float]]] = []
    for trade in ledger.trades:
        key = f"{trade.ticker}|{trade.side}|{trade.strike}|{trade.day.isoformat()}"
        barset = barsets.get(key)
        if barset is None:
            continue
        marks = marks_after(barset, trade.ts_utc, horizons)
        if marks:
            pairs.append((float(trade.entry_bid), marks))
    return curve(pairs, horizons=horizons)


def summarise(payload: Mapping[str, Any]) -> dict[str, Any]:
    fitted = payload["half_life"]
    usable = [p for p in payload["points"] if p["n"] >= MIN_TRADES]
    return {
        "horizons_measured": len(usable),
        "half_life_minutes": fitted.get("half_life_minutes"),
        "identified": bool(fitted.get("identified")),
        "reason": fitted.get("reason"),
    }
