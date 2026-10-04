from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .domain import EventKind

_ACTIONABLE = frozenset(
    {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT, EventKind.STOP}
)


class RegressionFirewallStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class TargetedRegressionExample:
    event_id: str
    slice_key: str
    truth: EventKind
    incumbent_probabilities: Mapping[EventKind, float]
    challenger_probabilities: Mapping[EventKind, float]
    incumbent_latency_ms: float = 0.0
    challenger_latency_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.slice_key.strip():
            raise ValueError("event_id and slice_key are required")
        if self.incumbent_latency_ms < 0 or self.challenger_latency_ms < 0:
            raise ValueError("latency cannot be negative")


@dataclass(frozen=True, slots=True)
class TargetedRegressionPolicy:
    minimum_target_samples: int = 50
    minimum_protected_samples: int = 30
    bootstrap_trials: int = 1500
    lower_percentile: float = 0.10
    minimum_target_log_loss_improvement: float = 0.0
    maximum_protected_log_loss_degradation: float = 0.01
    maximum_global_brier_delta: float = 0.0
    maximum_false_action_rate_delta: float = 0.0
    maximum_wrong_action_rate_delta: float = 0.0
    maximum_mean_latency_delta_ms: float = 2.0
    random_seed: int = 160091

    def __post_init__(self) -> None:
        if self.minimum_target_samples <= 0 or self.minimum_protected_samples <= 0:
            raise ValueError("sample thresholds must be positive")
        if self.bootstrap_trials < 100:
            raise ValueError("bootstrap_trials must be >=100")
        if not 0 < self.lower_percentile < 0.5:
            raise ValueError("lower_percentile must be in (0,0.5)")
        for name in (
            "maximum_protected_log_loss_degradation",
            "maximum_global_brier_delta",
            "maximum_false_action_rate_delta",
            "maximum_wrong_action_rate_delta",
            "maximum_mean_latency_delta_ms",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class SliceRegressionEvidence:
    slice_key: str
    target: bool
    samples: int
    mean_log_loss_improvement: float
    bootstrap_lower_improvement: float
    incumbent_brier: float
    challenger_brier: float
    brier_delta: float
    status: RegressionFirewallStatus
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TargetedRegressionFirewallReport:
    samples: int
    target_slices: tuple[str, ...]
    protected_slices: tuple[str, ...]
    slices: tuple[SliceRegressionEvidence, ...]
    incumbent_brier: float
    challenger_brier: float
    global_brier_delta: float
    incumbent_false_action_rate: float
    challenger_false_action_rate: float
    false_action_rate_delta: float
    incumbent_wrong_action_rate: float
    challenger_wrong_action_rate: float
    wrong_action_rate_delta: float
    incumbent_mean_latency_ms: float
    challenger_mean_latency_ms: float
    mean_latency_delta_ms: float
    status: RegressionFirewallStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is RegressionFirewallStatus.QUALIFIED


def _validate_probabilities(
    probabilities: Mapping[EventKind, float], labels: Sequence[EventKind]
) -> None:
    missing = [label for label in labels if label not in probabilities]
    if missing:
        raise ValueError("probability row is missing labels")
    values = [probabilities[label] for label in labels]
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("probabilities must be in [0,1]")
    if abs(sum(values) - 1.0) > 1e-6:
        raise ValueError("probabilities must sum to 1")


def _log_loss(probabilities: Mapping[EventKind, float], truth: EventKind) -> float:
    return -math.log(max(probabilities[truth], 1e-12))


def _brier(
    probabilities: Mapping[EventKind, float],
    truth: EventKind,
    labels: Sequence[EventKind],
) -> float:
    return sum(
        (probabilities[label] - float(label is truth)) ** 2 for label in labels
    )


def _argmax(
    probabilities: Mapping[EventKind, float], labels: Sequence[EventKind]
) -> EventKind:
    return max(labels, key=lambda label: probabilities[label])


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(fraction * (len(ordered) - 1))))
    return ordered[index]


def _bootstrap_lower(
    improvements: Sequence[float],
    *,
    trials: int,
    percentile: float,
    seed: int,
) -> float:
    if not improvements:
        return 0.0
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(trials):
        sampled = [
            improvements[rng.randrange(len(improvements))] for _ in improvements
        ]
        means.append(statistics.fmean(sampled))
    return _percentile(means, percentile)


