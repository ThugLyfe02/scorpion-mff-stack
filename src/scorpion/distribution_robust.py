from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .regime_stability import RegimeAxisReport, RegimeStabilityReport


class DistributionRobustStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class DistributionShiftPolicy:
    """Bound how much a future regime mixture may differ from historical shares.

    ``density_ratio_bound`` constrains each future bucket probability ``q_i`` relative to its
    historical normalized share ``p_i`` as ``p_i / R <= q_i <= p_i * R``. This is intentionally
    not a forecast. It asks whether the measured edge survives an adversarial but bounded change
    in the mix of already-observed regimes.
    """

    density_ratio_bound: float = 2.0
    minimum_axes: int = 2
    minimum_groups_per_axis: int = 2
    minimum_worst_case_mean_return: float = 0.0

    def __post_init__(self) -> None:
        if self.density_ratio_bound < 1.0:
            raise ValueError("density_ratio_bound must be >= 1")
        if self.minimum_axes <= 0 or self.minimum_groups_per_axis <= 0:
            raise ValueError("minimum axis/group thresholds must be positive")


@dataclass(frozen=True, slots=True)
class DistributionAxisStress:
    axis: str
    groups: int
    historical_mean_return: float
    worst_case_mean_return: float
    stress_loss: float
    worst_case_weights: tuple[tuple[str, float], ...]
    status: DistributionRobustStatus


@dataclass(frozen=True, slots=True)
class DistributionRobustReport:
    axes: tuple[DistributionAxisStress, ...]
    status: DistributionRobustStatus
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.status is DistributionRobustStatus.PASS


def _qualified_groups(axis: RegimeAxisReport) -> list[tuple[str, float, float]]:
    rows = [
        (group.bucket, group.sample_share, group.mean_return)
        for group in axis.groups
        if group.qualified and group.bucket != "UNKNOWN" and group.sample_share > 0
    ]
    total_share = sum(share for _, share, _ in rows)
    if total_share <= 0:
        return []
    return [(bucket, share / total_share, mean) for bucket, share, mean in rows]


def _worst_case_weights(
    rows: list[tuple[str, float, float]],
    density_ratio_bound: float,
) -> list[tuple[str, float, float]]:
    """Solve a tiny bounded-mixture LP greedily because the objective is linear.

    Start every bucket at its lower density-ratio bound, then allocate the remaining probability
    mass to the lowest-return buckets until each reaches its upper bound. This is the exact
    minimizer for a linear objective under box constraints plus ``sum(q)=1``.
    """

    if not rows:
        return []
    lower = {
        bucket: share / density_ratio_bound for bucket, share, _ in rows
    }
    upper = {
        bucket: min(1.0, share * density_ratio_bound) for bucket, share, _ in rows
    }
    weights = dict(lower)
    remaining = max(0.0, 1.0 - sum(weights.values()))
    for bucket, _, _ in sorted(rows, key=lambda item: item[2]):
        if remaining <= 1e-15:
            break
        capacity = max(0.0, upper[bucket] - weights[bucket])
        add = min(capacity, remaining)
        weights[bucket] += add
        remaining -= add
    if remaining > 1e-9:
        # Numerical/constraint safety: a valid density-ratio box should always admit the
        # historical distribution and therefore sum to one. Treat an infeasible box as a bug.
        raise ValueError("density-ratio constraints are infeasible")
    return [
        (bucket, weights[bucket], mean)
        for bucket, _, mean in rows
    ]


def _stress_axis(
    axis: RegimeAxisReport,
    policy: DistributionShiftPolicy,
) -> DistributionAxisStress:
    rows = _qualified_groups(axis)
    if len(rows) < policy.minimum_groups_per_axis:
        return DistributionAxisStress(
            axis=axis.axis,
            groups=len(rows),
            historical_mean_return=0.0,
            worst_case_mean_return=0.0,
            stress_loss=0.0,
            worst_case_weights=(),
            status=DistributionRobustStatus.INSUFFICIENT,
        )
    historical = sum(share * mean for _, share, mean in rows)
    stressed = _worst_case_weights(rows, policy.density_ratio_bound)
    worst = sum(weight * mean for _, weight, mean in stressed)
    status = (
        DistributionRobustStatus.PASS
        if worst >= policy.minimum_worst_case_mean_return
        else DistributionRobustStatus.FAIL
    )
    return DistributionAxisStress(
        axis=axis.axis,
        groups=len(rows),
        historical_mean_return=historical,
        worst_case_mean_return=worst,
        stress_loss=historical - worst,
        worst_case_weights=tuple((bucket, weight) for bucket, weight, _ in stressed),
        status=status,
    )


def evaluate_distribution_robustness(
    regime: RegimeStabilityReport,
    *,
    policy: DistributionShiftPolicy | None = None,
) -> DistributionRobustReport:
    policy = policy or DistributionShiftPolicy()
    axes = tuple(_stress_axis(axis, policy) for axis in regime.axes)
    usable = [axis for axis in axes if axis.status is not DistributionRobustStatus.INSUFFICIENT]
    failures: list[str] = []
    if len(usable) < policy.minimum_axes:
        failures.append(f"insufficient_stress_axes:{len(usable)}<{policy.minimum_axes}")
    for axis in axes:
        if axis.status is DistributionRobustStatus.FAIL:
            failures.append(
                f"distribution_shift_{axis.axis}:"
                f"{axis.worst_case_mean_return:.6f}<"
                f"{policy.minimum_worst_case_mean_return:.6f}"
            )
    if any(item.startswith("insufficient_") for item in failures):
        status = DistributionRobustStatus.INSUFFICIENT
    elif failures:
        status = DistributionRobustStatus.FAIL
    else:
        status = DistributionRobustStatus.PASS
    return DistributionRobustReport(tuple(axes), status, tuple(failures))
