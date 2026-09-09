from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .domain import EventKind


class ShiftWeightedConformalStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class ShiftWeightedConformalPolicy:
    alpha: float = 0.01
    minimum_effective_sample_size: float = 100.0
    maximum_normalized_weight_share: float = 0.05
    maximum_raw_weight: float = 20.0
    minimum_evaluation_samples: int = 100
    minimum_empirical_coverage: float = 0.985

    def __post_init__(self) -> None:
        if not 0 < self.alpha < 1:
            raise ValueError("alpha must be in (0,1)")
        if self.minimum_effective_sample_size <= 0:
            raise ValueError("minimum_effective_sample_size must be positive")
        if not 0 < self.maximum_normalized_weight_share <= 1:
            raise ValueError("maximum_normalized_weight_share must be in (0,1]")
        if self.maximum_raw_weight <= 0:
            raise ValueError("maximum_raw_weight must be positive")
        if self.minimum_evaluation_samples <= 0:
            raise ValueError("minimum_evaluation_samples must be positive")
        if not 0 <= self.minimum_empirical_coverage <= 1:
            raise ValueError("minimum_empirical_coverage must be in [0,1]")


@dataclass(frozen=True, slots=True)
class ShiftWeightedCalibrationRow:
    probabilities: Mapping[EventKind, float]
    truth: EventKind
    importance_weight: float


@dataclass(frozen=True, slots=True)
class ShiftWeightedConformalCalibrator:
    labels: tuple[EventKind, ...]
    alpha: float
    threshold: float
    raw_samples: int
    effective_sample_size: float
    max_normalized_weight_share: float
    clipped_weight_count: int
    status: ShiftWeightedConformalStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is ShiftWeightedConformalStatus.QUALIFIED

    def prediction_set(self, probabilities: Mapping[EventKind, float]) -> tuple[EventKind, ...]:
        _validate_probabilities(probabilities, self.labels)
        return tuple(
            label
            for label in self.labels
            if 1.0 - probabilities[label] <= self.threshold
        )


@dataclass(frozen=True, slots=True)
class ShiftWeightedConformalEvaluation:
    calibration: ShiftWeightedConformalCalibrator
    evaluation_samples: int
    empirical_coverage: float
    average_set_size: float
    singleton_rate: float
    status: ShiftWeightedConformalStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is ShiftWeightedConformalStatus.QUALIFIED


def _validate_probabilities(
    probabilities: Mapping[EventKind, float],
    labels: Sequence[EventKind],
) -> None:
    values: list[float] = []
    for label in labels:
        if label not in probabilities:
            raise ValueError(f"missing probability for {label.value}")
        value = probabilities[label]
        if not 0 <= value <= 1:
            raise ValueError("probabilities must be in [0,1]")
        values.append(value)
    if abs(sum(values) - 1.0) > 1e-6:
        raise ValueError("probabilities must sum to 1")


def _weighted_quantile(scores: list[tuple[float, float]], q: float) -> float:
    if not scores:
        raise ValueError("weighted quantile requires observations")
    ordered = sorted(scores, key=lambda item: item[0])
    total = sum(weight for _, weight in ordered)
    target = q * total
    cumulative = 0.0
    for score, weight in ordered:
        cumulative += weight
        if cumulative >= target:
            return score
    return ordered[-1][0]


