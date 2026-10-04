from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import StrEnum
from statistics import NormalDist

from .execution_forensics import CompletedTrade

_NORMAL = NormalDist()
_EULER_GAMMA = 0.5772156649015329


class SelectionAdjustedStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class SelectionAdjustedPolicy:
    minimum_samples: int = 30
    minimum_probability: float = 0.95
    minimum_trials: int = 1

    def __post_init__(self) -> None:
        if self.minimum_samples <= 1:
            raise ValueError("minimum_samples must be >1")
        if not 0 < self.minimum_probability < 1:
            raise ValueError("minimum_probability must be in (0,1)")
        if self.minimum_trials <= 0:
            raise ValueError("minimum_trials must be positive")


@dataclass(frozen=True, slots=True)
class SelectionAdjustedPerformance:
    segment: str
    samples: int
    trials_considered: int
    observed_sharpe: float
    skewness: float
    kurtosis: float
    selection_hurdle_sharpe: float
    sharpe_standard_error: float
    probability_above_selection_hurdle: float
    status: SelectionAdjustedStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is SelectionAdjustedStatus.QUALIFIED


def _moments(values: list[float]) -> tuple[float, float, float, float]:
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, 0.0, 0.0, 3.0
    std = statistics.stdev(values)
    if std <= 0:
        return mean, 0.0, 0.0, 3.0
    centered = [value - mean for value in values]
    skew = statistics.fmean(item**3 for item in centered) / (std**3)
    kurtosis = statistics.fmean(item**4 for item in centered) / (std**4)
    return mean, std, skew, max(kurtosis, 1.0)


def _expected_max_null_sharpe(samples: int, trials: int) -> float:
    if trials <= 1:
        return 0.0
    sigma = 1.0 / math.sqrt(max(samples - 1, 1))
    first = _NORMAL.inv_cdf(1.0 - 1.0 / trials)
    second = _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    return sigma * ((1.0 - _EULER_GAMMA) * first + _EULER_GAMMA * second)


def evaluate_selection_adjusted_performance(
    segment: str,
    trades: tuple[CompletedTrade, ...],
    *,
    trials_considered: int,
    policy: SelectionAdjustedPolicy | None = None,
) -> SelectionAdjustedPerformance:
    policy = policy or SelectionAdjustedPolicy()
    values = [float(item.return_fraction) for item in trades]
    samples = len(values)
    trials = max(trials_considered, policy.minimum_trials)
    if samples < policy.minimum_samples:
        return SelectionAdjustedPerformance(
            segment=segment,
            samples=samples,
            trials_considered=trials,
            observed_sharpe=0.0,
            skewness=0.0,
            kurtosis=3.0,
            selection_hurdle_sharpe=0.0,
            sharpe_standard_error=0.0,
            probability_above_selection_hurdle=0.0,
            status=SelectionAdjustedStatus.INSUFFICIENT,
            failures=(f"insufficient_samples:{samples}<{policy.minimum_samples}",),
        )

    mean, std, skew, kurtosis = _moments(values)
    if std <= 0:
        return SelectionAdjustedPerformance(
            segment=segment,
            samples=samples,
            trials_considered=trials,
            observed_sharpe=0.0,
            skewness=skew,
            kurtosis=kurtosis,
            selection_hurdle_sharpe=0.0,
            sharpe_standard_error=0.0,
            probability_above_selection_hurdle=0.0,
            status=SelectionAdjustedStatus.FAILED,
            failures=("zero_return_variance",),
        )

    sharpe = mean / std
    hurdle = _expected_max_null_sharpe(samples, trials)
    variance_term = 1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe**2
    standard_error = math.sqrt(max(variance_term, 1e-12) / (samples - 1))
    z_score = (sharpe - hurdle) / standard_error
    probability = _NORMAL.cdf(z_score)
    failures: list[str] = []
    if sharpe <= hurdle:
        failures.append(
            f"observed_sharpe_not_above_selection_hurdle:{sharpe:.6f}<={hurdle:.6f}"
        )
    if probability < policy.minimum_probability:
        failures.append(
            "selection_adjusted_probability_below_requirement:"
            f"{probability:.6f}<{policy.minimum_probability:.6f}"
        )
    status = SelectionAdjustedStatus.QUALIFIED if not failures else SelectionAdjustedStatus.FAILED
    return SelectionAdjustedPerformance(
        segment=segment,
        samples=samples,
        trials_considered=trials,
        observed_sharpe=sharpe,
        skewness=skew,
        kurtosis=kurtosis,
        selection_hurdle_sharpe=hurdle,
        sharpe_standard_error=standard_error,
        probability_above_selection_hurdle=probability,
        status=status,
        failures=tuple(failures),
    )


def evaluate_selected_segments(
    segments: dict[str, tuple[CompletedTrade, ...]],
    selected: set[str],
    *,
    policy: SelectionAdjustedPolicy | None = None,
) -> tuple[SelectionAdjustedPerformance, ...]:
    policy = policy or SelectionAdjustedPolicy()
    trials = max(1, len(segments))
    return tuple(
        evaluate_selection_adjusted_performance(
            name,
            segments[name],
            trials_considered=trials,
            policy=policy,
        )
        for name in sorted(selected)
        if name in segments
    )
