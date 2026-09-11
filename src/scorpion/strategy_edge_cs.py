from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class StrategyEdgeStatus(StrEnum):
    OBSERVING = "OBSERVING"
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    UNCERTAIN = "UNCERTAIN"
    UNTRUSTED_CLIPPING = "UNTRUSTED_CLIPPING"


@dataclass(frozen=True, slots=True)
class DailyEdgeObservation:
    market_date: date
    return_fraction: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.return_fraction):
            raise ValueError("return_fraction must be finite")


@dataclass(frozen=True, slots=True)
class StrategyEdgePolicy:
    alpha: float = 0.01
    lower_return_bound: float = -1.0
    upper_return_bound: float = 1.0
    minimum_observations: int = 30
    maximum_clipped_rate: float = 0.05

    def __post_init__(self) -> None:
        if not 0 < self.alpha < 1:
            raise ValueError("alpha must be in (0,1)")
        if self.lower_return_bound >= self.upper_return_bound:
            raise ValueError("lower_return_bound must be below upper_return_bound")
        if self.minimum_observations <= 0:
            raise ValueError("minimum_observations must be positive")
        if not 0 <= self.maximum_clipped_rate <= 1:
            raise ValueError("maximum_clipped_rate must be in [0,1]")


@dataclass(frozen=True, slots=True)
class EdgeConfidencePoint:
    observations: int
    mean_clipped_return: float
    lower_bound: float
    upper_bound: float
    clipped_observations: int
    clipped_rate: float


@dataclass(frozen=True, slots=True)
class StrategyEdgeReport:
    observations: int
    clipped_observations: int
    clipped_rate: float
    mean_clipped_return: float
    lower_bound: float
    upper_bound: float
    status: StrategyEdgeStatus
    trajectory: tuple[EdgeConfidencePoint, ...]
    failures: tuple[str, ...]

    @property
    def trusted_positive(self) -> bool:
        return self.status is StrategyEdgeStatus.POSITIVE


def _confidence_radius(
    observations: int,
    policy: StrategyEdgePolicy,
) -> float:
    # alpha_t spends the total error budget over all t because sum 1/(t(t+1)) = 1.
    alpha_t = policy.alpha / (observations * (observations + 1))
    width = policy.upper_return_bound - policy.lower_return_bound
    return width * math.sqrt(math.log(2.0 / alpha_t) / (2.0 * observations))


def evaluate_strategy_edge(
    observations: tuple[DailyEdgeObservation, ...],
    *,
    policy: StrategyEdgePolicy | None = None,
) -> StrategyEdgeReport:
    """Build a time-uniform Hoeffding confidence sequence over bounded daily returns.

    The caller must precommit the return bounds before monitoring. Values outside the bounds are
    clipped for statistical evidence and counted explicitly; excessive clipping makes the report
    untrusted. This is research/shadow evidence only and is not a live sizing or execution rule.
    """
    policy = policy or StrategyEdgePolicy()
    if len({item.market_date for item in observations}) != len(observations):
        raise ValueError("only one edge observation per market day is allowed")
    ordered = tuple(sorted(observations, key=lambda item: item.market_date))
    clipped_values: list[float] = []
    clipped_count = 0
    trajectory: list[EdgeConfidencePoint] = []
    for index, observation in enumerate(ordered, start=1):
        clipped = min(
            policy.upper_return_bound,
            max(policy.lower_return_bound, observation.return_fraction),
        )
        clipped_count += int(clipped != observation.return_fraction)
        clipped_values.append(clipped)
        mean_value = sum(clipped_values) / index
        radius = _confidence_radius(index, policy)
        lower = max(policy.lower_return_bound, mean_value - radius)
        upper = min(policy.upper_return_bound, mean_value + radius)
        trajectory.append(
            EdgeConfidencePoint(
                observations=index,
                mean_clipped_return=mean_value,
                lower_bound=lower,
                upper_bound=upper,
                clipped_observations=clipped_count,
                clipped_rate=clipped_count / index,
            )
        )
    if not trajectory:
        return StrategyEdgeReport(
            observations=0,
            clipped_observations=0,
            clipped_rate=0.0,
            mean_clipped_return=0.0,
            lower_bound=policy.lower_return_bound,
            upper_bound=policy.upper_return_bound,
            status=StrategyEdgeStatus.OBSERVING,
            trajectory=(),
            failures=("no_observations",),
        )
    final = trajectory[-1]
    failures: list[str] = []
    if final.clipped_rate > policy.maximum_clipped_rate:
        failures.append(
            f"clipped_return_rate_above_threshold:{final.clipped_rate:.6f}>"
            f"{policy.maximum_clipped_rate:.6f}"
        )
        status = StrategyEdgeStatus.UNTRUSTED_CLIPPING
    elif final.observations < policy.minimum_observations:
        failures.append(
            f"insufficient_edge_observations:{final.observations}<"
            f"{policy.minimum_observations}"
        )
        status = StrategyEdgeStatus.OBSERVING
    elif final.lower_bound > 0:
        status = StrategyEdgeStatus.POSITIVE
    elif final.upper_bound < 0:
        status = StrategyEdgeStatus.NEGATIVE
    else:
        status = StrategyEdgeStatus.UNCERTAIN
    return StrategyEdgeReport(
        observations=final.observations,
        clipped_observations=final.clipped_observations,
        clipped_rate=final.clipped_rate,
        mean_clipped_return=final.mean_clipped_return,
        lower_bound=final.lower_bound,
        upper_bound=final.upper_bound,
        status=status,
        trajectory=tuple(trajectory),
        failures=tuple(failures),
    )
