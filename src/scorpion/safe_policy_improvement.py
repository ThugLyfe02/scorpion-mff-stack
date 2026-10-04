from __future__ import annotations

import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class PolicyImprovementStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class PairedPolicyOutcome:
    event_id: str
    market_date: date
    incumbent_reward: float
    challenger_reward: float
    incumbent_actionable: bool
    challenger_actionable: bool

    @property
    def delta(self) -> float:
        return self.challenger_reward - self.incumbent_reward

    @property
    def action_escalation(self) -> bool:
        return self.challenger_actionable and not self.incumbent_actionable


@dataclass(frozen=True, slots=True)
class SafePolicyImprovementPolicy:
    minimum_events: int = 100
    minimum_days: int = 20
    bootstrap_trials: int = 4000
    block_days: int = 5
    lower_quantile: float = 0.05
    minimum_mean_improvement: float = 0.0
    minimum_positive_day_ratio: float = 0.55
    maximum_action_escalations: int = 0
    random_seed: int = 55217

    def __post_init__(self) -> None:
        if self.minimum_events <= 0 or self.minimum_days <= 0:
            raise ValueError("minimum sample thresholds must be positive")
        if self.bootstrap_trials < 100:
            raise ValueError("bootstrap_trials must be >=100")
        if self.block_days <= 0:
            raise ValueError("block_days must be positive")
        if not 0 < self.lower_quantile < 0.5:
            raise ValueError("lower_quantile must be in (0,0.5)")
        if not 0 <= self.minimum_positive_day_ratio <= 1:
            raise ValueError("minimum_positive_day_ratio must be in [0,1]")
        if self.maximum_action_escalations < 0:
            raise ValueError("maximum_action_escalations cannot be negative")


@dataclass(frozen=True, slots=True)
class SafePolicyImprovementReport:
    events: int
    days: int
    mean_event_delta: float
    mean_daily_delta: float
    median_daily_delta: float
    lower_confidence_bound: float
    positive_day_ratio: float
    worst_day_delta: float
    action_escalations: int
    status: PolicyImprovementStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is PolicyImprovementStatus.QUALIFIED


def _block_bootstrap(values: list[float], policy: SafePolicyImprovementPolicy) -> list[float]:
    rng = random.Random(policy.random_seed)
    if not values:
        return []
    block = min(policy.block_days, len(values))
    results: list[float] = []
    for _ in range(policy.bootstrap_trials):
        sample: list[float] = []
        while len(sample) < len(values):
            start = rng.randrange(len(values))
            for offset in range(block):
                sample.append(values[(start + offset) % len(values)])
                if len(sample) >= len(values):
                    break
        results.append(statistics.fmean(sample))
    results.sort()
    return results


def evaluate_safe_policy_improvement(
    outcomes: tuple[PairedPolicyOutcome, ...],
    *,
    policy: SafePolicyImprovementPolicy | None = None,
) -> SafePolicyImprovementReport:
    """Require paired, lower-bound improvement before a challenger reaches operator review."""
    policy = policy or SafePolicyImprovementPolicy()
    grouped: dict[date, list[float]] = defaultdict(list)
    escalations = 0
    for item in outcomes:
        grouped[item.market_date].append(item.delta)
        escalations += int(item.action_escalation)

    daily = [statistics.fmean(grouped[day]) for day in sorted(grouped)]
    event_delta = [item.delta for item in outcomes]
    mean_event = statistics.fmean(event_delta) if event_delta else 0.0
    mean_daily = statistics.fmean(daily) if daily else 0.0
    median_daily = statistics.median(daily) if daily else 0.0
    positive_day_ratio = sum(value > 0 for value in daily) / len(daily) if daily else 0.0
    worst = min(daily, default=0.0)
    bootstrap = _block_bootstrap(daily, policy)
    if bootstrap:
        index = min(
            len(bootstrap) - 1,
            max(0, int(policy.lower_quantile * (len(bootstrap) - 1))),
        )
        lower = bootstrap[index]
    else:
        lower = 0.0

    failures: list[str] = []
    if len(outcomes) < policy.minimum_events:
        failures.append(f"insufficient_events:{len(outcomes)}<{policy.minimum_events}")
    if len(daily) < policy.minimum_days:
        failures.append(f"insufficient_days:{len(daily)}<{policy.minimum_days}")
    if lower <= policy.minimum_mean_improvement:
        failures.append(
            f"paired_improvement_lower_bound:{lower:.6f}<={policy.minimum_mean_improvement:.6f}"
        )
    if positive_day_ratio < policy.minimum_positive_day_ratio:
        failures.append(
            "positive_day_ratio_below_requirement:"
            f"{positive_day_ratio:.6f}<{policy.minimum_positive_day_ratio:.6f}"
        )
    if escalations > policy.maximum_action_escalations:
        failures.append(
            f"action_escalations:{escalations}>{policy.maximum_action_escalations}"
        )

    if any(item.startswith("insufficient_") for item in failures):
        status = PolicyImprovementStatus.INSUFFICIENT
    elif failures:
        status = PolicyImprovementStatus.FAILED
    else:
        status = PolicyImprovementStatus.QUALIFIED
    return SafePolicyImprovementReport(
        events=len(outcomes),
        days=len(daily),
        mean_event_delta=mean_event,
        mean_daily_delta=mean_daily,
        median_daily_delta=median_daily,
        lower_confidence_bound=lower,
        positive_day_ratio=positive_day_ratio,
        worst_day_delta=worst,
        action_escalations=escalations,
        status=status,
        failures=tuple(failures),
    )