def fit_shift_weighted_conformal(
    rows: Sequence[ShiftWeightedCalibrationRow],
    *,
    labels: tuple[EventKind, ...],
    policy: ShiftWeightedConformalPolicy | None = None,
) -> ShiftWeightedConformalCalibrator:
    """Fit a diagnostic weighted conformal threshold under externally estimated shift weights.

    This does not manufacture a distribution-shift guarantee. It makes the weight degeneracy
    visible and fails closed when the weighted calibration sample no longer has enough effective
    support to be meaningful.
    """
    policy = policy or ShiftWeightedConformalPolicy()
    if not labels or len(set(labels)) != len(labels):
        raise ValueError("labels must be unique and non-empty")
    if not rows:
        return ShiftWeightedConformalCalibrator(
            labels,
            policy.alpha,
            1.0,
            0,
            0.0,
            0.0,
            0,
            ShiftWeightedConformalStatus.INSUFFICIENT,
            ("no_calibration_rows",),
        )

    weighted_scores: list[tuple[float, float]] = []
    clipped = 0
    weights: list[float] = []
    for row in rows:
        _validate_probabilities(row.probabilities, labels)
        if row.truth not in labels:
            raise ValueError("truth is outside label universe")
        if not math.isfinite(row.importance_weight) or row.importance_weight <= 0:
            raise ValueError("importance weights must be finite and positive")
        weight = min(row.importance_weight, policy.maximum_raw_weight)
        clipped += int(weight != row.importance_weight)
        weights.append(weight)
        weighted_scores.append((1.0 - row.probabilities[row.truth], weight))

    total = sum(weights)
    squared = sum(weight * weight for weight in weights)
    effective = total * total / squared if squared > 0 else 0.0
    max_share = max(weights) / total if total else 0.0
    threshold = _weighted_quantile(weighted_scores, 1.0 - policy.alpha)
    failures: list[str] = []
    if effective < policy.minimum_effective_sample_size:
        failures.append(
            "effective_sample_size_below_requirement:"
            f"{effective:.3f}<{policy.minimum_effective_sample_size:.3f}"
        )
    if max_share > policy.maximum_normalized_weight_share:
        failures.append(
            "weight_concentration_above_requirement:"
            f"{max_share:.6f}>{policy.maximum_normalized_weight_share:.6f}"
        )
    status = (
        ShiftWeightedConformalStatus.QUALIFIED
        if not failures
        else ShiftWeightedConformalStatus.INSUFFICIENT
    )
    return ShiftWeightedConformalCalibrator(
        labels=labels,
        alpha=policy.alpha,
        threshold=threshold,
        raw_samples=len(rows),
        effective_sample_size=effective,
        max_normalized_weight_share=max_share,
        clipped_weight_count=clipped,
        status=status,
        failures=tuple(failures),
    )


def evaluate_shift_weighted_conformal(
    calibrator: ShiftWeightedConformalCalibrator,
    probabilities: Sequence[Mapping[EventKind, float]],
    truth: Sequence[EventKind],
    *,
    policy: ShiftWeightedConformalPolicy | None = None,
) -> ShiftWeightedConformalEvaluation:
    policy = policy or ShiftWeightedConformalPolicy(alpha=calibrator.alpha)
    if len(probabilities) != len(truth):
        raise ValueError("probabilities and truth must have equal length")
    covered = 0
    singletons = 0
    sizes: list[int] = []
    for row, expected in zip(probabilities, truth, strict=True):
        if expected not in calibrator.labels:
            raise ValueError("truth is outside label universe")
        prediction = calibrator.prediction_set(row)
        sizes.append(len(prediction))
        covered += int(expected in prediction)
        singletons += int(len(prediction) == 1)
    samples = len(probabilities)
    coverage = covered / samples if samples else 0.0
    failures = list(calibrator.failures)
    if samples < policy.minimum_evaluation_samples:
        failures.append(
            f"insufficient_evaluation_samples:{samples}<{policy.minimum_evaluation_samples}"
        )
    if samples >= policy.minimum_evaluation_samples and coverage < policy.minimum_empirical_coverage:
        failures.append(
            "shifted_empirical_coverage_below_requirement:"
            f"{coverage:.6f}<{policy.minimum_empirical_coverage:.6f}"
        )
    if any(item.startswith(("effective_", "weight_", "insufficient_")) for item in failures):
        status = ShiftWeightedConformalStatus.INSUFFICIENT
    elif failures:
        status = ShiftWeightedConformalStatus.FAILED
    else:
        status = ShiftWeightedConformalStatus.QUALIFIED
    return ShiftWeightedConformalEvaluation(
        calibration=calibrator,
        evaluation_samples=samples,
        empirical_coverage=coverage,
        average_set_size=sum(sizes) / samples if samples else 0.0,
        singleton_rate=singletons / samples if samples else 0.0,
        status=status,
        failures=tuple(failures),
    )
