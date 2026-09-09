from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .domain import EventKind

_ACTIONABLE = frozenset(
    {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT, EventKind.STOP}
)


class AdaptiveEnsembleStatus(StrEnum):
    OBSERVING = "OBSERVING"
    TRUSTED = "TRUSTED"
    FROZEN_DRIFT = "FROZEN_DRIFT"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True, slots=True)
class AdaptiveEnsemblePolicy:
    learning_rate: float = 0.35
    forgetting_factor: float = 0.995
    minimum_updates: int = 100
    probability_floor: float = 1e-6
    wrong_action_penalty: float = 5.0
    maximum_single_model_weight: float = 0.85
    minimum_effective_models: float = 1.25

    def __post_init__(self) -> None:
        if self.learning_rate <= 0:
            raise ValueError("learning_rate must be positive")
        if not 0 < self.forgetting_factor <= 1:
            raise ValueError("forgetting_factor must be in (0,1]")
        if self.minimum_updates <= 0:
            raise ValueError("minimum_updates must be positive")
        if not 0 < self.probability_floor < 0.5:
            raise ValueError("probability_floor must be in (0,0.5)")
        if self.wrong_action_penalty < 0:
            raise ValueError("wrong_action_penalty cannot be negative")
        if not 0 < self.maximum_single_model_weight <= 1:
            raise ValueError("maximum_single_model_weight must be in (0,1]")
        if self.minimum_effective_models < 1:
            raise ValueError("minimum_effective_models must be >=1")


@dataclass(frozen=True, slots=True)
class AdaptiveModelWeight:
    model_id: str
    weight: float
    cumulative_loss: float


@dataclass(frozen=True, slots=True)
class AdaptiveEnsembleSnapshot:
    updates: int
    drift_active: bool
    weights: tuple[AdaptiveModelWeight, ...]
    effective_models: float
    entropy: float
    max_weight: float
    status: AdaptiveEnsembleStatus
    failures: tuple[str, ...]

    @property
    def trusted(self) -> bool:
        return self.status is AdaptiveEnsembleStatus.TRUSTED


_SCHEMA = """
CREATE TABLE IF NOT EXISTS adaptive_ensemble_state (
    ensemble_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    log_weight REAL NOT NULL,
    cumulative_loss REAL NOT NULL,
    updates INTEGER NOT NULL,
    updated_ts_utc TEXT NOT NULL,
    PRIMARY KEY(ensemble_id, model_id)
);
"""


def _normalize(log_weights: Mapping[str, float]) -> dict[str, float]:
    if not log_weights:
        return {}
    maximum = max(log_weights.values())
    exponentials = {key: math.exp(value - maximum) for key, value in log_weights.items()}
    total = sum(exponentials.values())
    if total <= 0:
        uniform = 1.0 / len(exponentials)
        return {key: uniform for key in exponentials}
    return {key: value / total for key, value in exponentials.items()}


def _distribution_loss(
    probabilities: Mapping[EventKind, float],
    truth: EventKind,
    policy: AdaptiveEnsemblePolicy,
) -> float:
    truth_probability = max(policy.probability_floor, probabilities.get(truth, 0.0))
    loss = -math.log(truth_probability)
    truth_actionable = truth in _ACTIONABLE
    if truth_actionable:
        wrong_action_mass = sum(
            probability
            for label, probability in probabilities.items()
            if label in _ACTIONABLE and label is not truth
        )
        loss += policy.wrong_action_penalty * wrong_action_mass
    else:
        false_action_mass = sum(
            probability for label, probability in probabilities.items() if label in _ACTIONABLE
        )
        loss += policy.wrong_action_penalty * false_action_mass
    return loss


def _snapshot(
    log_weights: Mapping[str, float],
    losses: Mapping[str, float],
    *,
    updates: int,
    drift_active: bool,
    policy: AdaptiveEnsemblePolicy,
) -> AdaptiveEnsembleSnapshot:
    normalized = _normalize(log_weights)
    entropy = -sum(weight * math.log(max(weight, 1e-12)) for weight in normalized.values())
    effective = math.exp(entropy) if normalized else 0.0
    maximum = max(normalized.values(), default=0.0)
    failures: list[str] = []
    if maximum > policy.maximum_single_model_weight:
        failures.append(
            f"single_model_weight_concentration:{maximum:.6f}>"
            f"{policy.maximum_single_model_weight:.6f}"
        )
    if normalized and effective < policy.minimum_effective_models:
        failures.append(
            f"effective_models_below_threshold:{effective:.6f}<"
            f"{policy.minimum_effective_models:.6f}"
        )
    if drift_active:
        status = AdaptiveEnsembleStatus.FROZEN_DRIFT
    elif updates < policy.minimum_updates:
        status = AdaptiveEnsembleStatus.OBSERVING
    elif failures:
        status = AdaptiveEnsembleStatus.DEGRADED
    else:
        status = AdaptiveEnsembleStatus.TRUSTED
    return AdaptiveEnsembleSnapshot(
        updates=updates,
        drift_active=drift_active,
        weights=tuple(
            AdaptiveModelWeight(model_id, normalized[model_id], losses.get(model_id, 0.0))
            for model_id in sorted(normalized)
        ),
        effective_models=effective,
        entropy=entropy,
        max_weight=maximum,
        status=status,
        failures=tuple(failures),
    )


