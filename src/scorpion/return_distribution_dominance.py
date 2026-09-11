from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class DistributionDominanceStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class PairedReturn:
    event_id: str
    incumbent_return: float
    challenger_return: float

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id is required")
        if not math.isfinite(self.incumbent_return) or not math.isfinite(self.challenger_return):
            raise ValueError("returns must be finite")


@dataclass(frozen=True, slots=True)
class DistributionDominancePolicy:
    minimum_samples: int = 100
    probability_grid: tuple[float, ...] = (0.05, 0.10, 0.20, 0.30, 0.50, 0.75, 1.0)
    maximum_lower_partial_shortfall: float = 0.0
    maximum_q10_degradation: float = 0.0
    minimum_mean_improvement: float = 0.0

    def __post_init__(self) -> None:
        if self.minimum_samples <= 0:
            raise ValueError("minimum_samples must be positive")
        if not self.probability_grid:
            raise ValueError("probability_grid cannot be empty")
        if any(not 0 < value <= 1 for value in self.probability_grid):
            raise ValueError("probability_grid values must be in (0,1]")
        if tuple(sorted(set(self.probability_grid))) != self.probability_grid:
            raise ValueError("probability_grid must be strictly increasing")
        if self.maximum_lower_partial_shortfall < 0 or self.maximum_q10_degradation < 0:
            raise ValueError("degradation tolerances cannot be negative")


@dataclass(frozen=True, slots=True)
class DistributionPoint:
    probability: float
    incumbent_quantile: float
    challenger_quantile: float
    quantile_delta: float
    incumbent_lower_partial_mean: float
    challenger_lower_partial_mean: float
    lower_partial_delta: float


@dataclass(frozen=True, slots=True)
class DistributionDominanceReport:
    samples: int
    incumbent_mean: float
    challenger_mean: float
    mean_improvement: float
    incumbent_q10: float
    challenger_q10: float
    q10_delta: float
    first_order_dominates: bool
    second_order_noninferior: bool
    worst_lower_partial_delta: float
    points: tuple[DistributionPoint, ...]
    status: DistributionDominanceStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is DistributionDominanceStatus.QUALIFIED


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = probability * (len(sorted_values) - 1)
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return sorted_values[lower_index]
    weight = position - lower_index
    return sorted_values[lower_index] * (1.0 - weight) + sorted_values[upper_index] * weight


def _lower_partial_mean(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        return 0.0
    count = max(1, math.ceil(probability * len(sorted_values)))
    return statistics.fmean(sorted_values[:count])


def evaluate_return_distribution_dominance(
    rows: Sequence[PairedReturn],
    *,
    policy: DistributionDominancePolicy | None = None,
) -> DistributionDominanceReport:
    """Compare a challenger to the incumbent without hiding downside in a mean statistic.

    Quantile dominance is a discrete empirical first-order check. Lower-partial-mean dominance
    is an integrated-quantile style second-order check: at every supplied probability mass, the
    challenger's average outcome in the lower tail must be no worse than the incumbent beyond
    the configured tolerance. This component is research-only and does not choose position size.
    """
    policy = policy or DistributionDominancePolicy()
    if len({row.event_id for row in rows}) != len(rows):
        raise ValueError("event_id values must be unique")
    incumbent = sorted(row.incumbent_return for row in rows)
    challenger = sorted(row.challenger_return for row in rows)
    samples = len(rows)
    incumbent_mean = statistics.fmean(incumbent) if incumbent else 0.0
    challenger_mean = statistics.fmean(challenger) if challenger else 0.0
    points: list[DistributionPoint] = []
    for probability in policy.probability_grid:
        incumbent_quantile = _quantile(incumbent, probability)
        challenger_quantile = _quantile(challenger, probability)
        incumbent_partial = _lower_partial_mean(incumbent, probability)
        challenger_partial = _lower_partial_mean(challenger, probability)
        points.append(
            DistributionPoint(
                probability=probability,
                incumbent_quantile=incumbent_quantile,
                challenger_quantile=challenger_quantile,
                quantile_delta=challenger_quantile - incumbent_quantile,
                incumbent_lower_partial_mean=incumbent_partial,
                challenger_lower_partial_mean=challenger_partial,
                lower_partial_delta=challenger_partial - incumbent_partial,
            )
        )

    q10_incumbent = _quantile(incumbent, 0.10)
    q10_challenger = _quantile(challenger, 0.10)
    q10_delta = q10_challenger - q10_incumbent
    first_order = bool(points) and all(point.quantile_delta >= -1e-12 for point in points)
    worst_partial = min((point.lower_partial_delta for point in points), default=0.0)
    second_order = bool(points) and worst_partial >= -policy.maximum_lower_partial_shortfall
    mean_improvement = challenger_mean - incumbent_mean

    failures: list[str] = []
    if samples < policy.minimum_samples:
        failures.append(f"insufficient_samples:{samples}<{policy.minimum_samples}")
    if mean_improvement <= policy.minimum_mean_improvement:
        failures.append(
            "mean_improvement_not_positive:"
            f"{mean_improvement:.6f}<={policy.minimum_mean_improvement:.6f}"
        )
    if q10_delta < -policy.maximum_q10_degradation:
        failures.append(
            "q10_degradation_above_tolerance:"
            f"{q10_delta:.6f}<-{policy.maximum_q10_degradation:.6f}"
        )
    if not second_order:
        failures.append(
            "lower_partial_distribution_degradation:"
            f"{worst_partial:.6f}<-{policy.maximum_lower_partial_shortfall:.6f}"
        )
    if any(item.startswith("insufficient_") for item in failures):
        status = DistributionDominanceStatus.INSUFFICIENT
    elif failures:
        status = DistributionDominanceStatus.FAILED
    else:
        status = DistributionDominanceStatus.QUALIFIED
    return DistributionDominanceReport(
        samples=samples,
        incumbent_mean=incumbent_mean,
        challenger_mean=challenger_mean,
        mean_improvement=mean_improvement,
        incumbent_q10=q10_incumbent,
        challenger_q10=q10_challenger,
        q10_delta=q10_delta,
        first_order_dominates=first_order,
        second_order_noninferior=second_order,
        worst_lower_partial_delta=worst_partial,
        points=tuple(points),
        status=status,
        failures=tuple(failures),
    )
