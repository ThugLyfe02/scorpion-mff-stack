from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .domain import EventKind


class EnsembleDiversityStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class OOFModelDecision:
    event_id: str
    model_id: str
    truth: EventKind
    predicted: EventKind

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.model_id.strip():
            raise ValueError("event_id and model_id are required")


@dataclass(frozen=True, slots=True)
class EnsembleDiversityPolicy:
    minimum_models: int = 2
    minimum_events: int = 100
    minimum_independent_clusters: int = 2
    prediction_agreement_redundancy: float = 0.95
    error_correlation_redundancy: float = 0.85
    maximum_cluster_weight: float = 0.75

    def __post_init__(self) -> None:
        if self.minimum_models < 2:
            raise ValueError("minimum_models must be >=2")
        if self.minimum_events <= 0 or self.minimum_independent_clusters <= 0:
            raise ValueError("sample/cluster thresholds must be positive")
        for name in (
            "prediction_agreement_redundancy",
            "error_correlation_redundancy",
            "maximum_cluster_weight",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")


@dataclass(frozen=True, slots=True)
class ModelPairDiversity:
    left: str
    right: str
    events: int
    prediction_agreement: float
    error_correlation: float
    joint_error_rate: float
    redundant: bool


@dataclass(frozen=True, slots=True)
class ModelRedundancyCluster:
    cluster_id: str
    members: tuple[str, ...]
    normalized_weight: float


@dataclass(frozen=True, slots=True)
class EnsembleDiversityReport:
    models: tuple[str, ...]
    events: int
    pairs: tuple[ModelPairDiversity, ...]
    clusters: tuple[ModelRedundancyCluster, ...]
    independent_clusters: int
    largest_cluster_size: int
    maximum_cluster_weight: float
    status: EnsembleDiversityStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is EnsembleDiversityStatus.QUALIFIED


def _matrix(
    rows: Sequence[OOFModelDecision],
) -> tuple[tuple[str, ...], tuple[str, ...], dict[tuple[str, str], OOFModelDecision]]:
    models = tuple(sorted({item.model_id for item in rows}))
    events = tuple(sorted({item.event_id for item in rows}))
    indexed: dict[tuple[str, str], OOFModelDecision] = {}
    truth_by_event: dict[str, EventKind] = {}
    for item in rows:
        key = (item.model_id, item.event_id)
        if key in indexed:
            raise ValueError(f"duplicate model/event observation: {key}")
        indexed[key] = item
        existing_truth = truth_by_event.get(item.event_id)
        if existing_truth is not None and existing_truth is not item.truth:
            raise ValueError(f"truth mismatch for event {item.event_id}")
        truth_by_event[item.event_id] = item.truth
    for model in models:
        missing = [event for event in events if (model, event) not in indexed]
        if missing:
            raise ValueError(f"model {model} is missing {len(missing)} events")
    return models, events, indexed


def _error_correlation(left: list[int], right: list[int]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_var = sum((value - left_mean) ** 2 for value in left)
    right_var = sum((value - right_mean) ** 2 for value in right)
    if left_var <= 0 or right_var <= 0:
        return 1.0 if left == right else 0.0
    covariance = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right, strict=True)
    )
    return max(-1.0, min(1.0, covariance / math.sqrt(left_var * right_var)))


def _pair(
    left: str,
    right: str,
    events: tuple[str, ...],
    indexed: Mapping[tuple[str, str], OOFModelDecision],
    policy: EnsembleDiversityPolicy,
) -> ModelPairDiversity:
    left_rows = [indexed[(left, event)] for event in events]
    right_rows = [indexed[(right, event)] for event in events]
    agreement = sum(
        left_row.predicted is right_row.predicted
        for left_row, right_row in zip(left_rows, right_rows, strict=True)
    ) / len(events)
    left_errors = [int(row.predicted is not row.truth) for row in left_rows]
    right_errors = [int(row.predicted is not row.truth) for row in right_rows]
    correlation = _error_correlation(left_errors, right_errors)
    joint_error = sum(
        left_error and right_error
        for left_error, right_error in zip(left_errors, right_errors, strict=True)
    ) / len(events)
    redundant = (
        agreement >= policy.prediction_agreement_redundancy
        or correlation >= policy.error_correlation_redundancy
    )
    return ModelPairDiversity(
        left=left,
        right=right,
        events=len(events),
        prediction_agreement=agreement,
        error_correlation=correlation,
        joint_error_rate=joint_error,
        redundant=redundant,
    )


