from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .domain import EventKind

_DEFAULT_REQUIRED = (
    EventKind.ENTRY,
    EventKind.ADD,
    EventKind.TRIM,
    EventKind.EXIT,
)
_ACTIONABLE = frozenset(
    {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT, EventKind.STOP}
)


class MondrianStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    INSUFFICIENT = "INSUFFICIENT"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class MondrianPolicy:
    alpha: float = 0.01
    minimum_calibration_samples_per_required_class: int = 30
    minimum_evaluation_samples_per_required_class: int = 30
    minimum_required_class_coverage: float = 0.98
    maximum_actionable_singleton_error_rate: float = 0.005
    required_classes: tuple[EventKind, ...] = _DEFAULT_REQUIRED

    def __post_init__(self) -> None:
        if not 0 < self.alpha < 1:
            raise ValueError("alpha must be in (0,1)")
        if self.minimum_calibration_samples_per_required_class <= 0:
            raise ValueError("minimum calibration samples must be positive")
        if self.minimum_evaluation_samples_per_required_class <= 0:
            raise ValueError("minimum evaluation samples must be positive")
        if not 0 <= self.minimum_required_class_coverage <= 1:
            raise ValueError("minimum required class coverage must be in [0,1]")
        if not 0 <= self.maximum_actionable_singleton_error_rate <= 1:
            raise ValueError("maximum actionable singleton error rate must be in [0,1]")
        if not self.required_classes or len(set(self.required_classes)) != len(self.required_classes):
            raise ValueError("required_classes must be unique and non-empty")


@dataclass(frozen=True, slots=True)
class MondrianCalibrator:
    labels: tuple[EventKind, ...]
    alpha: float
    thresholds: tuple[tuple[EventKind, float], ...]
    calibration_counts: tuple[tuple[EventKind, int], ...]

    def _threshold_map(self) -> dict[EventKind, float]:
        return dict(self.thresholds)

    def prediction_set(
        self,
        probabilities: Mapping[EventKind, float],
    ) -> tuple[EventKind, ...]:
        _validate_probabilities(probabilities, self.labels)
        threshold_map = self._threshold_map()
        return tuple(
            label
            for label in self.labels
            if label in threshold_map and 1.0 - probabilities[label] <= threshold_map[label]
        )


@dataclass(frozen=True, slots=True)
class MondrianClassCoverage:
    label: EventKind
    calibration_samples: int
    evaluation_samples: int
    empirical_coverage: float
    qualified: bool


@dataclass(frozen=True, slots=True)
class MondrianEvaluation:
    alpha: float
    class_coverage: tuple[MondrianClassCoverage, ...]
    overall_coverage: float
    average_set_size: float
    actionable_singletons: int
    actionable_singleton_errors: int
    actionable_singleton_error_rate: float
    status: MondrianStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is MondrianStatus.QUALIFIED


def _validate_probabilities(
    probabilities: Mapping[EventKind, float],
    labels: Sequence[EventKind],
) -> None:
    missing = [label for label in labels if label not in probabilities]
    if missing:
        raise ValueError(
            "probability row is missing labels: " + ",".join(label.value for label in missing)
        )
    values = [probabilities[label] for label in labels]
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("probabilities must be in [0,1]")
    total = sum(values)
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"probabilities must sum to 1; got {total:.8f}")


def _finite_sample_threshold(scores: list[float], alpha: float) -> float:
    ordered = sorted(scores)
    rank = math.ceil((len(ordered) + 1) * (1.0 - alpha))
    index = min(len(ordered) - 1, max(0, rank - 1))
    return ordered[index]


def fit_mondrian_calibrator(
    probabilities: Sequence[Mapping[EventKind, float]],
    truth: Sequence[EventKind],
    *,
    labels: tuple[EventKind, ...],
    alpha: float = 0.01,
) -> MondrianCalibrator:
    if len(probabilities) != len(truth):
        raise ValueError("probabilities and truth must have equal length")
    if not probabilities:
        raise ValueError("calibration set cannot be empty")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0,1)")
    if not labels or len(set(labels)) != len(labels):
        raise ValueError("labels must be unique and non-empty")

    scores_by_label: dict[EventKind, list[float]] = {label: [] for label in labels}
    for row, expected in zip(probabilities, truth, strict=True):
        _validate_probabilities(row, labels)
        if expected not in scores_by_label:
            raise ValueError(f"truth label {expected.value} is not in label space")
        scores_by_label[expected].append(1.0 - row[expected])

    thresholds = tuple(
        (label, _finite_sample_threshold(scores, alpha))
        for label in labels
        if (scores := scores_by_label[label])
    )
    counts = tuple((label, len(scores_by_label[label])) for label in labels)
    return MondrianCalibrator(labels, alpha, thresholds, counts)


