from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import StrEnum

from .execution_forensics import CompletedTrade


class HierarchicalStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    NON_POSITIVE = "NON_POSITIVE"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class HierarchicalPolicy:
    minimum_groups: int = 2
    minimum_total_samples: int = 30
    minimum_group_samples: int = 5
    lower_bound_z: float = 1.6448536269514722
    variance_floor: float = 1e-8

    def __post_init__(self) -> None:
        if self.minimum_groups < 2:
            raise ValueError("minimum_groups must be >=2")
        if self.minimum_total_samples <= 0 or self.minimum_group_samples <= 0:
            raise ValueError("sample thresholds must be positive")
        if self.lower_bound_z <= 0 or self.variance_floor <= 0:
            raise ValueError("lower_bound_z and variance_floor must be positive")


@dataclass(frozen=True, slots=True)
class PartialPoolingEstimate:
    group: str
    samples: int
    sample_mean: float
    sample_std: float
    family_mean: float
    between_group_variance: float
    posterior_weight_on_sample: float
    posterior_mean: float
    posterior_std: float
    posterior_lower_bound: float
    status: HierarchicalStatus

    @property
    def qualified(self) -> bool:
        return self.status is HierarchicalStatus.QUALIFIED


@dataclass(frozen=True, slots=True)
class HierarchicalReport:
    groups: int
    total_samples: int
    family_mean: float
    between_group_variance: float
    estimates: tuple[PartialPoolingEstimate, ...]
    status: HierarchicalStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is HierarchicalStatus.QUALIFIED


def _values(trades: tuple[CompletedTrade, ...]) -> list[float]:
    return [float(item.return_fraction) for item in trades]


def evaluate_hierarchical_shrinkage(
    groups: dict[str, tuple[CompletedTrade, ...]],
    *,
    policy: HierarchicalPolicy | None = None,
) -> HierarchicalReport:
    policy = policy or HierarchicalPolicy()
    usable = {
        name: items
        for name, items in groups.items()
        if len(items) >= policy.minimum_group_samples
    }
    total_samples = sum(len(items) for items in usable.values())
    if len(usable) < policy.minimum_groups or total_samples < policy.minimum_total_samples:
        return HierarchicalReport(
            groups=len(usable),
            total_samples=total_samples,
            family_mean=0.0,
            between_group_variance=0.0,
            estimates=(),
            status=HierarchicalStatus.INSUFFICIENT,
            failures=(
                "insufficient_hierarchical_family:"
                f"groups={len(usable)},samples={total_samples}",
            ),
        )

    group_stats: dict[str, tuple[int, float, float]] = {}
    weighted_sum = 0.0
    pooled_within_numerator = 0.0
    pooled_within_denominator = 0
    for name, items in usable.items():
        values = _values(items)
        mean = statistics.fmean(values)
        variance = statistics.variance(values) if len(values) > 1 else 0.0
        group_stats[name] = (len(values), mean, variance)
        weighted_sum += len(values) * mean
        if len(values) > 1:
            pooled_within_numerator += (len(values) - 1) * variance
            pooled_within_denominator += len(values) - 1

    family_mean = weighted_sum / total_samples
    pooled_within = (
        pooled_within_numerator / pooled_within_denominator
        if pooled_within_denominator
        else policy.variance_floor
    )
    pooled_within = max(pooled_within, policy.variance_floor)
    weighted_between = sum(
        samples * (mean - family_mean) ** 2
        for samples, mean, _ in group_stats.values()
    ) / total_samples
    average_sampling_variance = sum(
        variance / samples
        for samples, _, variance in group_stats.values()
    ) / len(group_stats)
    between_variance = max(
        weighted_between - average_sampling_variance,
        policy.variance_floor,
    )

    estimates: list[PartialPoolingEstimate] = []
    for name in sorted(group_stats):
        samples, mean, variance = group_stats[name]
        within = max(variance, pooled_within, policy.variance_floor)
        sampling_variance = within / samples
        weight = between_variance / (between_variance + sampling_variance)
        posterior_mean = weight * mean + (1.0 - weight) * family_mean
        posterior_variance = 1.0 / (
            (1.0 / between_variance) + (1.0 / sampling_variance)
        )
        posterior_std = math.sqrt(max(posterior_variance, 0.0))
        lower = posterior_mean - policy.lower_bound_z * posterior_std
        status = (
            HierarchicalStatus.QUALIFIED
            if lower > 0
            else HierarchicalStatus.NON_POSITIVE
        )
        estimates.append(
            PartialPoolingEstimate(
                group=name,
                samples=samples,
                sample_mean=mean,
                sample_std=math.sqrt(max(variance, 0.0)),
                family_mean=family_mean,
                between_group_variance=between_variance,
                posterior_weight_on_sample=weight,
                posterior_mean=posterior_mean,
                posterior_std=posterior_std,
                posterior_lower_bound=lower,
                status=status,
            )
        )

    failures = tuple(
        f"hierarchical_lower_bound_nonpositive:{item.group}"
        for item in estimates
        if not item.qualified
    )
    status = HierarchicalStatus.QUALIFIED if not failures else HierarchicalStatus.NON_POSITIVE
    return HierarchicalReport(
        groups=len(usable),
        total_samples=total_samples,
        family_mean=family_mean,
        between_group_variance=between_variance,
        estimates=tuple(estimates),
        status=status,
        failures=failures,
    )


def ticker_groups(
    trades: tuple[CompletedTrade, ...],
) -> dict[str, tuple[CompletedTrade, ...]]:
    grouped: dict[str, list[CompletedTrade]] = {}
    for trade in trades:
        ticker = trade.contract_key.split("|", 1)[0].upper()
        grouped.setdefault(ticker, []).append(trade)
    return {name: tuple(items) for name, items in grouped.items()}


def channel_groups(
    trades: tuple[CompletedTrade, ...],
) -> dict[str, tuple[CompletedTrade, ...]]:
    grouped: dict[str, list[CompletedTrade]] = {}
    for trade in trades:
        grouped.setdefault(trade.channel_id, []).append(trade)
    return {name: tuple(items) for name, items in grouped.items()}
