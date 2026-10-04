from __future__ import annotations

import random
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class CostAdjustedValueStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class CostAdjustedPolicyOutcome:
    event_id: str
    market_date: date
    fold: int
    incumbent_gross_reward: float
    challenger_gross_reward: float
    incumbent_execution_cost: float
    challenger_execution_cost: float
    incumbent_turnover: float = 0.0
    challenger_turnover: float = 0.0

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id is required")
        if self.fold < 0:
            raise ValueError("fold cannot be negative")
        for name in (
            "incumbent_execution_cost",
            "challenger_execution_cost",
            "incumbent_turnover",
            "challenger_turnover",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")

    @property
    def incumbent_net_reward(self) -> float:
        return self.incumbent_gross_reward - self.incumbent_execution_cost

    @property
    def challenger_net_reward(self) -> float:
        return self.challenger_gross_reward - self.challenger_execution_cost

    @property
    def gross_delta(self) -> float:
        return self.challenger_gross_reward - self.incumbent_gross_reward

    @property
    def cost_delta(self) -> float:
        return self.challenger_execution_cost - self.incumbent_execution_cost

    @property
    def net_delta(self) -> float:
        return self.challenger_net_reward - self.incumbent_net_reward


@dataclass(frozen=True, slots=True)
class CostAdjustedPolicyValuePolicy:
    minimum_events: int = 100
    minimum_days: int = 20
    minimum_folds: int = 4
    minimum_mean_net_delta: float = 0.0
    minimum_bootstrap_lower_net_delta: float = 0.0
    minimum_positive_day_ratio: float = 0.60
    minimum_positive_fold_ratio: float = 0.75
    minimum_worst_fold_net_delta: float = -0.01
    minimum_net_capture_ratio: float = 0.50
    maximum_turnover_multiple: float = 1.50
    bootstrap_trials: int = 2000
    block_days: int = 5
    lower_quantile: float = 0.10
    random_seed: int = 170053

    def __post_init__(self) -> None:
        if min(self.minimum_events, self.minimum_days, self.minimum_folds) <= 0:
            raise ValueError("sample thresholds must be positive")
        for name in (
            "minimum_positive_day_ratio",
            "minimum_positive_fold_ratio",
            "minimum_net_capture_ratio",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        if self.maximum_turnover_multiple <= 0:
            raise ValueError("maximum_turnover_multiple must be positive")
        if self.bootstrap_trials < 100:
            raise ValueError("bootstrap_trials must be >=100")
        if self.block_days <= 0:
            raise ValueError("block_days must be positive")
        if not 0 < self.lower_quantile < 0.5:
            raise ValueError("lower_quantile must be in (0,0.5)")


@dataclass(frozen=True, slots=True)
class CostAdjustedFold:
    fold: int
    events: int
    gross_delta: float
    cost_delta: float
    net_delta: float


@dataclass(frozen=True, slots=True)
class CostAdjustedPolicyValueReport:
    events: int
    days: int
    folds: tuple[CostAdjustedFold, ...]
    mean_gross_delta: float
    mean_cost_delta: float
    mean_net_delta: float
    bootstrap_lower_net_delta: float
    positive_day_ratio: float
    positive_fold_ratio: float
    worst_fold_net_delta: float
    net_capture_ratio: float
    incumbent_mean_turnover: float
    challenger_mean_turnover: float
    turnover_multiple: float
    status: CostAdjustedValueStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is CostAdjustedValueStatus.QUALIFIED


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(fraction * (len(ordered) - 1))))
    return ordered[index]


def _block_bootstrap_lower(
    values: Sequence[float],
    policy: CostAdjustedPolicyValuePolicy,
) -> float:
    if not values:
        return 0.0
    rng = random.Random(policy.random_seed)
    block = min(policy.block_days, len(values))
    estimates: list[float] = []
    for _ in range(policy.bootstrap_trials):
        sample: list[float] = []
        while len(sample) < len(values):
            start = rng.randrange(len(values))
            for offset in range(block):
                sample.append(values[(start + offset) % len(values)])
                if len(sample) >= len(values):
                    break
        estimates.append(statistics.fmean(sample))
    return _percentile(estimates, policy.lower_quantile)