def evaluate_mondrian_calibrator(
    calibrator: MondrianCalibrator,
    probabilities: Sequence[Mapping[EventKind, float]],
    truth: Sequence[EventKind],
    *,
    policy: MondrianPolicy | None = None,
) -> MondrianEvaluation:
    policy = policy or MondrianPolicy(alpha=calibrator.alpha)
    if len(probabilities) != len(truth):
        raise ValueError("probabilities and truth must have equal length")

    calibration_counts = dict(calibrator.calibration_counts)
    eval_counts: dict[EventKind, int] = {label: 0 for label in calibrator.labels}
    covered_counts: dict[EventKind, int] = {label: 0 for label in calibrator.labels}
    covered_total = 0
    set_sizes: list[int] = []
    actionable_singletons = 0
    actionable_errors = 0

    for row, expected in zip(probabilities, truth, strict=True):
        prediction = calibrator.prediction_set(row)
        set_sizes.append(len(prediction))
        eval_counts[expected] = eval_counts.get(expected, 0) + 1
        if expected in prediction:
            covered_counts[expected] = covered_counts.get(expected, 0) + 1
            covered_total += 1
        if len(prediction) == 1 and prediction[0] in _ACTIONABLE:
            actionable_singletons += 1
            if prediction[0] is not expected:
                actionable_errors += 1

    failures: list[str] = []
    class_reports: list[MondrianClassCoverage] = []
    for label in calibrator.labels:
        calibration_samples = calibration_counts.get(label, 0)
        evaluation_samples = eval_counts.get(label, 0)
        coverage = (
            covered_counts.get(label, 0) / evaluation_samples if evaluation_samples else 0.0
        )
        required = label in policy.required_classes
        enough_calibration = (
            calibration_samples >= policy.minimum_calibration_samples_per_required_class
        )
        enough_evaluation = (
            evaluation_samples >= policy.minimum_evaluation_samples_per_required_class
        )
        coverage_ok = coverage >= policy.minimum_required_class_coverage
        qualified = not required or (enough_calibration and enough_evaluation and coverage_ok)
        class_reports.append(
            MondrianClassCoverage(
                label=label,
                calibration_samples=calibration_samples,
                evaluation_samples=evaluation_samples,
                empirical_coverage=coverage,
                qualified=qualified,
            )
        )
        if required and not enough_calibration:
            failures.append(
                f"class_{label.value}_insufficient_calibration:"
                f"{calibration_samples}<"
                f"{policy.minimum_calibration_samples_per_required_class}"
            )
        if required and not enough_evaluation:
            failures.append(
                f"class_{label.value}_insufficient_evaluation:"
                f"{evaluation_samples}<"
                f"{policy.minimum_evaluation_samples_per_required_class}"
            )
        if required and enough_evaluation and not coverage_ok:
            failures.append(
                f"class_{label.value}_coverage_below_threshold:"
                f"{coverage:.6f}<{policy.minimum_required_class_coverage:.6f}"
            )

    actionable_error_rate = (
        actionable_errors / actionable_singletons if actionable_singletons else 0.0
    )
    if actionable_error_rate > policy.maximum_actionable_singleton_error_rate:
        failures.append(
            "actionable_singleton_error_rate_above_threshold:"
            f"{actionable_error_rate:.6f}>"
            f"{policy.maximum_actionable_singleton_error_rate:.6f}"
        )

    insufficient = any("insufficient_" in failure for failure in failures)
    if insufficient:
        status = MondrianStatus.INSUFFICIENT
    elif failures:
        status = MondrianStatus.FAILED
    else:
        status = MondrianStatus.QUALIFIED
    samples = len(probabilities)
    return MondrianEvaluation(
        alpha=calibrator.alpha,
        class_coverage=tuple(class_reports),
        overall_coverage=covered_total / samples if samples else 0.0,
        average_set_size=sum(set_sizes) / samples if samples else 0.0,
        actionable_singletons=actionable_singletons,
        actionable_singleton_errors=actionable_errors,
        actionable_singleton_error_rate=actionable_error_rate,
        status=status,
        failures=tuple(failures),
    )
