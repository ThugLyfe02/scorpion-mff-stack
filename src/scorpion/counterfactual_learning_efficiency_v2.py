from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import NormalDist

from .crossfit_provenance import (
    CertifiedCrossFittedOutcomePrediction,
    CrossFitProvenanceManifest,
    CrossFittedCensoringPrediction,
    require_prediction_heldout,
    verify_crossfit_manifest,
)
from .research_experimentation import (
    ResearchAssignment,
    ResearchExperimentOutcome,
    ResearchTreatmentBundle,
    load_research_experiment_records,
    verify_research_experiment_ledger,
)
from .research_randomization_attestation import (
    AssignmentRandomizationAttestation,
    load_assignment_attestations,
    verify_randomization_integrity,
)


@dataclass(frozen=True, slots=True)
class CausalLearningPolicyV2:
    minimum_matured_assignments: int = 40
    minimum_treatment_assignments: int = 8
    minimum_treatment_resolution_rate: float = 0.50
    maximum_resolution_rate_gap: float = 0.35
    minimum_censoring_probability: float = 0.05
    maximum_joint_importance_weight: float = 30.0
    minimum_effective_sample_size: float = 6.0
    minimum_clusters: int = 5
    decay_half_life_days: float = 21.0
    regime_prior_strength: float = 20.0
    confidence_alpha: float = 0.05
    maximum_supported_treatments: int = 20

    def __post_init__(self) -> None:
        if min(
            self.minimum_matured_assignments,
            self.minimum_treatment_assignments,
            self.minimum_clusters,
            self.maximum_supported_treatments,
        ) <= 0:
            raise ValueError("causal learning integer thresholds must be positive")
        for name in (
            "minimum_treatment_resolution_rate",
            "maximum_resolution_rate_gap",
            "minimum_censoring_probability",
            "confidence_alpha",
        ):
            value = getattr(self, name)
            if not 0 < value < 1:
                raise ValueError(f"{name} must be in (0,1)")
        if self.maximum_joint_importance_weight < 1:
            raise ValueError("maximum_joint_importance_weight must be >=1")
        if self.minimum_effective_sample_size <= 0:
            raise ValueError("minimum_effective_sample_size must be positive")
        if self.decay_half_life_days <= 0 or self.regime_prior_strength <= 0:
            raise ValueError("decay and regime prior strength must be positive")


@dataclass(frozen=True, slots=True)
class TreatmentResolutionEvidence:
    treatment_key: str
    matured: int
    resolved: int
    resolution_rate: float


@dataclass(frozen=True, slots=True)
class CausalTreatmentEstimateV2:
    treatment_key: str
    cost_units: int
    chosen_assignments: int
    resolved_assignments: int
    clusters: int
    effective_sample_size: float
    global_aipw_mean: float
    regime_aipw_mean: float
    regime_shrinkage: float
    posterior_mean: float
    cluster_robust_std_error: float
    simultaneous_lower_bound: float
    lower_value_per_cost: float


@dataclass(frozen=True, slots=True)
class CausalLearningReportV2:
    qualified: bool
    current_regime: str
    reward_contract_hash: str
    matured_assignments: int
    attested_assignments: int
    supported_treatments: int
    resolution_evidence: tuple[TreatmentResolutionEvidence, ...]
    maximum_resolution_rate_gap: float
    outcome_manifest_hash: str
    censoring_manifest_hash: str
    experiment_chain_hash: str
    randomization_chain_hash: str
    treatment_estimates: tuple[CausalTreatmentEstimateV2, ...]
    report_hash: str
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Row:
    assignment: ResearchAssignment
    attestation: AssignmentRandomizationAttestation
    outcome: ResearchExperimentOutcome | None
    outcome_prediction: CertifiedCrossFittedOutcomePrediction
    censoring_prediction: CrossFittedCensoringPrediction
    decay_weight: float


def _hash(payload: object) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode()).hexdigest()


def _effective_sample_size(weights: list[float]) -> float:
    total = sum(weights)
    squares = sum(item * item for item in weights)
    return total * total / squares if squares > 0 else 0.0


