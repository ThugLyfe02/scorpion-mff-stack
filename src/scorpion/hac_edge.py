from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import StrEnum

from .execution_forensics import CompletedTrade


class HACEdgeStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class HACEdgePolicy:
    minimum_samples: int = 30
    max_lag: int = 5
    lower_bound_z: float = 1.6448536269514722
    minimum_lower_bound: float = 0.0
    minimum_effective_samples: float = 15.0

    def __post_init__(self) -> None:
        if self.minimum_samples <= 1:
            raise ValueError("minimum_samples must be >1")
        if self.max_lag < 0:
            raise ValueError("max_lag cannot be negative")
        if self.lower_bound_z <= 0:
            raise ValueError("lower_bound_z must be positive")
        if self.minimum_effective_samples <= 0:
            raise ValueError("minimum_effective_samples must be positive")


@dataclass(frozen=True, slots=True)
class HACEdgeReport:
    segment: str
    samples: int
    lags_used: int
    mean_return: float
    naive_standard_error: float
    hac_standard_error: float
    hac_lower_bound: float
    lag1_autocorrelation: float
    effective_samples: float
    status: HACEdgeStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is HACEdgeStatus.QUALIFIED


def _ordered_values(trades: tuple[CompletedTrade, ...]) -> list[float]:
    ordered = sorted(trades, key=lambda item: (item.opened_ts_utc, item.entry_event_id))
    return [float(item.return_fraction) for item in ordered]


def _autocovariance(values: list[float], mean: float, lag: int) -> float:
    if lag >= len(values):
        return 0.0
    return sum(
        (values[index] - mean) * (values[index - lag] - mean)
        for index in range(lag, len(values))
    ) / len(values)


def evaluate_hac_edge(
    segment: str,
    trades: tuple[CompletedTrade, ...],
    *,
    policy: HACEdgePolicy | None = None,
) -> HACEdgeReport:
    """Discount edge significance for serially correlated chronological returns.

    The lower bound uses a normal approximation with Newey-West/Bartlett long-run variance.
    It is a research robustness diagnostic rather than a guarantee of future performance.
    """
    policy = policy or HACEdgePolicy()
    values = _ordered_values(trades)
    samples = len(values)
    if samples < policy.minimum_samples:
        return HACEdgeReport(
            segment=segment,
            samples=samples,
            lags_used=0,
            mean_return=0.0,
            naive_standard_error=0.0,
            hac_standard_error=0.0,
            hac_lower_bound=0.0,
            lag1_autocorrelation=0.0,
            effective_samples=0.0,
            status=HACEdgeStatus.INSUFFICIENT,
            failures=(f"insufficient_samples:{samples}<{policy.minimum_samples}",),
        )

    mean = statistics.fmean(values)
    variance = statistics.variance(values)
    naive_se = math.sqrt(max(variance, 0.0) / samples)
    gamma0 = _autocovariance(values, mean, 0)
    lags = min(policy.max_lag, samples - 1)
    long_run = gamma0
    gamma1 = 0.0
    for lag in range(1, lags + 1):
        gamma = _autocovariance(values, mean, lag)
        if lag == 1:
            gamma1 = gamma
        weight = 1.0 - lag / (lags + 1.0)
        long_run += 2.0 * weight * gamma
    long_run = max(long_run, 0.0)
    hac_se = math.sqrt(long_run / samples) if samples else 0.0
    lower = mean - policy.lower_bound_z * hac_se
    lag1 = gamma1 / gamma0 if gamma0 > 0 else 0.0
    effective = (
        min(float(samples), samples * gamma0 / long_run)
        if long_run > 0 and gamma0 > 0
        else float(samples)
    )

    failures: list[str] = []
    if lower <= policy.minimum_lower_bound:
        failures.append(
            "hac_lower_bound_not_positive:"
            f"{lower:.6f}<={policy.minimum_lower_bound:.6f}"
        )
    if effective < policy.minimum_effective_samples:
        failures.append(
            "effective_samples_below_threshold:"
            f"{effective:.3f}<{policy.minimum_effective_samples:.3f}"
        )
    status = HACEdgeStatus.QUALIFIED if not failures else HACEdgeStatus.FAILED
    return HACEdgeReport(
        segment=segment,
        samples=samples,
        lags_used=lags,
        mean_return=mean,
        naive_standard_error=naive_se,
        hac_standard_error=hac_se,
        hac_lower_bound=lower,
        lag1_autocorrelation=lag1,
        effective_samples=effective,
        status=status,
        failures=tuple(failures),
    )


def evaluate_selected_hac(
    segments: dict[str, tuple[CompletedTrade, ...]],
    selected: set[str],
    *,
    policy: HACEdgePolicy | None = None,
) -> tuple[HACEdgeReport, ...]:
    return tuple(
        evaluate_hac_edge(name, segments[name], policy=policy)
        for name in sorted(selected)
        if name in segments
    )
