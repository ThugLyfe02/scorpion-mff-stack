from __future__ import annotations

import random
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class ConditionalSafetyStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class ConditionalPolicyOutcome:
    event_id: str
    market_date: date
    slice_key: str
    incumbent_reward: float
    challenger_reward: float

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id is required")
        if not self.slice_key.strip():
            raise ValueError("slice_key is required")

    @property
    def delta(self) -> float:
        return self.challenger_reward - self.incumbent_reward


@dataclass(frozen=True, slots=True)
class ConditionalPolicySafetyPolicy:
    minimum_events: int = 100
    minimum_days: int = 20
    minimum_slice_events: int = 20
    minimum_slice_days: int = 5
    minimum_slice_coverage: float = 0.80
    lower_tail_fraction: float = 0.10
    minimum_global_mean_delta: float = 0.0
    minimum_tail_mean_delta: float = -0.01
    minimum_slice_mean_delta: float = -0.005
    minimum_slice_positive_day_ratio: float = 0.45
    minimum_slice_lower_bound: float = -0.01
    bootstrap_trials: int = 2000
    block_days: int = 3
    familywise_alpha: float = 0.10
    random_seed: int = 170041

    def __post_init__(self) -> None:
        if min(
            self.minimum_events,
            self.minimum_days,
            self.minimum_slice_events,
            self.minimum_slice_days,
        ) <= 0:
            raise ValueError("sample thresholds must be positive")
        for name in (
            "minimum_slice_coverage",
            "lower_tail_fraction",
            "minimum_slice_positive_day_ratio",
            "familywise_alpha",
        ):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0,1]")
        if self.bootstrap_trials < 100:
            raise ValueError("bootstrap_trials must be >=100")
        if self.block_days <= 0:
            raise ValueError("block_days must be positive")


@dataclass(frozen=True, slots=True)
class ConditionalSliceReport:
    slice_key: str
    events: int
    days: int
    mean_event_delta: float
    mean_daily_delta: float
    positive_day_ratio: float
    simultaneous_lower_bound: float
    qualified: bool
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConditionalPolicySafetyReport:
    events: int
    days: int
    slices: int
    eligible_slices: int
    eligible_event_coverage: float
    global_mean_event_delta: float
    global_mean_daily_delta: float
    lower_tail_mean_daily_delta: float
    slice_reports: tuple[ConditionalSliceReport, ...]
    status: ConditionalSafetyStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is ConditionalSafetyStatus.QUALIFIED


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(fraction * (len(ordered) - 1))))
    return ordered[index]


def _block_bootstrap_mean(
    values: Sequence[float],
    *,
    trials: int,
    block_days: int,
    quantile: float,
    seed: int,
) -> float:
    if not values:
        return 0.0
    rng = random.Random(seed)
    block = min(block_days, len(values))
    estimates: list[float] = []
    for _ in range(trials):
        sample: list[float] = []
        while len(sample) < len(values):
            start = rng.randrange(len(values))
            for offset in range(block):
                sample.append(values[(start + offset) % len(values)])
                if len(sample) >= len(values):
                    break
        estimates.append(statistics.fmean(sample))
    return _percentile(estimates, quantile)


def _daily(rows: Sequence[ConditionalPolicyOutcome]) -> tuple[float, ...]:
    grouped: dict[date, list[float]] = defaultdict(list)
    for row in rows:
        grouped[row.market_date].append(row.delta)
    return tuple(statistics.fmean(grouped[day]) for day in sorted(grouped))