def _treatments(assignments: tuple[ResearchAssignment, ...]) -> dict[str, ResearchTreatmentBundle]:
    output: dict[str, ResearchTreatmentBundle] = {}
    for assignment in assignments:
        for bundle, _ in assignment.distribution:
            previous = output.get(bundle.treatment_key)
            if previous is not None and previous != bundle:
                raise ValueError("treatment key has inconsistent bundle material")
            output[bundle.treatment_key] = bundle
    return output


def _validate_predictions(
    *,
    matured: tuple[ResearchAssignment, ...],
    outcome_predictions: tuple[CertifiedCrossFittedOutcomePrediction, ...],
    outcome_manifest: CrossFitProvenanceManifest,
    censoring_predictions: tuple[CrossFittedCensoringPrediction, ...],
    censoring_manifest: CrossFitProvenanceManifest,
) -> tuple[
    dict[str, CertifiedCrossFittedOutcomePrediction],
    dict[str, CrossFittedCensoringPrediction],
    tuple[str, ...],
]:
    failures: list[str] = []
    failures.extend(f"outcome:{item}" for item in verify_crossfit_manifest(outcome_manifest))
    failures.extend(f"censoring:{item}" for item in verify_crossfit_manifest(censoring_manifest))
    outcome_by_id = {item.assignment_id: item for item in outcome_predictions}
    censoring_by_id = {item.assignment_id: item for item in censoring_predictions}
    if len(outcome_by_id) != len(outcome_predictions):
        raise ValueError("outcome prediction assignment ids must be unique")
    if len(censoring_by_id) != len(censoring_predictions):
        raise ValueError("censoring prediction assignment ids must be unique")
    for assignment in matured:
        outcome_prediction = outcome_by_id.get(assignment.assignment_id)
        censoring_prediction = censoring_by_id.get(assignment.assignment_id)
        if outcome_prediction is None:
            failures.append("outcome_crossfit_prediction_missing")
        else:
            try:
                require_prediction_heldout(
                    outcome_manifest,
                    assignment_id=assignment.assignment_id,
                    fold_id=outcome_prediction.fold_id,
                    model_fingerprint=outcome_prediction.model_fingerprint,
                    manifest_hash=outcome_prediction.manifest_hash,
                )
            except ValueError:
                failures.append("outcome_crossfit_holdout_proof_failed")
        if censoring_prediction is None:
            failures.append("censoring_crossfit_prediction_missing")
        else:
            try:
                require_prediction_heldout(
                    censoring_manifest,
                    assignment_id=assignment.assignment_id,
                    fold_id=censoring_prediction.fold_id,
                    model_fingerprint=censoring_prediction.model_fingerprint,
                    manifest_hash=censoring_prediction.manifest_hash,
                )
            except ValueError:
                failures.append("censoring_crossfit_holdout_proof_failed")
    return outcome_by_id, censoring_by_id, tuple(dict.fromkeys(failures))


def _resolution_evidence(
    matured: tuple[ResearchAssignment, ...],
    outcomes: dict[str, ResearchExperimentOutcome],
) -> tuple[TreatmentResolutionEvidence, ...]:
    counts: dict[str, list[int]] = {}
    for assignment in matured:
        key = assignment.chosen_treatment.treatment_key
        bucket = counts.setdefault(key, [0, 0])
        bucket[0] += 1
        if assignment.assignment_id in outcomes:
            bucket[1] += 1
    return tuple(
        TreatmentResolutionEvidence(
            treatment_key=key,
            matured=counts[key][0],
            resolved=counts[key][1],
            resolution_rate=counts[key][1] / counts[key][0],
        )
        for key in sorted(counts)
    )


