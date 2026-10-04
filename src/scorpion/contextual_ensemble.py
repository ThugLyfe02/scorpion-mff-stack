from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from itertools import combinations

from .domain import EventKind

ContextKey = tuple[tuple[str, str], ...]
ModelWeights = tuple[tuple[str, float], ...]


class ContextualEnsembleStatus(StrEnum):
    BLOCKED = "BLOCKED"
    READY_FOR_SHADOW_RESEARCH = "READY_FOR_SHADOW_RESEARCH"


@dataclass(frozen=True, slots=True)
class ContextualEnsembleObservation:
    event_id: str
    fold_id: str
    observed_ts_utc: datetime
    truth: EventKind
    model_probabilities: Mapping[str, Mapping[EventKind, float]]
    slices: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.fold_id.strip():
            raise ValueError("event_id and fold_id are required")
        if self.observed_ts_utc.tzinfo is None or self.observed_ts_utc.utcoffset() is None:
            raise ValueError("observed_ts_utc must be timezone-aware")
        object.__setattr__(self, "observed_ts_utc", self.observed_ts_utc.astimezone(UTC))
        if not self.model_probabilities:
            raise ValueError("model_probabilities cannot be empty")
        if any(not key.strip() or not value.strip() for key, value in self.slices.items()):
            raise ValueError("slice names and values must be non-empty")


