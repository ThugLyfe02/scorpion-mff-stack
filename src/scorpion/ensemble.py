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
    support_confidence: float
    participating_models: int
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
    support_review_threshold: float = 0.75,
) -> EnsembleAssessment:
    if not predictions:
        return EnsembleAssessment(None, 0.0, 0.0, 0, 0.0, 0.0, False, True, {})

    vote_mass: dict[EventKind, float] = defaultdict(float)
    total_vote_mass = 0.0
    total_base_weight = 0.0
    action_states: set[bool] = set()
    participating_models = 0

    for prediction in predictions:
        base_weight = 1.0 if weights is None else float(weights.get(_model_key(prediction), 1.0))
        if base_weight < 0.0:
            raise ValueError("model weights must be non-negative")
        if base_weight == 0.0:
            continue

        participating_models += 1
        total_base_weight += base_weight
        confidence = max(0.0, min(1.0, prediction.confidence))
        mass = base_weight * confidence
        if mass <= 0.0:
            continue

        vote_mass[prediction.predicted_kind] += mass
        total_vote_mass += mass
        action_states.add(prediction.predicted_kind in ACTIONABLE_KINDS)

    support_confidence = (
        total_vote_mass / total_base_weight if total_base_weight > 0.0 else 0.0
    )
    if total_vote_mass <= 0.0:
        return EnsembleAssessment(
            None,
            0.0,
            support_confidence,
            participating_models,
            0.0,
            0.0,
            False,
            True,
            {},
        )

    ordered_votes = sorted(vote_mass.items(), key=lambda item: item[0].value)
    distribution = {kind: mass / total_vote_mass for kind, mass in ordered_votes if mass > 0.0}
    ranked = sorted(distribution.items(), key=lambda item: (-item[1], item[0].value))
    consensus_kind, consensus_confidence = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = consensus_confidence - second

    raw_entropy = -sum(
        probability * math.log(probability)
        for probability in distribution.values()
        if probability > 0.0
    )
    maximum_entropy = math.log(len(distribution)) if len(distribution) > 1 else 0.0
    entropy = raw_entropy / maximum_entropy if maximum_entropy else 0.0
    actionable_disagreement = len(action_states) > 1
    needs_adjudication = (
        actionable_disagreement
        or entropy >= entropy_review_threshold
        or consensus_confidence < consensus_review_threshold
        or support_confidence < support_review_threshold
    )

    return EnsembleAssessment(
        consensus_kind=consensus_kind,
        consensus_confidence=consensus_confidence,
        support_confidence=support_confidence,
        participating_models=participating_models,
        entropy=entropy,
        margin=margin,
        actionable_disagreement=actionable_disagreement,
        needs_adjudication=needs_adjudication,
        distribution={kind.value: probability for kind, probability in distribution.items()},
    )
