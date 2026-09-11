from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from statistics import NormalDist

from .research_experimentation import (
    ResearchAssignment,
    ResearchExperimentOutcome,
    ResearchTreatmentBundle,
    load_research_experiment_records,
    verify_research_experiment_ledger,
)


class CounterfactualLearningStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    INSUFFICIENT = "INSUFFICIENT"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class CrossFittedOutcomePrediction:
    assignment_id: str
    model_fingerprint: str
    predicted_values: Mapping[str, float]

    def __post_init__(self) -> None:
        if not self.assignment_id.strip() or not self.model_fingerprint.strip():
            raise ValueError("assignment_id and model_fingerprint are required")
        for treatment, value in self.predicted_values.items():
            if not treatment.strip() or not math.isfinite(value):
                raise ValueError("cross-fitted outcome predictions must be finite")


@dataclass(frozen=True, slots=True)
class CounterfactualLearningPolicy:
    minimum_resolved_assignments: int = 40
    minimum_resolution_rate: float = 0.80
    minimum_treatment_assignments: int = 8
    minimum_effective_sample_size: float = 6.0
    minimum_sequence_assignments: int = 6
    minimum_sequence_effective_sample_size: float = 4.0
    minimum_propensity: float = 0.05
    maximum_importance_weight: float = 20.0
    decay_half_life_days: float = 21.0
    regime_prior_strength: float = 20.0
    confidence_alpha: float = 0.05
    maximum_supported_treatments: int = 20

    def __post_init__(self) -> None:
        if min(
            self.minimum_resolved_assignments,
            self.minimum_treatment_assignments,
            self.minimum_sequence_assignments,
            self.maximum_supported_treatments,
        ) <= 0:
            raise ValueError("counterfactual sample thresholds must be positive")
        if not 0 < self.minimum_resolution_rate <= 1:
            raise ValueError("minimum_resolution_rate must be in (0,1]")
        if self.minimum_effective_sample_size <= 0 or self.minimum_sequence_effective_sample_size <= 0:
            raise ValueError("effective sample size thresholds must be positive")
        if not 0 < self.minimum_propensity <= 0.5:
            raise ValueError("minimum_propensity must be in (0,0.5]")
        if self.maximum_importance_weight < 1:
            raise ValueError("maximum_importance_weight must be >=1")
        if self.decay_half_life_days <= 0 or self.regime_prior_strength <= 0:
            raise ValueError("decay and regime prior strength must be positive")
        if not 0 < self.confidence_alpha < 0.5:
            raise ValueError("confidence_alpha must be in (0,0.5)")


@dataclass(frozen=True, slots=True)
class CounterfactualTreatmentEstimate:
    treatment_key: str
    cost_units: int
    assignments: int
    effective_sample_size: float
    global_dr_mean: float
    regime_dr_mean: float
    regime_shrinkage: float
    posterior_mean: float
    posterior_std_error: float
    simultaneous_lower_bound: float
    lower_value_per_cost: float


@dataclass(frozen=True, slots=True)
class CounterfactualSequenceEstimate:
    previous_treatment_key: str
    current_treatment_key: str
    assignments: int
    effective_sample_size: float
    conditional_dr_mean: float
    synergy_mean: float
    simultaneous_synergy_lower_bound: float


@dataclass(frozen=True, slots=True)
class CounterfactualLearningReport:
    status: CounterfactualLearningStatus
    current_regime: str
    reward_contract_hash: str
    assignments: int
    matured_assignments: int
    resolved_assignments: int
    resolution_rate: float
    outcome_model_fingerprint: str
    assignment_chain_hash: str
    treatment_estimates: tuple[CounterfactualTreatmentEstimate, ...]
    sequence_estimates: tuple[CounterfactualSequenceEstimate, ...]
    report_hash: str
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is CounterfactualLearningStatus.QUALIFIED


@dataclass(frozen=True, slots=True)
class _ResolvedRow:
    assignment: ResearchAssignment
    outcome: ResearchExperimentOutcome
    prediction: CrossFittedOutcomePrediction
    decay_weight: float


def _hash(payload: object) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode()).hexdigest()


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    total = sum(weights)
    if total <= 0:
        raise ValueError("counterfactual weights must have positive mass")
    return sum(value * weight for value, weight in zip(values, weights, strict=True)) / total


def _effective_sample_size(weights: list[float]) -> float:
    total = sum(weights)
    square = sum(weight * weight for weight in weights)
    return total * total / square if square > 0 else 0.0


def _weighted_variance(values: list[float], weights: list[float], mean: float) -> float:
    total = sum(weights)
    if total <= 0:
        return 0.0
    return sum(
        weight * (value - mean) ** 2
        for value, weight in zip(values, weights, strict=True)
    ) / total