def update_adaptive_ensemble(
    previous: AdaptiveEnsembleSnapshot | None,
    model_probabilities: Mapping[str, Mapping[EventKind, float]],
    truth: EventKind,
    *,
    drift_active: bool = False,
    policy: AdaptiveEnsemblePolicy | None = None,
) -> AdaptiveEnsembleSnapshot:
    """Update research/shadow ensemble weights using only newly observed labels.

    Weight adaptation freezes while drift is active. This prevents the ensemble from learning
    aggressively through a distribution break before the new regime has been characterized.
    """
    policy = policy or AdaptiveEnsemblePolicy()
    model_ids = tuple(sorted(model_probabilities))
    if not model_ids:
        raise ValueError("model_probabilities cannot be empty")
    if previous is not None:
        prior_ids = tuple(item.model_id for item in previous.weights)
        if prior_ids and prior_ids != model_ids:
            raise ValueError("adaptive ensemble model set changed without a new ensemble id")
        updates = previous.updates
        prior_log = {item.model_id: math.log(max(item.weight, 1e-12)) for item in previous.weights}
        cumulative = {item.model_id: item.cumulative_loss for item in previous.weights}
    else:
        uniform_log = -math.log(len(model_ids))
        prior_log = {model_id: uniform_log for model_id in model_ids}
        cumulative = {model_id: 0.0 for model_id in model_ids}
        updates = 0

    if drift_active:
        return _snapshot(
            prior_log,
            cumulative,
            updates=updates,
            drift_active=True,
            policy=policy,
        )

    updated_log: dict[str, float] = {}
    updated_losses: dict[str, float] = {}
    for model_id in model_ids:
        probabilities = model_probabilities[model_id]
        total = sum(probabilities.values())
        if any(value < 0 or value > 1 for value in probabilities.values()) or abs(total - 1.0) > 1e-6:
            raise ValueError(f"invalid probability distribution for model {model_id}")
        loss = _distribution_loss(probabilities, truth, policy)
        updated_losses[model_id] = (
            policy.forgetting_factor * cumulative.get(model_id, 0.0) + loss
        )
        updated_log[model_id] = (
            policy.forgetting_factor * prior_log.get(model_id, 0.0)
            - policy.learning_rate * loss
        )
    return _snapshot(
        updated_log,
        updated_losses,
        updates=updates + 1,
        drift_active=False,
        policy=policy,
    )


def persist_adaptive_snapshot(
    path: str | Path,
    ensemble_id: str,
    snapshot: AdaptiveEnsembleSnapshot,
) -> None:
    if not ensemble_id.strip():
        raise ValueError("ensemble_id is required")
    timestamp = datetime.now(UTC).isoformat()
    with sqlite3.connect(str(path)) as db:
        db.executescript(_SCHEMA)
        db.execute("DELETE FROM adaptive_ensemble_state WHERE ensemble_id=?", (ensemble_id,))
        for item in snapshot.weights:
            db.execute(
                """
                INSERT INTO adaptive_ensemble_state
                (ensemble_id,model_id,log_weight,cumulative_loss,updates,updated_ts_utc)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    ensemble_id,
                    item.model_id,
                    math.log(max(item.weight, 1e-12)),
                    item.cumulative_loss,
                    snapshot.updates,
                    timestamp,
                ),
            )


def fingerprint_adaptive_snapshot(snapshot: AdaptiveEnsembleSnapshot) -> str:
    payload = {
        "updates": snapshot.updates,
        "drift_active": snapshot.drift_active,
        "weights": [
            [item.model_id, round(item.weight, 12), round(item.cumulative_loss, 12)]
            for item in snapshot.weights
        ],
        "status": snapshot.status.value,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
