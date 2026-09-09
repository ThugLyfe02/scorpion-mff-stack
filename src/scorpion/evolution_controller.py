from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .adaptive_ensemble import AdaptiveEnsembleSnapshot
from .evolution_ledger import (
    EvolutionCandidate,
    EvolutionTrigger,
    build_evolution_candidate,
    persist_evolution_candidate,
)
from .feature_stability import FeatureStabilityReport
from .regime_mixture import RegimeMixtureReport


@dataclass(frozen=True, slots=True)
class EvolutionControlPolicy:
    minimum_new_samples: int = 100
    minimum_weight_l1_change: float = 0.15
    minimum_feature_changes: int = 1
    minimum_regime_changes: int = 1
    cooldown: timedelta = timedelta(hours=6)

    def __post_init__(self) -> None:
        if self.minimum_new_samples <= 0:
            raise ValueError("minimum_new_samples must be positive")
        if not 0 <= self.minimum_weight_l1_change <= 2:
            raise ValueError("minimum_weight_l1_change must be in [0,2]")
        if self.minimum_feature_changes <= 0 or self.minimum_regime_changes <= 0:
            raise ValueError("change thresholds must be positive")
        if self.cooldown < timedelta(0):
            raise ValueError("cooldown cannot be negative")


@dataclass(frozen=True, slots=True)
class EvolutionBaseline:
    candidate_id: str
    created_ts_utc: datetime
    dataset_samples: int
    stable_features: tuple[str, ...]
    model_weights: tuple[tuple[str, float], ...]
    regimes: tuple[str, ...]
    fill_model_trusted: bool

    def __post_init__(self) -> None:
        if self.created_ts_utc.tzinfo is None or self.created_ts_utc.utcoffset() is None:
            raise ValueError("created_ts_utc must be timezone-aware")
        if self.dataset_samples < 0:
            raise ValueError("dataset_samples cannot be negative")


@dataclass(frozen=True, slots=True)
class EvolutionObservation:
    observed_ts_utc: datetime
    dataset_samples: int
    feature_stability: FeatureStabilityReport
    adaptive_ensemble: AdaptiveEnsembleSnapshot
    regime_mixture: RegimeMixtureReport
    fill_model_trusted: bool
    drift_active: bool

    def __post_init__(self) -> None:
        if self.observed_ts_utc.tzinfo is None or self.observed_ts_utc.utcoffset() is None:
            raise ValueError("observed_ts_utc must be timezone-aware")
        if self.dataset_samples < 0:
            raise ValueError("dataset_samples cannot be negative")


@dataclass(frozen=True, slots=True)
class EvolutionDecision:
    should_generate: bool
    primary_trigger: EvolutionTrigger | None
    triggers: tuple[EvolutionTrigger, ...]
    new_samples: int
    weight_l1_change: float
    feature_changes: int
    regime_changes: int
    cooldown_active: bool
    reasons: tuple[str, ...]


def _weights(snapshot: AdaptiveEnsembleSnapshot) -> dict[str, float]:
    return {item.model_id: item.weight for item in snapshot.weights}


def _weight_l1(
    previous: tuple[tuple[str, float], ...],
    current: AdaptiveEnsembleSnapshot,
) -> float:
    left = dict(previous)
    right = _weights(current)
    model_ids = set(left) | set(right)
    return sum(abs(left.get(model_id, 0.0) - right.get(model_id, 0.0)) for model_id in model_ids)