def _treatment_universe(
    assignments: tuple[ResearchAssignment, ...],
) -> dict[str, ResearchTreatmentBundle]:
    treatments: dict[str, ResearchTreatmentBundle] = {}
    for assignment in assignments:
        for bundle, _ in assignment.distribution:
            existing = treatments.get(bundle.treatment_key)
            if existing is not None and existing != bundle:
                raise ValueError("research treatment key has inconsistent material")
            treatments[bundle.treatment_key] = bundle
    return treatments


def _pseudo_values(
    rows: list[_ResolvedRow],
    treatment_key: str,
    policy: CounterfactualLearningPolicy,
) -> tuple[list[float], list[float], list[float]]:
    pseudo: list[float] = []
    time_weights: list[float] = []
    correction_weights: list[float] = []
    for row in rows:
        predicted = row.prediction.predicted_values[treatment_key]
        chosen = row.assignment.chosen_treatment.treatment_key == treatment_key
        correction = 0.0
        correction_weight = 0.0
        if chosen:
            inverse = min(policy.maximum_importance_weight, 1.0 / row.assignment.chosen_propensity)
            correction = inverse * (row.outcome.realized_value - predicted)
            correction_weight = row.decay_weight * inverse
        pseudo.append(predicted + correction)
        time_weights.append(row.decay_weight)
        correction_weights.append(correction_weight)
    return pseudo, time_weights, correction_weights


def _estimate_treatment(
    treatment_key: str,
    bundle: ResearchTreatmentBundle,
    rows: list[_ResolvedRow],
    current_regime: str,
    z_value: float,
    policy: CounterfactualLearningPolicy,
) -> CounterfactualTreatmentEstimate | None:
    chosen_rows = [
        row for row in rows if row.assignment.chosen_treatment.treatment_key == treatment_key
    ]
    if len(chosen_rows) < policy.minimum_treatment_assignments:
        return None
    pseudo, weights, correction_weights = _pseudo_values(rows, treatment_key, policy)
    correction_ess = _effective_sample_size([weight for weight in correction_weights if weight > 0])
    if correction_ess < policy.minimum_effective_sample_size:
        return None
    global_mean = _weighted_mean(pseudo, weights)
    regime_rows = [row for row in rows if row.assignment.regime_key == current_regime]
    if regime_rows:
        regime_pseudo, regime_weights, regime_corrections = _pseudo_values(
            regime_rows,
            treatment_key,
            policy,
        )
        regime_mean = _weighted_mean(regime_pseudo, regime_weights)
        regime_ess = _effective_sample_size(
            [weight for weight in regime_corrections if weight > 0]
        )
        shrinkage = regime_ess / (regime_ess + policy.regime_prior_strength)
        variance_values = regime_pseudo
        variance_weights = regime_weights
    else:
        regime_mean = global_mean
        regime_ess = 0.0
        shrinkage = 0.0
        variance_values = pseudo
        variance_weights = weights
    posterior = shrinkage * regime_mean + (1.0 - shrinkage) * global_mean
    variance_mean = _weighted_mean(variance_values, variance_weights)
    variance = _weighted_variance(variance_values, variance_weights, variance_mean)
    time_ess = max(1.0, _effective_sample_size(variance_weights))
    std_error = math.sqrt(max(0.0, variance) / time_ess)
    lower = posterior - z_value * std_error
    per_cost = lower if bundle.cost_units == 0 else lower / bundle.cost_units
    return CounterfactualTreatmentEstimate(
        treatment_key=treatment_key,
        cost_units=bundle.cost_units,
        assignments=len(chosen_rows),
        effective_sample_size=correction_ess,
        global_dr_mean=global_mean,
        regime_dr_mean=regime_mean,
        regime_shrinkage=shrinkage,
        posterior_mean=posterior,
        posterior_std_error=std_error,
        simultaneous_lower_bound=lower,
        lower_value_per_cost=per_cost,
    )