def evaluate_conditional_policy_safety(
    outcomes: Sequence[ConditionalPolicyOutcome],
    *,
    policy: ConditionalPolicySafetyPolicy | None = None,
) -> ConditionalPolicySafetyReport:
    """Reject challengers whose headline improvement hides concentrated conditional harm.

    Slice keys are supplied by the caller from pre-declared research dimensions such as regime,
    liquidity tier, source channel, or time-of-day. This evaluator does not discover slices or
    deploy a policy. It uses a Bonferroni-style familywise bootstrap tail across sufficiently
    powered slices so adding more slices cannot make the evidence gate easier to pass.
    """
    policy = policy or ConditionalPolicySafetyPolicy()
    ordered = tuple(sorted(outcomes, key=lambda item: (item.market_date, item.event_id)))
    by_slice: dict[str, list[ConditionalPolicyOutcome]] = defaultdict(list)
    for row in ordered:
        by_slice[row.slice_key].append(row)

    global_daily = _daily(ordered)
    global_mean_event = statistics.fmean(row.delta for row in ordered) if ordered else 0.0
    global_mean_daily = statistics.fmean(global_daily) if global_daily else 0.0
    tail_count = max(1, int(len(global_daily) * policy.lower_tail_fraction + 0.999999))
    lower_tail = (
        statistics.fmean(sorted(global_daily)[:tail_count]) if global_daily else 0.0
    )

    eligible_keys = tuple(
        key
        for key, rows in sorted(by_slice.items())
        if len(rows) >= policy.minimum_slice_events
        and len({row.market_date for row in rows}) >= policy.minimum_slice_days
    )
    eligible_events = sum(len(by_slice[key]) for key in eligible_keys)
    coverage = eligible_events / len(ordered) if ordered else 0.0
    simultaneous_quantile = min(
        0.499999,
        policy.familywise_alpha / max(1, len(eligible_keys)),
    )

    slice_reports: list[ConditionalSliceReport] = []
    for index, key in enumerate(eligible_keys):
        rows = by_slice[key]
        daily = _daily(rows)
        mean_event = statistics.fmean(row.delta for row in rows)
        mean_daily = statistics.fmean(daily)
        positive_ratio = sum(value > 0 for value in daily) / len(daily)
        lower = _block_bootstrap_mean(
            daily,
            trials=policy.bootstrap_trials,
            block_days=policy.block_days,
            quantile=simultaneous_quantile,
            seed=policy.random_seed + index,
        )
        slice_failures: list[str] = []
        if mean_daily < policy.minimum_slice_mean_delta:
            slice_failures.append(
                "slice_mean_delta_below_tolerance:"
                f"{mean_daily:.6f}<{policy.minimum_slice_mean_delta:.6f}"
            )
        if positive_ratio < policy.minimum_slice_positive_day_ratio:
            slice_failures.append(
                "slice_positive_day_ratio_below_threshold:"
                f"{positive_ratio:.6f}<{policy.minimum_slice_positive_day_ratio:.6f}"
            )
        if lower < policy.minimum_slice_lower_bound:
            slice_failures.append(
                "slice_simultaneous_lower_bound_below_tolerance:"
                f"{lower:.6f}<{policy.minimum_slice_lower_bound:.6f}"
            )
        slice_reports.append(
            ConditionalSliceReport(
                slice_key=key,
                events=len(rows),
                days=len(daily),
                mean_event_delta=mean_event,
                mean_daily_delta=mean_daily,
                positive_day_ratio=positive_ratio,
                simultaneous_lower_bound=lower,
                qualified=not slice_failures,
                failures=tuple(slice_failures),
            )
        )

    failures: list[str] = []
    insufficient = False
    if len(ordered) < policy.minimum_events:
        failures.append(f"insufficient_events:{len(ordered)}<{policy.minimum_events}")
        insufficient = True
    if len(global_daily) < policy.minimum_days:
        failures.append(f"insufficient_days:{len(global_daily)}<{policy.minimum_days}")
        insufficient = True
    if not eligible_keys:
        failures.append("insufficient_powered_slices:0")
        insufficient = True
    if coverage < policy.minimum_slice_coverage:
        failures.append(
            "insufficient_powered_slice_coverage:"
            f"{coverage:.6f}<{policy.minimum_slice_coverage:.6f}"
        )
        insufficient = True
    if global_mean_daily <= policy.minimum_global_mean_delta:
        failures.append(
            "global_mean_delta_not_positive:"
            f"{global_mean_daily:.6f}<={policy.minimum_global_mean_delta:.6f}"
        )
    if lower_tail < policy.minimum_tail_mean_delta:
        failures.append(
            "lower_tail_mean_delta_below_tolerance:"
            f"{lower_tail:.6f}<{policy.minimum_tail_mean_delta:.6f}"
        )
    for report in slice_reports:
        failures.extend(f"slice:{report.slice_key}:{item}" for item in report.failures)

    if insufficient:
        status = ConditionalSafetyStatus.INSUFFICIENT
    elif failures:
        status = ConditionalSafetyStatus.FAILED
    else:
        status = ConditionalSafetyStatus.QUALIFIED
    return ConditionalPolicySafetyReport(
        events=len(ordered),
        days=len(global_daily),
        slices=len(by_slice),
        eligible_slices=len(eligible_keys),
        eligible_event_coverage=coverage,
        global_mean_event_delta=global_mean_event,
        global_mean_daily_delta=global_mean_daily,
        lower_tail_mean_daily_delta=lower_tail,
        slice_reports=tuple(slice_reports),
        status=status,
        failures=tuple(failures),
    )