def _aipw_values(
    rows: list[_Row],
    treatment_key: str,
    policy: CausalLearningPolicyV2,
) -> tuple[list[float], list[float], list[float]]:
    values: list[float] = []
    time_weights: list[float] = []
    correction_weights: list[float] = []
    for row in rows:
        predicted = row.outcome_prediction.predicted_values[treatment_key]
        correction = 0.0
        correction_weight = 0.0
        chosen = row.assignment.chosen_treatment.treatment_key == treatment_key
        if chosen and row.outcome is not None:
            behavior = row.assignment.chosen_propensity
            censoring = row.censoring_prediction.resolution_probability
            joint = min(
                policy.maximum_joint_importance_weight,
                1.0 / (behavior * censoring),
            )
            correction = joint * (row.outcome.realized_value - predicted)
            correction_weight = row.decay_weight * joint
        values.append(predicted + correction)
        time_weights.append(row.decay_weight)
        correction_weights.append(correction_weight)
    return values, time_weights, correction_weights


def _cluster_mean_and_se(
    rows: list[_Row],
    values: list[float],
    weights: list[float],
) -> tuple[float, float, int]:
    by_cluster: dict[str, list[tuple[float, float]]] = {}
    for row, value, weight in zip(rows, values, weights, strict=True):
        by_cluster.setdefault(row.attestation.cluster_id, []).append((value, weight))
    cluster_means: list[float] = []
    for members in by_cluster.values():
        total_weight = sum(weight for _, weight in members)
        if total_weight <= 0:
            continue
        cluster_means.append(
            sum(value * weight for value, weight in members) / total_weight
        )
    if not cluster_means:
        return 0.0, math.inf, 0
    mean = statistics.fmean(cluster_means)
    if len(cluster_means) == 1:
        return mean, math.inf, 1
    std_error = math.sqrt(statistics.variance(cluster_means) / len(cluster_means))
    return mean, std_error, len(cluster_means)


def _estimate(
    *,
    treatment_key: str,
    bundle: ResearchTreatmentBundle,
    rows: list[_Row],
    current_regime: str,
    z_value: float,
    policy: CausalLearningPolicyV2,
) -> CausalTreatmentEstimateV2 | None:
    chosen = [row for row in rows if row.assignment.chosen_treatment.treatment_key == treatment_key]
    resolved = [row for row in chosen if row.outcome is not None]
    if len(chosen) < policy.minimum_treatment_assignments:
        return None
    values, weights, correction_weights = _aipw_values(rows, treatment_key, policy)
    positive_correction = [item for item in correction_weights if item > 0]
    ess = _effective_sample_size(positive_correction)
    if ess < policy.minimum_effective_sample_size:
        return None
    global_mean, global_se, clusters = _cluster_mean_and_se(rows, values, weights)
    if clusters < policy.minimum_clusters or not math.isfinite(global_se):
        return None
    regime_rows = [row for row in rows if row.assignment.regime_key == current_regime]
    if regime_rows:
        regime_values, regime_weights, regime_corrections = _aipw_values(
            regime_rows,
            treatment_key,
            policy,
        )
        regime_mean, regime_se, regime_clusters = _cluster_mean_and_se(
            regime_rows,
            regime_values,
            regime_weights,
        )
        regime_ess = _effective_sample_size([item for item in regime_corrections if item > 0])
        shrinkage = regime_ess / (regime_ess + policy.regime_prior_strength)
        if regime_clusters < 2 or not math.isfinite(regime_se):
            shrinkage = 0.0
            regime_mean = global_mean
            regime_se = global_se
    else:
        regime_mean = global_mean
        regime_se = global_se
        shrinkage = 0.0
    posterior = shrinkage * regime_mean + (1.0 - shrinkage) * global_mean
    posterior_se = math.sqrt(
        (shrinkage * regime_se) ** 2 + ((1.0 - shrinkage) * global_se) ** 2
    )
    lower = posterior - z_value * posterior_se
    per_cost = lower if bundle.cost_units == 0 else lower / bundle.cost_units
    return CausalTreatmentEstimateV2(
        treatment_key=treatment_key,
        cost_units=bundle.cost_units,
        chosen_assignments=len(chosen),
        resolved_assignments=len(resolved),
        clusters=clusters,
        effective_sample_size=ess,
        global_aipw_mean=global_mean,
        regime_aipw_mean=regime_mean,
        regime_shrinkage=shrinkage,
        posterior_mean=posterior,
        cluster_robust_std_error=posterior_se,
        simultaneous_lower_bound=lower,
        lower_value_per_cost=per_cost,
    )


