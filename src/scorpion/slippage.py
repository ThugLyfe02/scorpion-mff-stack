"""Impact per size: the flat slippage assumption should be a curve.

The branch measured entry latency as a flat penalty - 20% of the ask on every clip - and
found it cost 31.53R against a 29.53R exit haircut. A flat penalty is a placeholder. The
square-root law says impact scales with the square root of size, so doubling a clip
costs about 1.41x rather than 2x, and the cost is a power law: a quiet, thin session
inflates it non-linearly, which is exactly when a halt should bind.

This fits the exponent rather than assuming the square root. If the exponent is within
two standard errors of one, the linear model was adequate and the module says so.

A fit on simulated costs recovers the assumption that generated them, so this is
evidence only once the fills are real. It refuses below a minimum number of distinct
sizes for that reason.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class SlippageError(ValueError):
    """An impact question that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


MIN_POINTS = 8
MIN_SIZES = 3


@dataclass(frozen=True, slots=True)
class Fit:
    exponent: float
    stderr: float
    intercept: float
    points: int
    sizes: int

    @property
    def concave(self) -> bool:
        """Impact grows more slowly than size - the square-root regime."""
        return self.exponent < 0.9

    @property
    def linear_not_rejected(self) -> bool:
        """The exponent is within two standard errors of one, so linearity survives."""
        return abs(self.exponent - 1.0) <= 2.0 * self.stderr

    def to_dict(self) -> dict[str, Any]:
        return {
            "exponent": round(self.exponent, 4),
            "stderr": round(self.stderr, 4),
            "intercept": round(self.intercept, 4),
            "points": self.points,
            "sizes": self.sizes,
            "concave": self.concave,
            "linear_not_rejected": self.linear_not_rejected,
            "verdict": (
                "linear_adequate" if self.linear_not_rejected
                else "concave_impact" if self.concave
                else "convex_impact"
            ),
        }


def observations(
    fills: Sequence[Mapping[str, Any]], *, cost_key: str = "cost_bps",
    size_key: str = "contracts",
) -> list[tuple[float, float]]:
    """(contracts, cost in bps) pairs, refusing non-positive values which cannot be logged."""
    out: list[tuple[float, float]] = []
    for row in fills:
        raw_size = row.get(size_key)
        raw_cost = row.get(cost_key)
        if raw_size is None or raw_cost is None:
            continue
        size = float(raw_size)
        cost = float(raw_cost)
        if size <= 0 or cost < 0:
            continue
        out.append((size, cost))
    return out


def _ols(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        raise SlippageError("degenerate", "every clip is the same size")
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys, strict=False)]
    variance = sum(r * r for r in residuals) / (n - 2) if n > 2 else 0.0
    return slope, intercept, math.sqrt(variance / sxx) if variance > 0 else 0.0


def fit_impact(observations_: Sequence[tuple[float, float]]) -> Fit:
    """Fit log cost against log size; the slope IS the impact exponent."""
    if len(observations_) < MIN_POINTS:
        raise SlippageError("thin_sample", f"{len(observations_)} observations")
    sizes = {size for size, _cost in observations_}
    if len(sizes) < MIN_SIZES:
        raise SlippageError("thin_sizes", f"{len(sizes)} distinct clip sizes")
    xs: list[float] = []
    ys: list[float] = []
    for size, cost in observations_:
        if cost <= 0:
            # a zero measured cost cannot be logged; it is not evidence of zero impact
            continue
        xs.append(math.log(size))
        ys.append(math.log(cost))
    if len(xs) < MIN_POINTS:
        raise SlippageError("thin_sample", f"{len(xs)} positive costs")
    slope, intercept, stderr = _ols(xs, ys)
    return Fit(
        exponent=slope, stderr=stderr, intercept=intercept, points=len(xs), sizes=len(sizes)
    )


def predict(fit: Fit, *, contracts: float, reference_cost: float, reference_size: float) -> float:
    """Cost at a new clip, anchored to a measured point so the intercept need not be trusted."""
    if contracts <= 0 or reference_size <= 0:
        raise SlippageError("bad_size", f"{contracts} against {reference_size}")
    if reference_cost <= 0:
        raise SlippageError("bad_cost", str(reference_cost))
    return float(reference_cost * (contracts / reference_size) ** fit.exponent)


def table(
    fit: Fit, *, reference_cost: float, reference_size: float,
    sizes: Sequence[float] = (1, 2, 5, 10, 20, 40),
) -> list[dict[str, Any]]:
    """The curve, plus what a linear model would have claimed, so the difference is visible."""
    rows: list[dict[str, Any]] = []
    for size in sizes:
        curved = predict(
            fit, contracts=size, reference_cost=reference_cost, reference_size=reference_size
        )
        linear = reference_cost * (size / reference_size)
        rows.append({
            "contracts": size,
            "cost_bps_curve": round(curved, 3),
            "cost_bps_linear": round(linear, 3),
            "linear_overstates_by_bps": round(linear - curved, 3),
        })
    return rows


def report(fills: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    observed = observations(fills)
    fitted = fit_impact(observed)
    biggest = max((size for size, _cost in observed), default=0.0)
    anchor = next(
        ((size, cost) for size, cost in observed if size == biggest), (0.0, 0.0)
    )
    payload = fitted.to_dict()
    payload["anchor"] = {"contracts": anchor[0], "cost_bps": round(anchor[1], 3)}
    if anchor[0] > 0 and anchor[1] > 0:
        payload["table"] = table(
            fitted, reference_cost=anchor[1], reference_size=anchor[0]
        )
    payload["caveats"] = [
        "an exponent fitted to simulated costs recovers the assumption that generated them, so "
        "this is evidence only once the fills are real",
        "impact is measured against absolute size, not size relative to volume, because the corpus "
        "does not carry volume by bar - that omission biases the exponent toward zero",
        "zero-cost fills are dropped rather than treated as zero impact, which would flatten "
        "the curve",
    ]
    return payload


def render(payload: Mapping[str, Any]) -> str:
    lines = [
        f"impact exponent {payload['exponent']:.3f} +/- {payload['stderr']:.3f} on "
        f"{payload['points']} fills across {payload['sizes']} sizes ({payload['verdict']})"
    ]
    for row in payload.get("table", []):
        lines.append(
            f"  {row['contracts']:3.0f} contracts  curve {row['cost_bps_curve']:7.2f} bps   "
            f"linear {row['cost_bps_linear']:7.2f}"
        )
    return "\n".join(lines)
