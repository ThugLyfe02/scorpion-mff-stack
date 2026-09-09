from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .domain import EventKind

_ACTIONABLE = frozenset(
    {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT, EventKind.STOP}
)


class ConformalStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    INSUFFICIENT = "INSUFFICIENT"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ConformalPolicy:
    alpha: float = 0.01
    minimum_calibration_samples: int = 100
    minimum_evaluation_samples: int = 100
    minimum_empirical_coverage: float = 0.985
    maximum_actionable_singleton_error_rate: float = 0.005

    def __post_init__(self) -> None:
        if not 0 < self.alpha < 1:
            raise ValueError("alpha must be in (0,1)")
        if self.minimum_calibration_samples <= 0 or self.minimum_evaluation_samples <= 0:
            raise ValueError("sample thresholds must be positive")
        if not 0 <= self.minimum_empirical_coverage <= 1:
            raise ValueError("minimum_empirical_coverage must be in [0,1]")
        if not 0 <= self.maximum_actionable_singleton_error_rate <= 1:
            raise ValueError("maximum actionable singleton error rate must be in [0,1]")


@dataclass(frozen=True, slots=True)
class ConformalCalibrator:
    labels: tuple[EventKind, ...]
    alpha: float
    nonconformity_threshold: float
    calibration_samples: int

    def prediction_set(
        self,
        probabilities: Mapping[EventKind, float],
    ) -> tuple[EventKind, ...]:
        _validate_probabilities(probabilities, self.labels)
        selected = tuple(
            label
            for label in self.labels
            if 1.0 - probabilities[label] <= self.nonconformity_threshold
        )
        return selected


@dataclass(frozen=True, slots=True)
class ConformalEvaluation:
    calibration_samples: int
    evaluation_samples: int
    alpha: float
    threshold: float
    empirical_coverage: float
    singleton_rate: float
    actionable_singletons: int
    actionable_singleton_errors: int
    actionable_singleton_error_rate: float
    average_set_size: float
    status: ConformalStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is ConformalStatus.QUALIFIED


def _validate_probabilities(
    probabilities: Mapping[EventKind, float],
    labels: Sequence[EventKind],
) -> None:
    missing = [label for label in labels if label not in probabilities]
    if missing:
        missing_labels = ",".join(item.value for item in missing)
        raise ValueError("probability row is missing labels: " + missing_labels)
    values = [probabilities[label] for label in labels]
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("probabilities must be in [0,1]")
    total = sum(values)
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"probabilities must sum to 1; got {total:.8f}")


def fit_conformal_calibrator(
    probabilities: Sequence[Mapping[EventKind, float]],
    truth: Sequence[EventKind],
    *,
    labels: tuple[EventKind, ...],
    alpha: float = 0.01,
) -> ConformalCalibrator:
    if len(probabilities) != len(truth):
        raise ValueError("probabilities and truth must have equal length")
    if not probabilities:
        raise ValueError("calibration set cannot be empty")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0,1)")
    if len(set(labels)) != len(labels) or not labels:
        raise ValueError("labels must be unique and non-empty")
    scores: list[float] = []
    for row, expected in zip(probabilities, truth, strict=True):
        _validate_probabilities(row, labels)
        if expected not in labels:
            raise ValueError(f"truth label {expected.value} is not in conformal label space")
        scores.append(1.0 - row[expected])
    ordered = sorted(scores)
    rank = math.ceil((len(ordered) + 1) * (1.0 - alpha))
    index = min(len(ordered) - 1, max(0, rank - 1))
    return ConformalCalibrator(labels, alpha, ordered[index], len(ordered))


def evaluate_conformal_calibrator(
    calibrator: ConformalCalibrator,
    probabilities: Sequence[Mapping[EventKind, float]],
    truth: Sequence[EventKind],
    *,
    policy: ConformalPolicy | None = None,
) -> ConformalEvaluation:
    policy = policy or ConformalPolicy(alpha=calibrator.alpha)
    if len(probabilities) != len(truth):
        raise ValueError("probabilities and truth must have equal length")
    covered = 0
    singleton = 0
    actionable_singletons = 0
    actionable_errors = 0
    set_sizes: list[int] = []
    for row, expected in zip(probabilities, truth, strict=True):
        prediction = calibrator.prediction_set(row)
        set_sizes.append(len(prediction))
        if expected in prediction:
            covered += 1
        if len(prediction) == 1:
            singleton += 1
            label = prediction[0]
            if label in _ACTIONABLE:
                actionable_singletons += 1
                if label is not expected:
                    actionable_errors += 1

    samples = len(probabilities)
    coverage = covered / samples if samples else 0.0
    actionable_error_rate = (
        actionable_errors / actionable_singletons if actionable_singletons else 0.0
    )
    failures: list[str] = []
    if calibrator.calibration_samples < policy.minimum_calibration_samples:
        failures.append(
            "insufficient_calibration_samples:"
            f"{calibrator.calibration_samples}<{policy.minimum_calibration_samples}"
        )
    if samples < policy.minimum_evaluation_samples:
        failures.append(
            f"insufficient_evaluation_samples:{samples}<{policy.minimum_evaluation_samples}"
        )
    if samples >= policy.minimum_evaluation_samples and coverage < policy.minimum_empirical_coverage:
        failures.append(
            "empirical_conformal_coverage_below_threshold:"
            f"{coverage:.6f}<{policy.minimum_empirical_coverage:.6f}"
        )
    if actionable_error_rate > policy.maximum_actionable_singleton_error_rate:
        failures.append(
            "actionable_singleton_error_rate_above_threshold:"
            f"{actionable_error_rate:.6f}>"
            f"{policy.maximum_actionable_singleton_error_rate:.6f}"
        )

    if any(item.startswith("insufficient_") for item in failures):
        status = ConformalStatus.INSUFFICIENT
    elif failures:
        status = ConformalStatus.FAILED
    else:
        status = ConformalStatus.QUALIFIED
    return ConformalEvaluation(
        calibration_samples=calibrator.calibration_samples,
        evaluation_samples=samples,
        alpha=calibrator.alpha,
        threshold=calibrator.nonconformity_threshold,
        empirical_coverage=coverage,
        singleton_rate=singleton / samples if samples else 0.0,
        actionable_singletons=actionable_singletons,
        actionable_singleton_errors=actionable_errors,
        actionable_singleton_error_rate=actionable_error_rate,
        average_set_size=sum(set_sizes) / samples if samples else 0.0,
        status=status,
        failures=tuple(failures),
    )
