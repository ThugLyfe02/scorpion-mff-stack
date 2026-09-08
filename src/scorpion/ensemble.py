from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .accuracy import ACTIONABLE_KINDS
from .domain import EventKind
from .shadow import ShadowPrediction


@dataclass(frozen=True, slots=True)
class EnsembleAssessment:
    consensus_kind: EventKind | None
    consensus_confidence: float
    entropy: float
    margin: float
    actionable_disagreement: bool
    needs_adjudication: bool
    distribution: dict[str, float]


def _model_key(prediction: ShadowPrediction) -> str:
    return f"{prediction.model_name}@{prediction.model_version}"


def assess_ensemble(
    predictions: Sequence[ShadowPrediction],
    *,
    weights: Mapping[str, float] | None = None,
    entropy_review_threshold: float = 0.30,
    consensus_review_threshold: float = 0.80,
) -> EnsembleAssessment:
    if not predictions:
        return EnsembleAssessment(None, 0.0, 0.0, 0.0, False, True, {})

    vote_mass: dict[EventKind, float] = defaultdict(float)
    total_weight = 0.0
    action_states: set[bool] = set()
    for prediction in predictions:
        base_weight = 1.0 if weights is None else float(weights.get(_model_key(prediction), 1.0))
        if base_weight < 0.0:
            raise ValueError("model weights must be non-negative")
        weight = base_weight * max(0.0, min(1.0, prediction.confidence))
        vote_mass[prediction.predicted_kind] += weight
        total_weight += weight
        action_states.add(prediction.predicted_kind in ACTIONABLE_KINDS)

    if total_weight <= 0.0:
        return EnsembleAssessment(None, 0.0, 0.0, 0.0, len(action_states) > 1, True, {})

    ordered_votes = sorted(vote_mass.items(), key=lambda item: item[0].value)
    distribution = {kind: mass / total_weight for kind, mass in ordered_votes}
    ranked = sorted(distribution.items(), key=lambda item: (-item[1], item[0].value))
    consensus_kind, consensus_confidence = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = consensus_confidence - second
    raw_entropy = -sum(probability * math.log(probability) for probability in distribution.values())
    maximum_entropy = math.log(len(EventKind))
    entropy = raw_entropy / maximum_entropy if maximum_entropy else 0.0
    actionable_disagreement = len(action_states) > 1
    needs_adjudication = (
        actionable_disagreement
        or entropy >= entropy_review_threshold
        or consensus_confidence < consensus_review_threshold
    )
    return EnsembleAssessment(
        consensus_kind=consensus_kind,
        consensus_confidence=consensus_confidence,
        entropy=entropy,
        margin=margin,
        actionable_disagreement=actionable_disagreement,
        needs_adjudication=needs_adjudication,
        distribution={kind.value: probability for kind, probability in distribution.items()},
    )