def _action_rates(
    rows: Sequence[TargetedRegressionExample],
    *,
    challenger: bool,
    labels: Sequence[EventKind],
) -> tuple[float, float]:
    false_actions = 0
    false_opportunities = 0
    wrong_actions = 0
    wrong_opportunities = 0
    for row in rows:
        probabilities = (
            row.challenger_probabilities if challenger else row.incumbent_probabilities
        )
        predicted = _argmax(probabilities, labels)
        if row.truth not in _ACTIONABLE:
            false_opportunities += 1
            false_actions += int(predicted in _ACTIONABLE)
        elif predicted in _ACTIONABLE:
            wrong_opportunities += 1
            wrong_actions += int(predicted is not row.truth)
    false_rate = false_actions / false_opportunities if false_opportunities else 0.0
    wrong_rate = wrong_actions / wrong_opportunities if wrong_opportunities else 0.0
    return false_rate, wrong_rate


def evaluate_targeted_regression_firewall(
    examples: Sequence[TargetedRegressionExample],
    *,
    target_slices: tuple[str, ...],
    labels: tuple[EventKind, ...],
    policy: TargetedRegressionPolicy | None = None,
) -> TargetedRegressionFirewallReport:
    """Prevent targeted self-evolution from fixing one slice by breaking another.

    Target slices must show positive paired held-out log-loss improvement with a bootstrap lower
    bound above the configured floor. Every sufficiently represented non-target slice is treated
    as protected and must remain non-inferior. Global calibration, action errors, and latency are
    independent regression barriers. This component evaluates shadow/research predictions only.
    """
    policy = policy or TargetedRegressionPolicy()
    if not target_slices or len(set(target_slices)) != len(target_slices):
        raise ValueError("target_slices must be unique and non-empty")
    if not labels or len(set(labels)) != len(labels):
        raise ValueError("labels must be unique and non-empty")
    if len({row.event_id for row in examples}) != len(examples):
        raise ValueError("event_id values must be unique")
    rows = tuple(examples)
    for row in rows:
        if row.truth not in labels:
            raise ValueError("truth label is outside the evaluation label set")
        _validate_probabilities(row.incumbent_probabilities, labels)
        _validate_probabilities(row.challenger_probabilities, labels)

    grouped: dict[str, list[TargetedRegressionExample]] = defaultdict(list)
    for row in rows:
        grouped[row.slice_key].append(row)
    target_set = set(target_slices)
    slice_reports: list[SliceRegressionEvidence] = []
    top_failures: list[str] = []
    for index, slice_key in enumerate(sorted(grouped)):
        slice_rows = grouped[slice_key]
        improvements = [
            _log_loss(row.incumbent_probabilities, row.truth)
            - _log_loss(row.challenger_probabilities, row.truth)
            for row in slice_rows
        ]
        mean_improvement = statistics.fmean(improvements)
        lower = _bootstrap_lower(
            improvements,
            trials=policy.bootstrap_trials,
            percentile=policy.lower_percentile,
            seed=policy.random_seed + index * 104729,
        )
        incumbent_brier = statistics.fmean(
            _brier(row.incumbent_probabilities, row.truth, labels) for row in slice_rows
        )
        challenger_brier = statistics.fmean(
            _brier(row.challenger_probabilities, row.truth, labels) for row in slice_rows
        )
        target = slice_key in target_set
        failures: list[str] = []
        minimum_samples = (
            policy.minimum_target_samples if target else policy.minimum_protected_samples
        )
        if len(slice_rows) < minimum_samples:
            failures.append(f"insufficient_samples:{len(slice_rows)}<{minimum_samples}")
        if target and lower <= policy.minimum_target_log_loss_improvement:
            failures.append(
                "target_lower_improvement_not_positive:"
                f"{lower:.6f}<={policy.minimum_target_log_loss_improvement:.6f}"
            )
        if (
            not target
            and len(slice_rows) >= minimum_samples
            and lower < -policy.maximum_protected_log_loss_degradation
        ):
            failures.append(
                "protected_slice_regression:"
                f"{lower:.6f}<-{policy.maximum_protected_log_loss_degradation:.6f}"
            )
        if any(item.startswith("insufficient_") for item in failures):
            status = RegressionFirewallStatus.INSUFFICIENT
        elif failures:
            status = RegressionFirewallStatus.FAILED
        else:
            status = RegressionFirewallStatus.QUALIFIED
        evidence = SliceRegressionEvidence(
            slice_key=slice_key,
            target=target,
            samples=len(slice_rows),
            mean_log_loss_improvement=mean_improvement,
            bootstrap_lower_improvement=lower,
            incumbent_brier=incumbent_brier,
            challenger_brier=challenger_brier,
            brier_delta=challenger_brier - incumbent_brier,
            status=status,
            failures=tuple(failures),
        )
        slice_reports.append(evidence)
        top_failures.extend(f"slice:{slice_key}:{item}" for item in failures)

    missing_targets = sorted(target_set - set(grouped))
    for slice_key in missing_targets:
        top_failures.append(f"target_slice_missing:{slice_key}")

    incumbent_brier = (
        statistics.fmean(_brier(row.incumbent_probabilities, row.truth, labels) for row in rows)
        if rows
        else 0.0
    )
    challenger_brier = (
        statistics.fmean(_brier(row.challenger_probabilities, row.truth, labels) for row in rows)
        if rows
        else 0.0
    )
    brier_delta = challenger_brier - incumbent_brier
    incumbent_false, incumbent_wrong = _action_rates(rows, challenger=False, labels=labels)
    challenger_false, challenger_wrong = _action_rates(rows, challenger=True, labels=labels)
    false_delta = challenger_false - incumbent_false
    wrong_delta = challenger_wrong - incumbent_wrong
    incumbent_latency = (
        statistics.fmean(row.incumbent_latency_ms for row in rows) if rows else 0.0
    )
    challenger_latency = (
        statistics.fmean(row.challenger_latency_ms for row in rows) if rows else 0.0
    )
    latency_delta = challenger_latency - incumbent_latency

    if brier_delta > policy.maximum_global_brier_delta:
        top_failures.append(
            "global_brier_regression:"
            f"{brier_delta:.6f}>{policy.maximum_global_brier_delta:.6f}"
        )
    if false_delta > policy.maximum_false_action_rate_delta:
        top_failures.append(
            "false_action_rate_regression:"
            f"{false_delta:.6f}>{policy.maximum_false_action_rate_delta:.6f}"
        )
    if wrong_delta > policy.maximum_wrong_action_rate_delta:
        top_failures.append(
            "wrong_action_rate_regression:"
            f"{wrong_delta:.6f}>{policy.maximum_wrong_action_rate_delta:.6f}"
        )
    if latency_delta > policy.maximum_mean_latency_delta_ms:
        top_failures.append(
            "mean_latency_regression_ms:"
            f"{latency_delta:.6f}>{policy.maximum_mean_latency_delta_ms:.6f}"
        )

    insufficient = any(
        "insufficient_" in item or item.startswith("target_slice_missing")
        for item in top_failures
    )
    if insufficient:
        status = RegressionFirewallStatus.INSUFFICIENT
    elif top_failures:
        status = RegressionFirewallStatus.FAILED
    else:
        status = RegressionFirewallStatus.QUALIFIED
    return TargetedRegressionFirewallReport(
        samples=len(rows),
        target_slices=tuple(sorted(target_set)),
        protected_slices=tuple(sorted(set(grouped) - target_set)),
        slices=tuple(slice_reports),
        incumbent_brier=incumbent_brier,
        challenger_brier=challenger_brier,
        global_brier_delta=brier_delta,
        incumbent_false_action_rate=incumbent_false,
        challenger_false_action_rate=challenger_false,
        false_action_rate_delta=false_delta,
        incumbent_wrong_action_rate=incumbent_wrong,
        challenger_wrong_action_rate=challenger_wrong,
        wrong_action_rate_delta=wrong_delta,
        incumbent_mean_latency_ms=incumbent_latency,
        challenger_mean_latency_ms=challenger_latency,
        mean_latency_delta_ms=latency_delta,
        status=status,
        failures=tuple(top_failures),
    )