def _sequence_estimates(
    rows: list[_ResolvedRow],
    assignments_by_id: Mapping[str, ResearchAssignment],
    base: Mapping[str, CounterfactualTreatmentEstimate],
    z_value: float,
    policy: CounterfactualLearningPolicy,
) -> tuple[CounterfactualSequenceEstimate, ...]:
    by_previous: dict[str, list[_ResolvedRow]] = {}
    for row in rows:
        previous_id = row.assignment.previous_assignment_id
        if not previous_id:
            continue
        previous = assignments_by_id.get(previous_id)
        if previous is None:
            continue
        key = previous.chosen_treatment.treatment_key
        by_previous.setdefault(key, []).append(row)
    output: list[CounterfactualSequenceEstimate] = []
    for previous_key in sorted(by_previous):
        members = by_previous[previous_key]
        for current_key, base_estimate in sorted(base.items()):
            observed = [
                row for row in members
                if row.assignment.chosen_treatment.treatment_key == current_key
            ]
            if len(observed) < policy.minimum_sequence_assignments:
                continue
            pseudo, weights, corrections = _pseudo_values(members, current_key, policy)
            ess = _effective_sample_size([weight for weight in corrections if weight > 0])
            if ess < policy.minimum_sequence_effective_sample_size:
                continue
            conditional = _weighted_mean(pseudo, weights)
            variance = _weighted_variance(pseudo, weights, conditional)
            std_error = math.sqrt(max(0.0, variance) / max(1.0, _effective_sample_size(weights)))
            synergy = conditional - base_estimate.posterior_mean
            output.append(
                CounterfactualSequenceEstimate(
                    previous_treatment_key=previous_key,
                    current_treatment_key=current_key,
                    assignments=len(observed),
                    effective_sample_size=ess,
                    conditional_dr_mean=conditional,
                    synergy_mean=synergy,
                    simultaneous_synergy_lower_bound=synergy - z_value * std_error,
                )
            )
    return tuple(output)


def evaluate_counterfactual_learning_efficiency(
    path: str,
    *,
    predictions: tuple[CrossFittedOutcomePrediction, ...],
    reward_contract_hash: str,
    current_regime: str,
    as_of_ts_utc: datetime,
    policy: CounterfactualLearningPolicy | None = None,
) -> CounterfactualLearningReport:
    """Estimate research-treatment value with doubly robust, support-aware attribution."""
    policy = policy or CounterfactualLearningPolicy()
    if not reward_contract_hash.strip() or not current_regime.strip():
        raise ValueError("reward_contract_hash and current_regime are required")
    if as_of_ts_utc.tzinfo is None or as_of_ts_utc.utcoffset() is None:
        raise ValueError("as_of_ts_utc must be timezone-aware")
    as_of = as_of_ts_utc.astimezone(UTC)
    verification = verify_research_experiment_ledger(path)
    if not verification.valid:
        return _report(
            status=CounterfactualLearningStatus.BLOCKED,
            current_regime=current_regime,
            reward_contract_hash=reward_contract_hash,
            assignments=verification.assignments,
            matured=0,
            resolved=0,
            resolution_rate=0.0,
            model_fingerprint="",
            chain_hash=verification.chain_hash,
            treatment_estimates=(),
            sequence_estimates=(),
            failures=verification.failures,
        )
    assignments, outcomes = load_research_experiment_records(path)
    relevant = tuple(
        item for item in assignments
        if item.reward_contract_hash == reward_contract_hash and item.assigned_ts_utc <= as_of
    )
    treatments = _treatment_universe(relevant)
    failures: list[str] = []
    if len(treatments) > policy.maximum_supported_treatments:
        failures.append("counterfactual_treatment_universe_too_large")
    matured = tuple(item for item in relevant if item.maturity_ts_utc <= as_of)
    outcome_by_assignment = {
        item.assignment_id: item for item in outcomes
        if item.reward_contract_hash == reward_contract_hash and item.realized_ts_utc <= as_of
    }
    resolved_assignments = tuple(
        item for item in matured if item.assignment_id in outcome_by_assignment
    )
    resolution_rate = len(resolved_assignments) / len(matured) if matured else 0.0
    if len(resolved_assignments) < policy.minimum_resolved_assignments:
        failures.append("insufficient_resolved_counterfactual_assignments")
    if matured and resolution_rate < policy.minimum_resolution_rate:
        failures.append("counterfactual_outcome_resolution_rate_below_floor")
    prediction_by_id = {item.assignment_id: item for item in predictions}
    if len(prediction_by_id) != len(predictions):
        raise ValueError("cross-fitted prediction assignment ids must be unique")
    model_fingerprints = {item.model_fingerprint for item in predictions}
    if len(model_fingerprints) > 1:
        raise ValueError("counterfactual outcome model fingerprint must be stable")
    model_fingerprint = next(iter(model_fingerprints), "")
    treatment_keys = tuple(sorted(treatments))
    rows: list[_ResolvedRow] = []
    for assignment in resolved_assignments:
        if assignment.chosen_propensity < policy.minimum_propensity:
            failures.append("counterfactual_logged_propensity_below_floor")
            continue
        prediction = prediction_by_id.get(assignment.assignment_id)
        if prediction is None:
            failures.append("counterfactual_cross_fitted_prediction_missing")
            continue
        missing = [key for key in treatment_keys if key not in prediction.predicted_values]
        if missing:
            failures.append("counterfactual_prediction_treatment_support_missing")
            continue
        age_days = max(0.0, (as_of - assignment.assigned_ts_utc).total_seconds() / 86400.0)
        decay = 0.5 ** (age_days / policy.decay_half_life_days)
        rows.append(
            _ResolvedRow(
                assignment=assignment,
                outcome=outcome_by_assignment[assignment.assignment_id],
                prediction=prediction,
                decay_weight=decay,
            )
        )
    if failures and any(item.startswith("counterfactual_") for item in failures):
        hard = {
            "counterfactual_treatment_universe_too_large",
            "counterfactual_logged_propensity_below_floor",
            "counterfactual_cross_fitted_prediction_missing",
            "counterfactual_prediction_treatment_support_missing",
        }
        if any(item in hard for item in failures):
            return _report(
                status=CounterfactualLearningStatus.BLOCKED,
                current_regime=current_regime,
                reward_contract_hash=reward_contract_hash,
                assignments=len(relevant),
                matured=len(matured),
                resolved=len(rows),
                resolution_rate=resolution_rate,
                model_fingerprint=model_fingerprint,
                chain_hash=verification.chain_hash,
                treatment_estimates=(),
                sequence_estimates=(),
                failures=tuple(dict.fromkeys(failures)),
            )
    if failures:
        return _report(
            status=CounterfactualLearningStatus.INSUFFICIENT,
            current_regime=current_regime,
            reward_contract_hash=reward_contract_hash,
            assignments=len(relevant),
            matured=len(matured),
            resolved=len(rows),
            resolution_rate=resolution_rate,
            model_fingerprint=model_fingerprint,
            chain_hash=verification.chain_hash,
            treatment_estimates=(),
            sequence_estimates=(),
            failures=tuple(dict.fromkeys(failures)),
        )
    adjusted_alpha = policy.confidence_alpha / max(1, len(treatment_keys))
    z_value = NormalDist().inv_cdf(1.0 - adjusted_alpha / 2.0)
    estimates = tuple(
        item
        for key in treatment_keys
        if (
            item := _estimate_treatment(
                key,
                treatments[key],
                rows,
                current_regime,
                z_value,
                policy,
            )
        ) is not None
    )
    if len(estimates) < 2:
        failures.append("insufficient_supported_counterfactual_treatments")
        status = CounterfactualLearningStatus.INSUFFICIENT
        sequences: tuple[CounterfactualSequenceEstimate, ...] = ()
    else:
        status = CounterfactualLearningStatus.QUALIFIED
        base = {item.treatment_key: item for item in estimates}
        by_id = {item.assignment_id: item for item in assignments}
        sequences = _sequence_estimates(rows, by_id, base, z_value, policy)
    return _report(
        status=status,
        current_regime=current_regime,
        reward_contract_hash=reward_contract_hash,
        assignments=len(relevant),
        matured=len(matured),
        resolved=len(rows),
        resolution_rate=resolution_rate,
        model_fingerprint=model_fingerprint,
        chain_hash=verification.chain_hash,
        treatment_estimates=estimates,
        sequence_estimates=sequences,
        failures=tuple(failures),
    )