def evaluate_cost_adjusted_policy_value(
    outcomes: Sequence[CostAdjustedPolicyOutcome],
    *,
    policy: CostAdjustedPolicyValuePolicy | None = None,
) -> CostAdjustedPolicyValueReport:
    """Require challenger value to survive execution cost and turnover out of sample.

    Rewards and execution costs must already come from causal held-out or shadow evidence.
    This evaluator does not estimate live size, submit orders, or promote a model. Its purpose
    is to stop gross predictive improvement from being mistaken for economically realizable edge.
    """
    policy = policy or CostAdjustedPolicyValuePolicy()
    ordered = tuple(sorted(outcomes, key=lambda item: (item.market_date, item.fold, item.event_id)))
    by_day: dict[date, list[float]] = defaultdict(list)
    by_fold: dict[int, list[CostAdjustedPolicyOutcome]] = defaultdict(list)
    for row in ordered:
        by_day[row.market_date].append(row.net_delta)
        by_fold[row.fold].append(row)

    daily = tuple(statistics.fmean(by_day[day]) for day in sorted(by_day))
    fold_reports = tuple(
        CostAdjustedFold(
            fold=fold,
            events=len(rows),
            gross_delta=statistics.fmean(row.gross_delta for row in rows),
            cost_delta=statistics.fmean(row.cost_delta for row in rows),
            net_delta=statistics.fmean(row.net_delta for row in rows),
        )
        for fold, rows in sorted(by_fold.items())
    )
    mean_gross = statistics.fmean(row.gross_delta for row in ordered) if ordered else 0.0
    mean_cost = statistics.fmean(row.cost_delta for row in ordered) if ordered else 0.0
    mean_net = statistics.fmean(row.net_delta for row in ordered) if ordered else 0.0
    lower = _block_bootstrap_lower(daily, policy)
    positive_day_ratio = sum(value > 0 for value in daily) / len(daily) if daily else 0.0
    fold_values = [item.net_delta for item in fold_reports]
    positive_fold_ratio = (
        sum(value > 0 for value in fold_values) / len(fold_values) if fold_values else 0.0
    )
    worst_fold = min(fold_values, default=0.0)

    if mean_gross > 0:
        capture = mean_net / mean_gross
    elif mean_net > 0:
        capture = 1.0
    else:
        capture = 0.0

    incumbent_turnover = (
        statistics.fmean(row.incumbent_turnover for row in ordered) if ordered else 0.0
    )
    challenger_turnover = (
        statistics.fmean(row.challenger_turnover for row in ordered) if ordered else 0.0
    )
    if incumbent_turnover <= 1e-12:
        turnover_multiple = 1.0 if challenger_turnover <= 1e-12 else 1e12
    else:
        turnover_multiple = challenger_turnover / incumbent_turnover

    failures: list[str] = []
    insufficient = False
    if len(ordered) < policy.minimum_events:
        failures.append(f"insufficient_events:{len(ordered)}<{policy.minimum_events}")
        insufficient = True
    if len(daily) < policy.minimum_days:
        failures.append(f"insufficient_days:{len(daily)}<{policy.minimum_days}")
        insufficient = True
    if len(fold_reports) < policy.minimum_folds:
        failures.append(f"insufficient_folds:{len(fold_reports)}<{policy.minimum_folds}")
        insufficient = True
    if mean_net <= policy.minimum_mean_net_delta:
        failures.append(
            f"mean_net_delta_not_positive:{mean_net:.6f}<={policy.minimum_mean_net_delta:.6f}"
        )
    if lower <= policy.minimum_bootstrap_lower_net_delta:
        failures.append(
            "bootstrap_lower_net_delta_not_positive:"
            f"{lower:.6f}<={policy.minimum_bootstrap_lower_net_delta:.6f}"
        )
    if positive_day_ratio < policy.minimum_positive_day_ratio:
        failures.append(
            "positive_day_ratio_below_threshold:"
            f"{positive_day_ratio:.6f}<{policy.minimum_positive_day_ratio:.6f}"
        )
    if positive_fold_ratio < policy.minimum_positive_fold_ratio:
        failures.append(
            "positive_fold_ratio_below_threshold:"
            f"{positive_fold_ratio:.6f}<{policy.minimum_positive_fold_ratio:.6f}"
        )
    if worst_fold < policy.minimum_worst_fold_net_delta:
        failures.append(
            "worst_fold_net_delta_below_tolerance:"
            f"{worst_fold:.6f}<{policy.minimum_worst_fold_net_delta:.6f}"
        )
    if capture < policy.minimum_net_capture_ratio:
        failures.append(
            "net_capture_ratio_below_threshold:"
            f"{capture:.6f}<{policy.minimum_net_capture_ratio:.6f}"
        )
    if turnover_multiple > policy.maximum_turnover_multiple:
        failures.append(
            "turnover_multiple_above_threshold:"
            f"{turnover_multiple:.6f}>{policy.maximum_turnover_multiple:.6f}"
        )

    if insufficient:
        status = CostAdjustedValueStatus.INSUFFICIENT
    elif failures:
        status = CostAdjustedValueStatus.FAILED
    else:
        status = CostAdjustedValueStatus.QUALIFIED
    return CostAdjustedPolicyValueReport(
        events=len(ordered),
        days=len(daily),
        folds=fold_reports,
        mean_gross_delta=mean_gross,
        mean_cost_delta=mean_cost,
        mean_net_delta=mean_net,
        bootstrap_lower_net_delta=lower,
        positive_day_ratio=positive_day_ratio,
        positive_fold_ratio=positive_fold_ratio,
        worst_fold_net_delta=worst_fold,
        net_capture_ratio=capture,
        incumbent_mean_turnover=incumbent_turnover,
        challenger_mean_turnover=challenger_turnover,
        turnover_multiple=turnover_multiple,
        status=status,
        failures=tuple(failures),
    )
