from __future__ import annotations

import collections
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .domain import EventKind

ACTIONABLE_KINDS = frozenset({
    EventKind.ENTRY,
    EventKind.ADD,
    EventKind.TRIM,
    EventKind.EXIT,
})


class ConfidenceBand(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class DecisionEvidence:
    rule_id: str
    confidence: float
    matched_terms: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    normalized_text: str = ""
    latency_us: int = 0

    @property
    def band(self) -> ConfidenceBand:
        if self.confidence >= 0.95:
            return ConfidenceBand.HIGH
        if self.confidence >= 0.75:
            return ConfidenceBand.MEDIUM
        return ConfidenceBand.LOW


@dataclass(frozen=True, slots=True)
class AssociationEvidence:
    method: str
    confidence: float
    candidate_count: int


@dataclass(frozen=True, slots=True)
class LabeledDecision:
    expected: EventKind
    predicted: EventKind


@dataclass(frozen=True, slots=True)
class ClassMetrics:
    precision: float
    recall: float
    f1: float
    support: int


@dataclass(frozen=True, slots=True)
class AccuracyReport:
    total: int
    correct: int
    accuracy: float
    actionable_precision: float
    ambiguity_rate: float
    per_kind: dict[str, ClassMetrics]


@dataclass(frozen=True, slots=True)
class DriftReport:
    total_variation: float
    baseline: dict[str, float]
    current: dict[str, float]
    drifted: bool


def _safe_div(numerator: int | float, denominator: int | float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def score_decisions(samples: Sequence[LabeledDecision]) -> AccuracyReport:
    total = len(samples)
    correct = sum(sample.expected is sample.predicted for sample in samples)
    predicted_actionable = sum(sample.predicted in ACTIONABLE_KINDS for sample in samples)
    correct_actionable = sum(
        sample.expected is sample.predicted and sample.predicted in ACTIONABLE_KINDS
        for sample in samples
    )
    ambiguous = sum(sample.predicted is EventKind.AMBIGUOUS for sample in samples)

    kinds = sorted(EventKind, key=lambda kind: kind.value)
    per_kind: dict[str, ClassMetrics] = {}
    for kind in kinds:
        tp = sum(sample.expected is kind and sample.predicted is kind for sample in samples)
        fp = sum(sample.expected is not kind and sample.predicted is kind for sample in samples)
        fn = sum(sample.expected is kind and sample.predicted is not kind for sample in samples)
        support = sum(sample.expected is kind for sample in samples)
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        f1 = _safe_div(2 * precision * recall, precision + recall)
        per_kind[kind.value] = ClassMetrics(precision, recall, f1, support)

    return AccuracyReport(
        total=total,
        correct=correct,
        accuracy=_safe_div(correct, total),
        actionable_precision=_safe_div(correct_actionable, predicted_actionable),
        ambiguity_rate=_safe_div(ambiguous, total),
        per_kind=per_kind,
    )


def percentile(values: Sequence[int | float], percentile_value: float) -> float:
    if not values:
        return 0.0
    if not 0 <= percentile_value <= 1:
        raise ValueError("percentile must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(percentile_value * len(ordered)))
    return ordered[rank - 1]


def distribution(kinds: Iterable[EventKind]) -> dict[str, float]:
    counts = collections.Counter(kind.value for kind in kinds)
    total = sum(counts.values())
    if total == 0:
        return {}
    return {key: value / total for key, value in sorted(counts.items())}


def compare_distributions(
    baseline_kinds: Iterable[EventKind],
    current_kinds: Iterable[EventKind],
    *,
    threshold: float = 0.20,
) -> DriftReport:
    baseline = distribution(baseline_kinds)
    current = distribution(current_kinds)
    keys = set(baseline) | set(current)
    tv = 0.5 * sum(abs(baseline.get(key, 0.0) - current.get(key, 0.0)) for key in keys)
    return DriftReport(
        total_variation=tv,
        baseline=baseline,
        current=current,
        drifted=tv >= threshold,
    )