def evaluate_causal_learning_efficiency_v2(
    path: str,
    *,
    outcome_predictions: tuple[CertifiedCrossFittedOutcomePrediction, ...],
    outcome_manifest: CrossFitProvenanceManifest,
    censoring_predictions: tuple[CrossFittedCensoringPrediction, ...],
    censoring_manifest: CrossFitProvenanceManifest,
    reward_contract_hash: str,
    current_regime: str,
    as_of_ts_utc: datetime,
    policy: CausalLearningPolicyV2 | None = None,
) -> CausalLearningReportV2:
    policy = policy or CausalLearningPolicyV2()
    if not reward_contract_hash.strip() or not current_regime.strip():
        raise ValueError("reward_contract_hash and current_regime are required")
    if as_of_ts_utc.tzinfo is None or as_of_ts_utc.utcoffset() is None:
        raise ValueError("as_of_ts_utc must be timezone-aware")
    as_of = as_of_ts_utc.astimezone(UTC)
    experiment_verification = verify_research_experiment_ledger(path)
    randomization_verification = verify_randomization_integrity(path)
    failures: list[str] = []
    if not experiment_verification.valid:
        failures.extend(f"experiment:{item}" for item in experiment_verification.failures)
    if not randomization_verification.valid:
        failures.extend(f"randomization:{item}" for item in randomization_verification.failures)
    assignments, outcomes = load_research_experiment_records(path)
    relevant = tuple(
        item for item in assignments
        if item.reward_contract_hash == reward_contract_hash and item.assigned_ts_utc <= as_of
    )
    matured = tuple(item for item in relevant if item.maturity_ts_utc <= as_of)
    if len(matured) < policy.minimum_matured_assignments:
        failures.append("insufficient_matured_assignments")
    treatments = _treatments(relevant)
    if len(treatments) > policy.maximum_supported_treatments:
        failures.append("causal_treatment_universe_too_large")
    attestations = load_assignment_attestations(path)
    missing_attestation = [
        item.assignment_id for item in matured if item.assignment_id not in attestations
    ]
    if missing_attestation:
        failures.append("unattested_randomization_assignment_present")
    outcome_by_assignment = {
        item.assignment_id: item for item in outcomes
        if item.reward_contract_hash == reward_contract_hash and item.realized_ts_utc <= as_of
    }
    resolution = _resolution_evidence(matured, outcome_by_assignment)
    rates = [item.resolution_rate for item in resolution]
    maximum_gap = max(rates) - min(rates) if rates else 0.0
    if any(item.resolution_rate < policy.minimum_treatment_resolution_rate for item in resolution):
        failures.append("treatment_specific_resolution_rate_below_floor")
    if maximum_gap > policy.maximum_resolution_rate_gap:
        failures.append("differential_censoring_gap_above_ceiling")
    outcome_by_id, censoring_by_id, provenance_failures = _validate_predictions(
        matured=matured,
        outcome_predictions=outcome_predictions,
        outcome_manifest=outcome_manifest,
        censoring_predictions=censoring_predictions,
        censoring_manifest=censoring_manifest,
    )
    failures.extend(provenance_failures)
    for assignment in matured:
        censoring = censoring_by_id.get(assignment.assignment_id)
        if (
            censoring is not None
            and censoring.resolution_probability < policy.minimum_censoring_probability
        ):
            failures.append("censoring_probability_below_floor")
        prediction = outcome_by_id.get(assignment.assignment_id)
        if prediction is not None:
            missing_keys = set(treatments) - set(prediction.predicted_values)
            if missing_keys:
                failures.append("outcome_prediction_treatment_support_missing")
    hard_failures = tuple(dict.fromkeys(failures))
    if hard_failures:
        return _report(
            qualified=False,
            current_regime=current_regime,
            reward_contract_hash=reward_contract_hash,
            matured=len(matured),
            attested=len(matured) - len(missing_attestation),
            resolution=resolution,
            maximum_gap=maximum_gap,
            outcome_manifest=outcome_manifest,
            censoring_manifest=censoring_manifest,
            experiment_chain=experiment_verification.chain_hash,
            randomization_chain=randomization_verification.chain_hash,
            estimates=(),
            failures=hard_failures,
        )
    rows = [
        _Row(
            assignment=assignment,
            attestation=attestations[assignment.assignment_id],
            outcome=outcome_by_assignment.get(assignment.assignment_id),
            outcome_prediction=outcome_by_id[assignment.assignment_id],
            censoring_prediction=censoring_by_id[assignment.assignment_id],
            decay_weight=0.5 ** (
                max(0.0, (as_of - assignment.assigned_ts_utc).total_seconds() / 86400.0)
                / policy.decay_half_life_days
            ),
        )
        for assignment in matured
    ]
    adjusted_alpha = policy.confidence_alpha / max(1, len(treatments))
    z_value = NormalDist().inv_cdf(1.0 - adjusted_alpha / 2.0)
    estimates = tuple(
        estimate
        for key in sorted(treatments)
        if (
            estimate := _estimate(
                treatment_key=key,
                bundle=treatments[key],
                rows=rows,
                current_regime=current_regime,
                z_value=z_value,
                policy=policy,
            )
        ) is not None
    )
    if len(estimates) < 2:
        failures.append("insufficient_supported_causal_treatments")
    return _report(
        qualified=not failures,
        current_regime=current_regime,
        reward_contract_hash=reward_contract_hash,
        matured=len(matured),
        attested=len(matured),
        resolution=resolution,
        maximum_gap=maximum_gap,
        outcome_manifest=outcome_manifest,
        censoring_manifest=censoring_manifest,
        experiment_chain=experiment_verification.chain_hash,
        randomization_chain=randomization_verification.chain_hash,
        estimates=estimates,
        failures=tuple(failures),
    )