def _report(
    *,
    status: CounterfactualLearningStatus,
    current_regime: str,
    reward_contract_hash: str,
    assignments: int,
    matured: int,
    resolved: int,
    resolution_rate: float,
    model_fingerprint: str,
    chain_hash: str,
    treatment_estimates: tuple[CounterfactualTreatmentEstimate, ...],
    sequence_estimates: tuple[CounterfactualSequenceEstimate, ...],
    failures: tuple[str, ...],
) -> CounterfactualLearningReport:
    material = {
        "version": "counterfactual-learning-efficiency-v1",
        "status": status.value,
        "current_regime": current_regime,
        "reward_contract_hash": reward_contract_hash,
        "assignments": assignments,
        "matured": matured,
        "resolved": resolved,
        "resolution_rate": round(resolution_rate, 12),
        "outcome_model_fingerprint": model_fingerprint,
        "assignment_chain_hash": chain_hash,
        "treatments": [asdict(item) for item in treatment_estimates],
        "sequences": [asdict(item) for item in sequence_estimates],
        "failures": failures,
    }
    return CounterfactualLearningReport(
        status=status,
        current_regime=current_regime,
        reward_contract_hash=reward_contract_hash,
        assignments=assignments,
        matured_assignments=matured,
        resolved_assignments=resolved,
        resolution_rate=resolution_rate,
        outcome_model_fingerprint=model_fingerprint,
        assignment_chain_hash=chain_hash,
        treatment_estimates=treatment_estimates,
        sequence_estimates=sequence_estimates,
        report_hash=_hash(material),
        failures=failures,
    )