@dataclass(frozen=True, slots=True)
class ContextualEnsemblePolicy:
    context_dimensions: tuple[str, ...]
    interaction_order: int = 1
    minimum_global_samples: int = 100
    minimum_oof_folds: int = 3
    minimum_context_samples: int = 30
    minimum_context_folds: int = 2
    prior_strength: float = 50.0
    skill_scale: float = 3.0
    maximum_context_weight_shift: float = 0.15
    minimum_context_oof_log_loss_gain: float = 0.0
    maximum_contexts: int = 500
    minimum_holdout_samples: int = 50
    minimum_holdout_days: int = 3
    minimum_mean_log_loss_improvement: float = 0.001
    minimum_bootstrap_lower_bound: float = 0.0
    maximum_accuracy_regression: float = 0.0
    maximum_brier_regression: float = 0.0
    maximum_actionable_false_positive_regression: float = 0.0
    bootstrap_resamples: int = 1000
    bootstrap_block_days: int = 3
    bootstrap_alpha: float = 0.05
    bootstrap_seed: int = 17
    actionable_labels: tuple[EventKind, ...] = (
        EventKind.ENTRY,
        EventKind.ADD,
        EventKind.TRIM,
        EventKind.EXIT,
        EventKind.STOP,
    )

    def __post_init__(self) -> None:
        if not self.context_dimensions:
            raise ValueError("context_dimensions cannot be empty")
        if any(not item.strip() for item in self.context_dimensions):
            raise ValueError("context dimensions must be non-empty")
        if len(self.context_dimensions) != len(set(self.context_dimensions)):
            raise ValueError("context_dimensions must be unique")
        if self.interaction_order < 1 or self.interaction_order > len(self.context_dimensions):
            raise ValueError("interaction_order is outside the configured dimensions")
        if min(
            self.minimum_global_samples,
            self.minimum_oof_folds,
            self.minimum_context_samples,
            self.minimum_context_folds,
            self.maximum_contexts,
            self.minimum_holdout_samples,
            self.minimum_holdout_days,
            self.bootstrap_resamples,
            self.bootstrap_block_days,
        ) <= 0:
            raise ValueError("sample, fold, context and bootstrap counts must be positive")
        for name in ("prior_strength", "skill_scale"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in (
            "maximum_context_weight_shift",
            "maximum_accuracy_regression",
            "maximum_brier_regression",
            "maximum_actionable_false_positive_regression",
            "bootstrap_alpha",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be finite and in [0,1]")
        for name in (
            "minimum_context_oof_log_loss_gain",
            "minimum_mean_log_loss_improvement",
            "minimum_bootstrap_lower_bound",
        ):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.bootstrap_alpha <= 0 or self.bootstrap_alpha >= 1:
            raise ValueError("bootstrap_alpha must be in (0,1)")
        if len(self.actionable_labels) != len(set(self.actionable_labels)):
            raise ValueError("actionable_labels must be unique")


@dataclass(frozen=True, slots=True)
class ContextWeightRecord:
    context_key: ContextKey
    support: int
    folds: int
    shrinkage: float
    local_log_loss_gain: float
    weights: ModelWeights


@dataclass(frozen=True, slots=True)
class ContextualEnsembleFit:
    model_ids: tuple[str, ...]
    labels: tuple[EventKind, ...]
    global_weights: ModelWeights
    contexts: tuple[ContextWeightRecord, ...]
    training_event_ids: frozenset[str]
    training_fingerprint: str
    policy_fingerprint: str
    fit_hash: str


@dataclass(frozen=True, slots=True)
class ContextualEnsemblePrediction:
    probabilities: tuple[tuple[EventKind, float], ...]
    selected_context: ContextKey | None
    weights: ModelWeights


@dataclass(frozen=True, slots=True)
class ContextualEnsembleEvaluation:
    status: ContextualEnsembleStatus
    samples: int
    unique_days: int
    contextual_usage_rate: float
    global_log_loss: float
    contextual_log_loss: float
    mean_log_loss_improvement: float
    bootstrap_lower_bound: float
    global_accuracy: float
    contextual_accuracy: float
    global_brier: float
    contextual_brier: float
    global_actionable_false_positive_rate: float
    contextual_actionable_false_positive_rate: float
    failures: tuple[str, ...]
    report_hash: str

    @property
    def ready_for_shadow_research(self) -> bool:
        return self.status is ContextualEnsembleStatus.READY_FOR_SHADOW_RESEARCH


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _validate_observations(
    rows: tuple[ContextualEnsembleObservation, ...],
    *,
    context_dimensions: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[EventKind, ...]]:
    if not rows:
        raise ValueError("observations cannot be empty")
    event_ids = [row.event_id for row in rows]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("observation event ids must be unique")
    first_models = tuple(sorted(rows[0].model_probabilities))
    if len(first_models) < 2 or any(not model_id.strip() for model_id in first_models):
        raise ValueError("at least two named models are required")
    first_labels = tuple(sorted(rows[0].model_probabilities[first_models[0]], key=lambda x: x.value))
    if not first_labels:
        raise ValueError("model probability label set cannot be empty")
    for row in rows:
        if tuple(sorted(row.model_probabilities)) != first_models:
            raise ValueError(f"model set mismatch for event {row.event_id}")
        if row.truth not in first_labels:
            raise ValueError(f"truth label missing from probabilities for event {row.event_id}")
        if any(dimension not in row.slices for dimension in context_dimensions):
            raise ValueError(f"event {row.event_id} is missing a configured context dimension")
        for model_id in first_models:
            probabilities = row.model_probabilities[model_id]
            labels = tuple(sorted(probabilities, key=lambda x: x.value))
            if labels != first_labels:
                raise ValueError(f"label set mismatch for model {model_id}")
            values = tuple(probabilities[label] for label in first_labels)
            if any(not math.isfinite(value) or value < 0 or value > 1 for value in values):
                raise ValueError(f"invalid probabilities for model {model_id}")
            if abs(sum(values) - 1.0) > 1e-6:
                raise ValueError(f"probabilities must sum to one for model {model_id}")
    return first_models, first_labels


def _context_keys(
    slices: Mapping[str, str],
    dimensions: tuple[str, ...],
    interaction_order: int,
) -> tuple[ContextKey, ...]:
    output: list[ContextKey] = []
    for order in range(1, interaction_order + 1):
        for subset in combinations(dimensions, order):
            output.append(tuple((dimension, slices[dimension]) for dimension in subset))
    return tuple(output)


def _log_loss(probability: float) -> float:
    return -math.log(max(probability, 1e-12))


def _model_losses(
    rows: tuple[ContextualEnsembleObservation, ...],
    models: tuple[str, ...],
) -> dict[str, float]:
    return {
        model_id: sum(
            _log_loss(row.model_probabilities[model_id][row.truth]) for row in rows
        )
        / len(rows)
        for model_id in models
    }


def _skill_weights(losses: Mapping[str, float], *, scale: float) -> dict[str, float]:
    minimum = min(losses.values())
    raw = {
        model_id: math.exp(-scale * (loss - minimum))
        for model_id, loss in losses.items()
    }
    total = sum(raw.values())
    return {model_id: raw[model_id] / total for model_id in sorted(raw)}


def _weighted_probabilities(
    row: ContextualEnsembleObservation,
    *,
    weights: Mapping[str, float],
    labels: tuple[EventKind, ...],
) -> dict[EventKind, float]:
    result = {
        label: sum(
            weights[model_id] * row.model_probabilities[model_id][label]
            for model_id in weights
        )
        for label in labels
    }
    total = sum(result.values())
    if total <= 0:
        raise ValueError("weighted ensemble produced non-positive probability mass")
    return {label: value / total for label, value in result.items()}


def _mean_ensemble_loss(
    rows: tuple[ContextualEnsembleObservation, ...],
    *,
    weights: Mapping[str, float],
    labels: tuple[EventKind, ...],
) -> float:
    return sum(
        _log_loss(_weighted_probabilities(row, weights=weights, labels=labels)[row.truth])
        for row in rows
    ) / len(rows)


def _weights_tuple(weights: Mapping[str, float]) -> ModelWeights:
    return tuple((model_id, weights[model_id]) for model_id in sorted(weights))


def _weights_dict(weights: ModelWeights) -> dict[str, float]:
    return dict(weights)


def _policy_fingerprint(policy: ContextualEnsemblePolicy) -> str:
    material = asdict(policy)
    material["actionable_labels"] = tuple(label.value for label in policy.actionable_labels)
    return _hash_payload({"version": "contextual-ensemble-policy-v1", **material})


def fit_contextual_ensemble(
    rows: tuple[ContextualEnsembleObservation, ...],
    *,
    policy: ContextualEnsemblePolicy,
) -> ContextualEnsembleFit:
    """Fit context-conditioned weights from already out-of-fold model predictions only."""
    models, labels = _validate_observations(rows, context_dimensions=policy.context_dimensions)
    if len(rows) < policy.minimum_global_samples:
        raise ValueError("insufficient global OOF samples")
    folds = {row.fold_id for row in rows}
    if len(folds) < policy.minimum_oof_folds:
        raise ValueError("insufficient distinct OOF folds")

    global_weights = _skill_weights(_model_losses(rows, models), scale=policy.skill_scale)
    grouped: dict[ContextKey, list[ContextualEnsembleObservation]] = defaultdict(list)
    for row in rows:
        for key in _context_keys(row.slices, policy.context_dimensions, policy.interaction_order):
            grouped[key].append(row)

    candidates: list[ContextWeightRecord] = []
    for key, members in grouped.items():
        context_rows = tuple(members)
        context_folds = {row.fold_id for row in context_rows}
        if len(context_rows) < policy.minimum_context_samples:
            continue
        if len(context_folds) < policy.minimum_context_folds:
            continue
        local_raw = _skill_weights(_model_losses(context_rows, models), scale=policy.skill_scale)
        maximum_difference = max(
            abs(local_raw[model_id] - global_weights[model_id]) for model_id in models
        )
        shrinkage = len(context_rows) / (len(context_rows) + policy.prior_strength)
        if maximum_difference > 0:
            shrinkage = min(
                shrinkage,
                policy.maximum_context_weight_shift / maximum_difference,
            )
        blended = {
            model_id: global_weights[model_id]
            + shrinkage * (local_raw[model_id] - global_weights[model_id])
            for model_id in models
        }
        total = sum(blended.values())
        blended = {model_id: value / total for model_id, value in blended.items()}
        global_loss = _mean_ensemble_loss(
            context_rows,
            weights=global_weights,
            labels=labels,
        )
        local_loss = _mean_ensemble_loss(context_rows, weights=blended, labels=labels)
        gain = global_loss - local_loss
        if gain < policy.minimum_context_oof_log_loss_gain:
            continue
        candidates.append(
            ContextWeightRecord(
                context_key=key,
                support=len(context_rows),
                folds=len(context_folds),
                shrinkage=shrinkage,
                local_log_loss_gain=gain,
                weights=_weights_tuple(blended),
            )
        )
    contexts = tuple(
        sorted(
            candidates,
            key=lambda item: (-item.support, -len(item.context_key), item.context_key),
        )[: policy.maximum_contexts]
    )
    training_ids = frozenset(row.event_id for row in rows)
    training_fingerprint = _hash_payload(
        {
            "version": "contextual-ensemble-oof-data-v1",
            "events": [
                {
                    "event_id": row.event_id,
                    "fold_id": row.fold_id,
                    "observed_ts_utc": row.observed_ts_utc.isoformat(),
                    "truth": row.truth.value,
                    "slices": tuple(sorted(row.slices.items())),
                    "model_probabilities": {
                        model_id: {
                            label.value: row.model_probabilities[model_id][label]
                            for label in labels
                        }
                        for model_id in models
                    },
                }
                for row in sorted(rows, key=lambda item: item.event_id)
            ],
        }
    )
    policy_fingerprint = _policy_fingerprint(policy)
    fit_hash = _hash_payload(
        {
            "version": "contextual-ensemble-fit-v1",
            "training_fingerprint": training_fingerprint,
            "policy_fingerprint": policy_fingerprint,
            "global_weights": _weights_tuple(global_weights),
            "contexts": [asdict(item) for item in contexts],
        }
    )
    return ContextualEnsembleFit(
        model_ids=models,
        labels=labels,
        global_weights=_weights_tuple(global_weights),
        contexts=contexts,
        training_event_ids=training_ids,
        training_fingerprint=training_fingerprint,
        policy_fingerprint=policy_fingerprint,
        fit_hash=fit_hash,
    )


def predict_contextual_ensemble(
    fit: ContextualEnsembleFit,
    row: ContextualEnsembleObservation,
) -> ContextualEnsemblePrediction:
    models, labels = _validate_observations(
        (row,),
        context_dimensions=tuple(
            sorted({dimension for record in fit.contexts for dimension, _ in record.context_key})
        )
        or tuple(sorted(row.slices)),
    )
    if models != fit.model_ids or labels != fit.labels:
        raise ValueError("prediction model or label set does not match contextual fit")
    matching = tuple(
        record
        for record in fit.contexts
        if all(row.slices.get(dimension) == value for dimension, value in record.context_key)
    )
    selected = (
        max(
            matching,
            key=lambda item: (len(item.context_key), item.support, item.context_key),
        )
        if matching
        else None
    )
    weights = selected.weights if selected is not None else fit.global_weights
    probabilities = _weighted_probabilities(
        row,
        weights=_weights_dict(weights),
        labels=fit.labels,
    )
    return ContextualEnsemblePrediction(
        probabilities=tuple((label, probabilities[label]) for label in fit.labels),
        selected_context=selected.context_key if selected is not None else None,
        weights=weights,
    )


def _argmax(probabilities: Mapping[EventKind, float]) -> EventKind:
    return min(probabilities, key=lambda label: (-probabilities[label], label.value))


def _brier(probabilities: Mapping[EventKind, float], truth: EventKind) -> float:
    return sum(
        (probability - (1.0 if label is truth else 0.0)) ** 2
        for label, probability in probabilities.items()
    ) / len(probabilities)


def _block_bootstrap_lower_bound(
    daily_improvements: tuple[float, ...],
    *,
    resamples: int,
    block_size: int,
    alpha: float,
    seed: int,
) -> float:
    if not daily_improvements:
        return float("-inf")
    rng = random.Random(seed)
    count = len(daily_improvements)
    values: list[float] = []
    for _ in range(resamples):
        sample: list[float] = []
        while len(sample) < count:
            start = rng.randrange(count)
            sample.extend(
                daily_improvements[(start + offset) % count]
                for offset in range(block_size)
            )
        values.append(sum(sample[:count]) / count)
    values.sort()
    index = max(0, min(len(values) - 1, math.floor(alpha * (len(values) - 1))))
    return values[index]


def evaluate_contextual_ensemble(
    fit: ContextualEnsembleFit,
    holdout: tuple[ContextualEnsembleObservation, ...],
    *,
    policy: ContextualEnsemblePolicy,
) -> ContextualEnsembleEvaluation:
    """Qualify contextual weighting only on a frozen holdout never used during fitting."""
    models, labels = _validate_observations(
        holdout,
        context_dimensions=policy.context_dimensions,
    )
    if models != fit.model_ids or labels != fit.labels:
        raise ValueError("holdout model or label set does not match contextual fit")
    overlap = fit.training_event_ids & {row.event_id for row in holdout}
    if overlap:
        raise ValueError("frozen holdout overlaps contextual ensemble training events")
    if fit.policy_fingerprint != _policy_fingerprint(policy):
        raise ValueError("contextual ensemble policy changed after fitting")

    global_weights = _weights_dict(fit.global_weights)
    global_losses: list[float] = []
    contextual_losses: list[float] = []
    global_briers: list[float] = []
    contextual_briers: list[float] = []
    global_correct = 0
    contextual_correct = 0
    context_used = 0
    non_actionable = 0
    global_false_positive = 0
    contextual_false_positive = 0
    actionable = set(policy.actionable_labels)
    daily_deltas: dict[str, list[float]] = defaultdict(list)

    for row in holdout:
        global_probs = _weighted_probabilities(
            row,
            weights=global_weights,
            labels=fit.labels,
        )
        contextual_prediction = predict_contextual_ensemble(fit, row)
        contextual_probs = dict(contextual_prediction.probabilities)
        if contextual_prediction.selected_context is not None:
            context_used += 1
        global_loss = _log_loss(global_probs[row.truth])
        contextual_loss = _log_loss(contextual_probs[row.truth])
        global_losses.append(global_loss)
        contextual_losses.append(contextual_loss)
        daily_deltas[row.observed_ts_utc.date().isoformat()].append(
            global_loss - contextual_loss
        )
        global_briers.append(_brier(global_probs, row.truth))
        contextual_briers.append(_brier(contextual_probs, row.truth))
        global_prediction = _argmax(global_probs)
        contextual_label = _argmax(contextual_probs)
        global_correct += global_prediction is row.truth
        contextual_correct += contextual_label is row.truth
        if row.truth not in actionable:
            non_actionable += 1
            global_false_positive += global_prediction in actionable
            contextual_false_positive += contextual_label in actionable

    samples = len(holdout)
    global_log_loss = sum(global_losses) / samples
    contextual_log_loss = sum(contextual_losses) / samples
    improvement = global_log_loss - contextual_log_loss
    global_accuracy = global_correct / samples
    contextual_accuracy = contextual_correct / samples
    global_brier = sum(global_briers) / samples
    contextual_brier = sum(contextual_briers) / samples
    global_fpr = global_false_positive / non_actionable if non_actionable else 0.0
    contextual_fpr = contextual_false_positive / non_actionable if non_actionable else 0.0
    daily_means = tuple(
        sum(values) / len(values) for _, values in sorted(daily_deltas.items())
    )
    lower_bound = _block_bootstrap_lower_bound(
        daily_means,
        resamples=policy.bootstrap_resamples,
        block_size=policy.bootstrap_block_days,
        alpha=policy.bootstrap_alpha,
        seed=policy.bootstrap_seed,
    )

    failures: list[str] = []
    if samples < policy.minimum_holdout_samples:
        failures.append("insufficient_holdout_samples")
    if len(daily_means) < policy.minimum_holdout_days:
        failures.append("insufficient_holdout_days")
    if improvement < policy.minimum_mean_log_loss_improvement:
        failures.append("insufficient_mean_log_loss_improvement")
    if lower_bound < policy.minimum_bootstrap_lower_bound:
        failures.append("bootstrap_log_loss_improvement_not_robust")
    if global_accuracy - contextual_accuracy > policy.maximum_accuracy_regression:
        failures.append("holdout_accuracy_regression")
    if contextual_brier - global_brier > policy.maximum_brier_regression:
        failures.append("holdout_brier_regression")
    if contextual_fpr - global_fpr > policy.maximum_actionable_false_positive_regression:
        failures.append("actionable_false_positive_regression")
    status = (
        ContextualEnsembleStatus.READY_FOR_SHADOW_RESEARCH
        if not failures
        else ContextualEnsembleStatus.BLOCKED
    )
    material = {
        "version": "contextual-ensemble-holdout-v1",
        "fit_hash": fit.fit_hash,
        "samples": samples,
        "unique_days": len(daily_means),
        "contextual_usage_rate": context_used / samples,
        "global_log_loss": global_log_loss,
        "contextual_log_loss": contextual_log_loss,
        "mean_log_loss_improvement": improvement,
        "bootstrap_lower_bound": lower_bound,
        "global_accuracy": global_accuracy,
        "contextual_accuracy": contextual_accuracy,
        "global_brier": global_brier,
        "contextual_brier": contextual_brier,
        "global_actionable_false_positive_rate": global_fpr,
        "contextual_actionable_false_positive_rate": contextual_fpr,
        "failures": failures,
    }
    report_hash = _hash_payload(material)
    return ContextualEnsembleEvaluation(
        status=status,
        samples=samples,
        unique_days=len(daily_means),
        contextual_usage_rate=context_used / samples,
        global_log_loss=global_log_loss,
        contextual_log_loss=contextual_log_loss,
        mean_log_loss_improvement=improvement,
        bootstrap_lower_bound=lower_bound,
        global_accuracy=global_accuracy,
        contextual_accuracy=contextual_accuracy,
        global_brier=global_brier,
        contextual_brier=contextual_brier,
        global_actionable_false_positive_rate=global_fpr,
        contextual_actionable_false_positive_rate=contextual_fpr,
        failures=tuple(failures),
        report_hash=report_hash,
    )