def _report(
    *,
    qualified: bool,
    current_regime: str,
    reward_contract_hash: str,
    matured: int,
    attested: int,
    resolution: tuple[TreatmentResolutionEvidence, ...],
    maximum_gap: float,
    outcome_manifest: CrossFitProvenanceManifest,
    censoring_manifest: CrossFitProvenanceManifest,
    experiment_chain: str,
    randomization_chain: str,
    estimates: tuple[CausalTreatmentEstimateV2, ...],
    failures: tuple[str, ...],
) -> CausalLearningReportV2:
    material = {
        "version": "causal-learning-efficiency-v2",
        "qualified": qualified,
        "current_regime": current_regime,
        "reward_contract_hash": reward_contract_hash,
        "matured_assignments": matured,
        "attested_assignments": attested,
        "resolution_evidence": [asdict(item) for item in resolution],
        "maximum_resolution_rate_gap": round(maximum_gap, 12),
        "outcome_manifest_hash": outcome_manifest.manifest_hash,
        "censoring_manifest_hash": censoring_manifest.manifest_hash,
        "experiment_chain_hash": experiment_chain,
        "randomization_chain_hash": randomization_chain,
        "treatment_estimates": [asdict(item) for item in estimates],
        "failures": failures,
    }
    return CausalLearningReportV2(
        qualified=qualified,
        current_regime=current_regime,
        reward_contract_hash=reward_contract_hash,
        matured_assignments=matured,
        attested_assignments=attested,
        supported_treatments=len(estimates),
        resolution_evidence=resolution,
        maximum_resolution_rate_gap=maximum_gap,
        outcome_manifest_hash=outcome_manifest.manifest_hash,
        censoring_manifest_hash=censoring_manifest.manifest_hash,
        experiment_chain_hash=experiment_chain,
        randomization_chain_hash=randomization_chain,
        treatment_estimates=estimates,
        report_hash=_hash(material),
        failures=failures,
    )
