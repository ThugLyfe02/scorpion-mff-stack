from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .domain import EventKind


class UncertaintyStatus(StrEnum):
    INFORMATIVE = "INFORMATIVE"
    NEEDS_MORE_DATA = "NEEDS_MORE_DATA"
    INHERENTLY_AMBIGUOUS = "INHERENTLY_AMBIGUOUS"
    CONFLICTED = "CONFLICTED"


@dataclass(frozen=True, slots=True)
class UncertaintyPolicy:
    high_epistemic_threshold: float = 0.20
    high_aleatoric_threshold: float = 0.45
    high_disagreement_threshold: float = 0.25

    def __post_init__(self) -> None:
        for name in (
            "high_epistemic_threshold",
            "high_aleatoric_threshold",
            "high_disagreement_threshold",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")


@dataclass(frozen=True, slots=True)
class UncertaintyDecomposition:
    predictive_entropy: float
    expected_model_entropy: float
    mutual_information: float
    normalized_epistemic: float
    normalized_aleatoric: float
    variation_ratio: float
    top_label: EventKind
    top_probability: float
    status: UncertaintyStatus


@dataclass(frozen=True, slots=True)
class UncertaintyHealthPolicy:
    minimum_samples: int = 100
    maximum_conflicted_rate: float = 0.05
    maximum_needs_more_data_rate: float = 0.15
    maximum_inherently_ambiguous_rate: float = 0.25

    def __post_init__(self) -> None:
        if self.minimum_samples <= 0:
            raise ValueError("minimum_samples must be positive")
        for name in (
            "maximum_conflicted_rate",
            "maximum_needs_more_data_rate",
            "maximum_inherently_ambiguous_rate",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")


@dataclass(frozen=True, slots=True)
class UncertaintyHealthReport:
    samples: int
    informative_rate: float
    conflicted_rate: float
    needs_more_data_rate: float
    inherently_ambiguous_rate: float
    mean_epistemic: float
    mean_aleatoric: float
    qualified: bool
    failures: tuple[str, ...]


def _entropy(values: Sequence[float]) -> float:
    return -sum(value * math.log(max(value, 1e-12)) for value in values)


def decompose_ensemble_uncertainty(
    model_probabilities: Mapping[str, Mapping[EventKind, float]],
    *,
    labels: Sequence[EventKind],
    policy: UncertaintyPolicy | None = None,
) -> UncertaintyDecomposition:
    """Separate disagreement-driven epistemic uncertainty from per-model ambiguity."""
    policy = policy or UncertaintyPolicy()
    if not model_probabilities:
        raise ValueError("model_probabilities cannot be empty")
    if not labels:
        raise ValueError("labels cannot be empty")

    label_tuple = tuple(labels)
    model_rows: list[list[float]] = []
    votes: list[EventKind] = []
    for model_id in sorted(model_probabilities):
        row = model_probabilities[model_id]
        missing = [label for label in label_tuple if label not in row]
        if missing:
            raise ValueError(f"model {model_id} is missing labels")
        values = [row[label] for label in label_tuple]
        if any(value < 0 or value > 1 for value in values):
            raise ValueError(f"model {model_id} has invalid probabilities")
        if abs(sum(values) - 1.0) > 1e-6:
            raise ValueError(f"model {model_id} probabilities must sum to 1")
        model_rows.append(values)
        votes.append(label_tuple[max(range(len(values)), key=values.__getitem__)])

    mean = [
        sum(row[index] for row in model_rows) / len(model_rows)
        for index in range(len(label_tuple))
    ]
    predictive_entropy = _entropy(mean)
    expected_entropy = sum(_entropy(row) for row in model_rows) / len(model_rows)
    mutual_information = max(0.0, predictive_entropy - expected_entropy)
    maximum_entropy = math.log(len(label_tuple)) if len(label_tuple) > 1 else 1.0
    epistemic = min(1.0, mutual_information / maximum_entropy)
    aleatoric = min(1.0, expected_entropy / maximum_entropy)
    top_index = max(range(len(mean)), key=mean.__getitem__)
    top_label = label_tuple[top_index]
    top_probability = mean[top_index]
    majority = max(votes.count(label) for label in set(votes))
    variation_ratio = 1.0 - majority / len(votes)

    if (
        epistemic >= policy.high_epistemic_threshold
        and variation_ratio >= policy.high_disagreement_threshold
    ):
        status = UncertaintyStatus.CONFLICTED
    elif epistemic >= policy.high_epistemic_threshold:
        status = UncertaintyStatus.NEEDS_MORE_DATA
    elif aleatoric >= policy.high_aleatoric_threshold:
        status = UncertaintyStatus.INHERENTLY_AMBIGUOUS
    else:
        status = UncertaintyStatus.INFORMATIVE

    return UncertaintyDecomposition(
        predictive_entropy=predictive_entropy,
        expected_model_entropy=expected_entropy,
        mutual_information=mutual_information,
        normalized_epistemic=epistemic,
        normalized_aleatoric=aleatoric,
        variation_ratio=variation_ratio,
        top_label=top_label,
        top_probability=top_probability,
        status=status,
    )


def evaluate_uncertainty_health(
    rows: Sequence[UncertaintyDecomposition],
    *,
    policy: UncertaintyHealthPolicy | None = None,
) -> UncertaintyHealthReport:
    policy = policy or UncertaintyHealthPolicy()
    samples = len(rows)
    if samples == 0:
        return UncertaintyHealthReport(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, False, ("no_samples",))

    def rate(status: UncertaintyStatus) -> float:
        return sum(row.status is status for row in rows) / samples

    informative = rate(UncertaintyStatus.INFORMATIVE)
    conflicted = rate(UncertaintyStatus.CONFLICTED)
    needs_more = rate(UncertaintyStatus.NEEDS_MORE_DATA)
    ambiguous = rate(UncertaintyStatus.INHERENTLY_AMBIGUOUS)
    mean_epistemic = sum(row.normalized_epistemic for row in rows) / samples
    mean_aleatoric = sum(row.normalized_aleatoric for row in rows) / samples
    failures: list[str] = []
    if samples < policy.minimum_samples:
        failures.append(f"insufficient_samples:{samples}<{policy.minimum_samples}")
    if conflicted > policy.maximum_conflicted_rate:
        failures.append(
            f"conflicted_rate:{conflicted:.6f}>{policy.maximum_conflicted_rate:.6f}"
        )
    if needs_more > policy.maximum_needs_more_data_rate:
        failures.append(
            "needs_more_data_rate:"
            f"{needs_more:.6f}>{policy.maximum_needs_more_data_rate:.6f}"
        )
    if ambiguous > policy.maximum_inherently_ambiguous_rate:
        failures.append(
            "inherently_ambiguous_rate:"
            f"{ambiguous:.6f}>{policy.maximum_inherently_ambiguous_rate:.6f}"
        )
    return UncertaintyHealthReport(
        samples=samples,
        informative_rate=informative,
        conflicted_rate=conflicted,
        needs_more_data_rate=needs_more,
        inherently_ambiguous_rate=ambiguous,
        mean_epistemic=mean_epistemic,
        mean_aleatoric=mean_aleatoric,
        qualified=not failures,
        failures=tuple(failures),
    )