def _clusters(
    models: tuple[str, ...],
    pairs: tuple[ModelPairDiversity, ...],
    weights: Mapping[str, float],
) -> tuple[ModelRedundancyCluster, ...]:
    parent = {model: model for model in models}

    def find(value: str) -> str:
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            next_value = parent[value]
            parent[value] = root
            value = next_value
        return root

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for pair in pairs:
        if pair.redundant:
            union(pair.left, pair.right)
    grouped: dict[str, list[str]] = defaultdict(list)
    for model in models:
        grouped[find(model)].append(model)
    return tuple(
        ModelRedundancyCluster(
            cluster_id=f"model-cluster-{index + 1}",
            members=tuple(sorted(members)),
            normalized_weight=sum(weights[model] for model in members),
        )
        for index, (_, members) in enumerate(sorted(grouped.items()))
    )


def evaluate_ensemble_diversity(
    rows: Sequence[OOFModelDecision],
    *,
    model_weights: Mapping[str, float] | None = None,
    policy: EnsembleDiversityPolicy | None = None,
) -> EnsembleDiversityReport:
    """Audit whether an ensemble contains genuinely independent OOF error structure.

    Near-clone models are clustered before weight concentration is evaluated. This component is
    research/shadow-only and never changes execution state or model weights by itself.
    """
    policy = policy or EnsembleDiversityPolicy()
    models, events, indexed = _matrix(rows)
    failures: list[str] = []
    if len(models) < policy.minimum_models:
        failures.append(f"insufficient_models:{len(models)}<{policy.minimum_models}")
    if len(events) < policy.minimum_events:
        failures.append(f"insufficient_events:{len(events)}<{policy.minimum_events}")
    raw_weights = (
        {model: float(model_weights.get(model, 0.0)) for model in models}
        if model_weights is not None
        else {model: 1.0 for model in models}
    )
    if any(value < 0 or not math.isfinite(value) for value in raw_weights.values()):
        raise ValueError("model weights must be finite and non-negative")
    total_weight = sum(raw_weights.values())
    if total_weight <= 0:
        raise ValueError("model weights must contain positive mass")
    weights = {model: value / total_weight for model, value in raw_weights.items()}
    pairs = tuple(
        _pair(models[left_index], models[right_index], events, indexed, policy)
        for left_index in range(len(models))
        for right_index in range(left_index + 1, len(models))
    )
    clusters = _clusters(models, pairs, weights)
    independent = len(clusters)
    largest = max((len(item.members) for item in clusters), default=0)
    maximum_weight = max((item.normalized_weight for item in clusters), default=0.0)
    if independent < policy.minimum_independent_clusters:
        failures.append(
            f"insufficient_independent_model_clusters:{independent}<"
            f"{policy.minimum_independent_clusters}"
        )
    if maximum_weight > policy.maximum_cluster_weight:
        failures.append(
            f"redundancy_cluster_weight_above_threshold:{maximum_weight:.6f}>"
            f"{policy.maximum_cluster_weight:.6f}"
        )
    if any(item.startswith("insufficient_") for item in failures):
        status = EnsembleDiversityStatus.INSUFFICIENT
    elif failures:
        status = EnsembleDiversityStatus.FAILED
    else:
        status = EnsembleDiversityStatus.QUALIFIED
    return EnsembleDiversityReport(
        models=models,
        events=len(events),
        pairs=pairs,
        clusters=clusters,
        independent_clusters=independent,
        largest_cluster_size=largest,
        maximum_cluster_weight=maximum_weight,
        status=status,
        failures=tuple(failures),
    )
