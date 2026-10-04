from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .domain import EventKind


@dataclass(frozen=True, slots=True)
class OOFLabelPrediction:
    event_id: str
    observed_label: EventKind
    probabilities: Mapping[EventKind, float]


@dataclass(frozen=True, slots=True)
class LabelNoisePolicy:
    minimum_class_samples: int = 20
    low_self_confidence_quantile: float = 0.10
    minimum_alternative_margin: float = 0.20
    maximum_flagged_rate: float = 0.10

    def __post_init__(self) -> None:
        if self.minimum_class_samples <= 0:
            raise ValueError("minimum_class_samples must be positive")
        if not 0 < self.low_self_confidence_quantile < 0.5:
            raise ValueError("low_self_confidence_quantile must be in (0,0.5)")
        if not 0 <= self.minimum_alternative_margin <= 1:
            raise ValueError("minimum_alternative_margin must be in [0,1]")
        if not 0 <= self.maximum_flagged_rate <= 1:
            raise ValueError("maximum_flagged_rate must be in [0,1]")


@dataclass(frozen=True, slots=True)
class LabelIssue:
    event_id: str
    observed_label: EventKind
    suggested_label: EventKind
    self_confidence: float
    alternative_confidence: float
    margin: float
    class_threshold: float


@dataclass(frozen=True, slots=True)
class LabelNoiseReport:
    samples: int
    eligible_samples: int
    flagged: int
    flagged_rate: float
    class_thresholds: tuple[tuple[EventKind, float], ...]
    issues: tuple[LabelIssue, ...]
    qualified: bool
    failures: tuple[str, ...]


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(q * (len(ordered) - 1))))
    return ordered[index]


def audit_oof_label_noise(
    rows: Sequence[OOFLabelPrediction],
    *,
    labels: Sequence[EventKind],
    policy: LabelNoisePolicy | None = None,
) -> LabelNoiseReport:
    """Flag suspicious labels using only out-of-fold prediction confidence.

    The audit never mutates labels. It produces a prioritized re-review set and a dataset-health
    gate so a model cannot improve its apparent accuracy by silently training through noisy truth.
    """
    policy = policy or LabelNoisePolicy()
    label_tuple = tuple(labels)
    if not label_tuple:
        raise ValueError("labels cannot be empty")

    by_class: dict[EventKind, list[float]] = defaultdict(list)
    validated: list[OOFLabelPrediction] = []
    for row in rows:
        missing = [label for label in label_tuple if label not in row.probabilities]
        if missing:
            raise ValueError(f"event {row.event_id} is missing probabilities")
        values = [row.probabilities[label] for label in label_tuple]
        if any(value < 0 or value > 1 for value in values):
            raise ValueError(f"event {row.event_id} has invalid probabilities")
        if abs(sum(values) - 1.0) > 1e-6:
            raise ValueError(f"event {row.event_id} probabilities must sum to 1")
        if row.observed_label not in label_tuple:
            raise ValueError(f"event {row.event_id} observed label is outside label universe")
        confidence = row.probabilities[row.observed_label]
        by_class[row.observed_label].append(confidence)
        validated.append(row)

    thresholds = {
        label: _quantile(values, policy.low_self_confidence_quantile)
        for label, values in by_class.items()
        if len(values) >= policy.minimum_class_samples
    }
    issues: list[LabelIssue] = []
    eligible = 0
    for row in validated:
        threshold = thresholds.get(row.observed_label)
        if threshold is None:
            continue
        eligible += 1
        self_confidence = row.probabilities[row.observed_label]
        alternative = max(
            (label for label in label_tuple if label is not row.observed_label),
            key=lambda label: row.probabilities[label],
        )
        alternative_confidence = row.probabilities[alternative]
        margin = alternative_confidence - self_confidence
        if self_confidence <= threshold and margin >= policy.minimum_alternative_margin:
            issues.append(
                LabelIssue(
                    event_id=row.event_id,
                    observed_label=row.observed_label,
                    suggested_label=alternative,
                    self_confidence=self_confidence,
                    alternative_confidence=alternative_confidence,
                    margin=margin,
                    class_threshold=threshold,
                )
            )

    issues.sort(key=lambda item: (-item.margin, item.self_confidence, item.event_id))
    flagged_rate = len(issues) / eligible if eligible else 0.0
    failures: list[str] = []
    if eligible == 0:
        failures.append("no_classes_have_enough_oof_support")
    if flagged_rate > policy.maximum_flagged_rate:
        failures.append(
            f"label_noise_rate:{flagged_rate:.6f}>{policy.maximum_flagged_rate:.6f}"
        )
    return LabelNoiseReport(
        samples=len(validated),
        eligible_samples=eligible,
        flagged=len(issues),
        flagged_rate=flagged_rate,
        class_thresholds=tuple(sorted(thresholds.items(), key=lambda item: item[0].value)),
        issues=tuple(issues),
        qualified=not failures,
        failures=tuple(failures),
    )