def evaluate_evolution_need(
    baseline: EvolutionBaseline | None,
    observation: EvolutionObservation,
    *,
    policy: EvolutionControlPolicy | None = None,
) -> EvolutionDecision:
    """Decide whether evidence changed enough to generate a new research challenger."""
    policy = policy or EvolutionControlPolicy()
    if baseline is None:
        enough = observation.dataset_samples >= policy.minimum_new_samples
        return EvolutionDecision(
            should_generate=enough,
            primary_trigger=EvolutionTrigger.NEW_DATA if enough else None,
            triggers=(EvolutionTrigger.NEW_DATA,) if enough else (),
            new_samples=observation.dataset_samples,
            weight_l1_change=0.0,
            feature_changes=len(observation.feature_stability.qualified_features),
            regime_changes=len(observation.regime_mixture.regimes),
            cooldown_active=False,
            reasons=(
                "initial_evolution_candidate_ready"
                if enough
                else "insufficient_initial_data_for_evolution"
            ,),
        )

    new_samples = max(0, observation.dataset_samples - baseline.dataset_samples)
    weight_change = _weight_l1(baseline.model_weights, observation.adaptive_ensemble)
    feature_changes = len(
        set(baseline.stable_features)
        ^ set(observation.feature_stability.qualified_features)
    )
    regime_changes = len(set(baseline.regimes) ^ set(observation.regime_mixture.regimes))
    cooldown_active = observation.observed_ts_utc < baseline.created_ts_utc + policy.cooldown

    triggers: list[EvolutionTrigger] = []
    reasons: list[str] = []
    if observation.drift_active:
        triggers.append(EvolutionTrigger.DRIFT)
        reasons.append("distribution_drift_active")
    if baseline.fill_model_trusted and not observation.fill_model_trusted:
        triggers.append(EvolutionTrigger.CALIBRATION_DECAY)
        reasons.append("fill_model_trust_degraded")
    if feature_changes >= policy.minimum_feature_changes or regime_changes >= policy.minimum_regime_changes:
        triggers.append(EvolutionTrigger.REGIME_CHANGE)
        reasons.append(
            f"structural_change:features={feature_changes},regimes={regime_changes}"
        )
    if new_samples >= policy.minimum_new_samples:
        triggers.append(EvolutionTrigger.NEW_DATA)
        reasons.append(f"new_samples={new_samples}")
    if weight_change >= policy.minimum_weight_l1_change:
        triggers.append(EvolutionTrigger.MODEL_DEGRADATION)
        reasons.append(f"adaptive_weight_l1_change={weight_change:.6f}")

    critical = {
        EvolutionTrigger.DRIFT,
        EvolutionTrigger.CALIBRATION_DECAY,
    }
    critical_triggered = any(trigger in critical for trigger in triggers)
    should_generate = bool(triggers) and (critical_triggered or not cooldown_active)
    if triggers and cooldown_active and not critical_triggered:
        reasons.append("cooldown_suppressed_noncritical_evolution")

    priority = (
        EvolutionTrigger.DRIFT,
        EvolutionTrigger.CALIBRATION_DECAY,
        EvolutionTrigger.REGIME_CHANGE,
        EvolutionTrigger.MODEL_DEGRADATION,
        EvolutionTrigger.NEW_DATA,
    )
    primary = next((trigger for trigger in priority if trigger in triggers), None)
    return EvolutionDecision(
        should_generate=should_generate,
        primary_trigger=primary if should_generate else None,
        triggers=tuple(dict.fromkeys(triggers)),
        new_samples=new_samples,
        weight_l1_change=weight_change,
        feature_changes=feature_changes,
        regime_changes=regime_changes,
        cooldown_active=cooldown_active,
        reasons=tuple(reasons),
    )


def maybe_generate_evolution_candidate(
    path: str,
    *,
    baseline: EvolutionBaseline | None,
    observation: EvolutionObservation,
    parent_release_id: str,
    code_revision: str,
    policy_fingerprint: str,
    dataset_fingerprint: str,
    feature_set_version: str,
    control_policy: EvolutionControlPolicy | None = None,
) -> tuple[EvolutionDecision, EvolutionCandidate | None]:
    decision = evaluate_evolution_need(baseline, observation, policy=control_policy)
    if not decision.should_generate or decision.primary_trigger is None:
        return decision, None
    candidate = build_evolution_candidate(
        parent_release_id=parent_release_id,
        trigger=decision.primary_trigger,
        code_revision=code_revision,
        policy_fingerprint=policy_fingerprint,
        dataset_fingerprint=dataset_fingerprint,
        feature_set_version=feature_set_version,
        feature_stability=observation.feature_stability,
        adaptive_ensemble=observation.adaptive_ensemble,
        regime_mixture=observation.regime_mixture,
    )
    persist_evolution_candidate(path, candidate)
    return decision, candidate
