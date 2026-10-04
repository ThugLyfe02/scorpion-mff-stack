from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum


class ConsensusStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class Annotation:
    event_id: str
    reviewer_id: str
    label: str

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.reviewer_id.strip() or not self.label.strip():
            raise ValueError("event_id, reviewer_id, and label are required")


@dataclass(frozen=True, slots=True)
class LabelConsensusPolicy:
    minimum_annotations_per_event: int = 2
    minimum_consensus_probability: float = 0.80
    maximum_entropy: float = 0.60
    laplace: float = 1.0
    iterations: int = 20

    def __post_init__(self) -> None:
        if self.minimum_annotations_per_event <= 0:
            raise ValueError("minimum_annotations_per_event must be positive")
        if not 0 < self.minimum_consensus_probability <= 1:
            raise ValueError("minimum_consensus_probability must be in (0,1]")
        if self.maximum_entropy < 0:
            raise ValueError("maximum_entropy cannot be negative")
        if self.laplace <= 0 or self.iterations <= 0:
            raise ValueError("laplace and iterations must be positive")


@dataclass(frozen=True, slots=True)
class ReviewerReliability:
    reviewer_id: str
    annotations: int
    expected_accuracy: float


@dataclass(frozen=True, slots=True)
class EventConsensus:
    event_id: str
    annotations: int
    label: str | None
    probability: float
    entropy: float
    distribution: tuple[tuple[str, float], ...]
    status: ConsensusStatus

    @property
    def accepted(self) -> bool:
        return self.status is ConsensusStatus.ACCEPTED


@dataclass(frozen=True, slots=True)
class LabelConsensusReport:
    events: int
    reviewers: int
    labels: tuple[str, ...]
    accepted_events: int
    review_required_events: int
    insufficient_events: int
    reviewer_reliability: tuple[ReviewerReliability, ...]
    event_consensus: tuple[EventConsensus, ...]


def _normalize(values: dict[str, float]) -> dict[str, float]:
    total = sum(values.values())
    if total <= 0:
        uniform = 1.0 / len(values)
        return {key: uniform for key in values}
    return {key: value / total for key, value in values.items()}


def _entropy(distribution: dict[str, float]) -> float:
    return -sum(value * math.log(max(value, 1e-12)) for value in distribution.values())


def evaluate_label_consensus(
    annotations: tuple[Annotation, ...],
    *,
    policy: LabelConsensusPolicy | None = None,
) -> LabelConsensusReport:
    """Estimate reviewer reliability and event truth with a compact Dawid-Skene-style EM loop."""
    policy = policy or LabelConsensusPolicy()
    if not annotations:
        return LabelConsensusReport(0, 0, (), 0, 0, 0, (), ())

    labels = tuple(sorted({item.label for item in annotations}))
    events: dict[str, list[Annotation]] = defaultdict(list)
    reviewers: dict[str, list[Annotation]] = defaultdict(list)
    for item in annotations:
        events[item.event_id].append(item)
        reviewers[item.reviewer_id].append(item)

    posteriors: dict[str, dict[str, float]] = {}
    for event_id, rows in events.items():
        counts = Counter(item.label for item in rows)
        posteriors[event_id] = _normalize(
            {label: float(counts[label]) + policy.laplace for label in labels}
        )

    confusion: dict[str, dict[str, dict[str, float]]] = {}
    for _ in range(policy.iterations):
        confusion = {}
        for reviewer_id, rows in reviewers.items():
            matrix = {
                truth: {observed: policy.laplace for observed in labels}
                for truth in labels
            }
            for row in rows:
                posterior = posteriors[row.event_id]
                for truth, probability in posterior.items():
                    matrix[truth][row.label] += probability
            for truth in labels:
                matrix[truth] = _normalize(matrix[truth])
            confusion[reviewer_id] = matrix

        updated: dict[str, dict[str, float]] = {}
        prior_counts = {
            label: policy.laplace
            + sum(posterior[label] for posterior in posteriors.values())
            for label in labels
        }
        prior = _normalize(prior_counts)
        for event_id, rows in events.items():
            scores: dict[str, float] = {}
            for truth in labels:
                log_score = math.log(max(prior[truth], 1e-12))
                for row in rows:
                    probability = confusion[row.reviewer_id][truth][row.label]
                    log_score += math.log(max(probability, 1e-12))
                scores[truth] = log_score
            maximum = max(scores.values())
            updated[event_id] = _normalize(
                {label: math.exp(value - maximum) for label, value in scores.items()}
            )
        posteriors = updated

    reliability: list[ReviewerReliability] = []
    for reviewer_id, rows in reviewers.items():
        matrix = confusion[reviewer_id]
        expected = sum(matrix[label][label] for label in labels) / len(labels)
        reliability.append(ReviewerReliability(reviewer_id, len(rows), expected))

    consensus_rows: list[EventConsensus] = []
    accepted = review = insufficient = 0
    for event_id in sorted(events):
        rows = events[event_id]
        distribution = posteriors[event_id]
        best_label, best_probability = max(distribution.items(), key=lambda item: item[1])
        entropy = _entropy(distribution)
        if len(rows) < policy.minimum_annotations_per_event:
            status = ConsensusStatus.INSUFFICIENT
            insufficient += 1
            selected: str | None = None
        elif (
            best_probability >= policy.minimum_consensus_probability
            and entropy <= policy.maximum_entropy
        ):
            status = ConsensusStatus.ACCEPTED
            accepted += 1
            selected = best_label
        else:
            status = ConsensusStatus.REVIEW_REQUIRED
            review += 1
            selected = None
        consensus_rows.append(
            EventConsensus(
                event_id=event_id,
                annotations=len(rows),
                label=selected,
                probability=best_probability,
                entropy=entropy,
                distribution=tuple(sorted(distribution.items())),
                status=status,
            )
        )

    return LabelConsensusReport(
        events=len(events),
        reviewers=len(reviewers),
        labels=labels,
        accepted_events=accepted,
        review_required_events=review,
        insufficient_events=insufficient,
        reviewer_reliability=tuple(sorted(reliability, key=lambda item: item.reviewer_id)),
        event_consensus=tuple(consensus_rows),
    )
