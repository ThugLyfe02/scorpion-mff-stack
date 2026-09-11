from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import NormalDist

from .crossfit_provenance import (
    CrossFitProvenanceManifest,
    require_prediction_heldout,
    verify_crossfit_manifest,
)
from .research_experimentation import (
    ResearchAssignment,
    ResearchExperimentOutcome,
    load_research_experiment_records,
    verify_research_experiment_ledger,
)
from .research_randomization_attestation import (
    load_assignment_attestations,
    verify_randomization_integrity,
)


@dataclass(frozen=True, slots=True)
class SequentialTargetPolicyDecision:
    assignment_id: str
    probabilities: dict[str, float]

    def __post_init__(self) -> None:
        if not self.assignment_id.strip():
            raise ValueError("assignment_id is required")
        if not self.probabilities:
            raise ValueError("target policy probabilities cannot be empty")
        total = 0.0
        for treatment, probability in self.probabilities.items():
            if not treatment.strip() or not math.isfinite(probability) or probability < 0:
                raise ValueError("target policy probabilities must be finite and non-negative")
            total += probability
        if abs(total - 1.0) > 1e-9:
            raise ValueError("target policy probabilities must sum to one")


@dataclass(frozen=True, slots=True)
class CertifiedSequentialQPrediction:
    assignment_id: str
    model_fingerprint: str
    manifest_hash: str
    fold_id: str
    q_values: dict[str, float]
    state_value: float
    next_state_value: float

    def __post_init__(self) -> None:
        for name in ("assignment_id", "model_fingerprint", "manifest_hash", "fold_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if not self.q_values:
            raise ValueError("q_values cannot be empty")
        numeric = tuple(self.q_values.values()) + (self.state_value, self.next_state_value)
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("sequential Q predictions must be finite")


@dataclass(frozen=True, slots=True)
class SequentialOPEPolicy:
    minimum_complete_episodes: int = 12
    minimum_episode_completion_rate: float = 0.75
    minimum_effective_sample_size: float = 6.0
    minimum_clusters: int = 5
    maximum_cumulative_importance_weight: float = 30.0
    discount: float = 1.0
    confidence_alpha: float = 0.05

    def __post_init__(self) -> None:
        if self.minimum_complete_episodes <= 0 or self.minimum_clusters <= 0:
            raise ValueError("sequential OPE sample thresholds must be positive")
        if not 0 < self.minimum_episode_completion_rate <= 1:
            raise ValueError("minimum_episode_completion_rate must be in (0,1]")
        if self.minimum_effective_sample_size <= 0:
            raise ValueError("minimum_effective_sample_size must be positive")
        if self.maximum_cumulative_importance_weight < 1:
            raise ValueError("maximum_cumulative_importance_weight must be >=1")
        if not 0 < self.discount <= 1:
            raise ValueError("discount must be in (0,1]")
        if not 0 < self.confidence_alpha < 0.5:
            raise ValueError("confidence_alpha must be in (0,0.5)")


@dataclass(frozen=True, slots=True)
class SequentialEpisodeEvidence:
    episode_id: str
    steps: int
    cluster_id: str
    cumulative_importance_weight: float
    dr_value: float


@dataclass(frozen=True, slots=True)
class SequentialPolicyValueReport:
    qualified: bool
    reward_contract_hash: str
    target_policy_hash: str
    q_manifest_hash: str
    episodes: int
    complete_episodes: int
    completion_rate: float
    effective_sample_size: float
    clusters: int
    dr_policy_value: float
    cluster_robust_std_error: float
    lower_confidence_bound: float
    experiment_chain_hash: str
    randomization_chain_hash: str
    episode_evidence: tuple[SequentialEpisodeEvidence, ...]
    report_hash: str
    failures: tuple[str, ...]


def _hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _effective_sample_size(weights: list[float]) -> float:
    total = sum(weights)
    squares = sum(weight * weight for weight in weights)
    return total * total / squares if squares > 0 else 0.0


def _cluster_robust(
    evidence: tuple[SequentialEpisodeEvidence, ...],
) -> tuple[float, float, int]:
    by_cluster: dict[str, list[float]] = {}
    for item in evidence:
        by_cluster.setdefault(item.cluster_id, []).append(item.dr_value)
    cluster_means = [statistics.fmean(values) for values in by_cluster.values()]
    if not cluster_means:
        return 0.0, math.inf, 0
    mean = statistics.fmean(cluster_means)
    if len(cluster_means) == 1:
        return mean, math.inf, 1
    std_error = math.sqrt(statistics.variance(cluster_means) / len(cluster_means))
    return mean, std_error, len(cluster_means)


def evaluate_sequential_research_policy(
    path: str,
    *,
    target_policy: tuple[SequentialTargetPolicyDecision, ...],
    q_predictions: tuple[CertifiedSequentialQPrediction, ...],
    q_manifest: CrossFitProvenanceManifest,
    reward_contract_hash: str,
    as_of_ts_utc: datetime,
    policy: SequentialOPEPolicy | None = None,
) -> SequentialPolicyValueReport:
    policy = policy or SequentialOPEPolicy()
    if not reward_contract_hash.strip():
        raise ValueError("reward_contract_hash is required")
    if as_of_ts_utc.tzinfo is None or as_of_ts_utc.utcoffset() is None:
        raise ValueError("as_of_ts_utc must be timezone-aware")
    as_of = as_of_ts_utc.astimezone(UTC)
    experiment_integrity = verify_research_experiment_ledger(path)
    randomization_integrity = verify_randomization_integrity(path)
    manifest_failures = verify_crossfit_manifest(q_manifest)
    failures: list[str] = []
    if not experiment_integrity.valid:
        failures.extend(f"experiment:{item}" for item in experiment_integrity.failures)
    if not randomization_integrity.valid:
        failures.extend(f"randomization:{item}" for item in randomization_integrity.failures)
    if manifest_failures:
        failures.extend(f"q_manifest:{item}" for item in manifest_failures)
    assignments, outcomes = load_research_experiment_records(path)
    attestations = load_assignment_attestations(path)
    relevant = tuple(
        item for item in assignments
        if item.reward_contract_hash == reward_contract_hash and item.assigned_ts_utc <= as_of
    )
    by_episode: dict[str, list[ResearchAssignment]] = {}
    for assignment in relevant:
        by_episode.setdefault(assignment.episode_id, []).append(assignment)
    outcome_by_assignment: dict[str, ResearchExperimentOutcome] = {
        outcome.assignment_id: outcome for outcome in outcomes
        if outcome.reward_contract_hash == reward_contract_hash and outcome.realized_ts_utc <= as_of
    }
    target_by_id = {item.assignment_id: item for item in target_policy}
    q_by_id = {item.assignment_id: item for item in q_predictions}
    if len(target_by_id) != len(target_policy):
        raise ValueError("target policy assignment ids must be unique")
    if len(q_by_id) != len(q_predictions):
        raise ValueError("Q prediction assignment ids must be unique")

    complete_episode_ids: list[str] = []
    for episode_id, members in by_episode.items():
        mature = all(item.maturity_ts_utc <= as_of for item in members)
        resolved = all(item.assignment_id in outcome_by_assignment for item in members)
        attested = all(item.assignment_id in attestations for item in members)
        predicted = all(item.assignment_id in q_by_id for item in members)
        targeted = all(item.assignment_id in target_by_id for item in members)
        if mature and resolved and attested and predicted and targeted:
            complete_episode_ids.append(episode_id)
    episodes = len(by_episode)
    completion_rate = len(complete_episode_ids) / episodes if episodes else 0.0
    if len(complete_episode_ids) < policy.minimum_complete_episodes:
        failures.append("insufficient_complete_sequential_episodes")
    if episodes and completion_rate < policy.minimum_episode_completion_rate:
        failures.append("sequential_episode_completion_rate_below_floor")

    episode_evidence: list[SequentialEpisodeEvidence] = []
    for episode_id in sorted(complete_episode_ids):
        members = sorted(by_episode[episode_id], key=lambda item: item.step_index)
        cluster_ids = {attestations[item.assignment_id].cluster_id for item in members}
        if len(cluster_ids) != 1:
            failures.append("sequential_episode_cross_cluster_interference")
            continue
        cumulative_ratio = 1.0
        first_prediction = q_by_id[members[0].assignment_id]
        value = first_prediction.state_value
        discount_power = 1.0
        for assignment in members:
            prediction = q_by_id[assignment.assignment_id]
            try:
                require_prediction_heldout(
                    q_manifest,
                    assignment_id=assignment.assignment_id,
                    fold_id=prediction.fold_id,
                    model_fingerprint=prediction.model_fingerprint,
                    manifest_hash=prediction.manifest_hash,
                )
            except ValueError:
                failures.append("sequential_q_holdout_proof_failed")
                break
            target = target_by_id[assignment.assignment_id]
            chosen_key = assignment.chosen_treatment.treatment_key
            if chosen_key not in target.probabilities or chosen_key not in prediction.q_values:
                failures.append("sequential_target_or_q_support_missing")
                break
            target_probability = target.probabilities[chosen_key]
            if target_probability < 0:
                failures.append("sequential_target_probability_invalid")
                break
            ratio = target_probability / assignment.chosen_propensity
            cumulative_ratio = min(
                policy.maximum_cumulative_importance_weight,
                cumulative_ratio * ratio,
            )
            reward = outcome_by_assignment[assignment.assignment_id].realized_value
            temporal_difference = (
                reward
                + policy.discount * prediction.next_state_value
                - prediction.q_values[chosen_key]
            )
            value += discount_power * cumulative_ratio * temporal_difference
            discount_power *= policy.discount
        else:
            episode_evidence.append(
                SequentialEpisodeEvidence(
                    episode_id=episode_id,
                    steps=len(members),
                    cluster_id=next(iter(cluster_ids)),
                    cumulative_importance_weight=cumulative_ratio,
                    dr_value=value,
                )
            )
    weights = [item.cumulative_importance_weight for item in episode_evidence]
    ess = _effective_sample_size(weights)
    if ess < policy.minimum_effective_sample_size:
        failures.append("sequential_effective_sample_size_below_floor")
    mean, std_error, clusters = _cluster_robust(tuple(episode_evidence))
    if clusters < policy.minimum_clusters or not math.isfinite(std_error):
        failures.append("sequential_cluster_support_below_floor")
    z_value = NormalDist().inv_cdf(1.0 - policy.confidence_alpha / 2.0)
    lower = mean - z_value * std_error if math.isfinite(std_error) else -math.inf
    target_policy_hash = _hash(
        {
            "version": "sequential-target-policy-v1",
            "decisions": [
                {
                    "assignment_id": item.assignment_id,
                    "probabilities": sorted(item.probabilities.items()),
                }
                for item in sorted(target_policy, key=lambda row: row.assignment_id)
            ],
        }
    )
    unique_failures = tuple(dict.fromkeys(failures))
    material = {
        "version": "sequential-research-policy-value-v1",
        "qualified": not unique_failures,
        "reward_contract_hash": reward_contract_hash,
        "target_policy_hash": target_policy_hash,
        "q_manifest_hash": q_manifest.manifest_hash,
        "episodes": episodes,
        "complete_episodes": len(episode_evidence),
        "completion_rate": round(completion_rate, 12),
        "effective_sample_size": round(ess, 12),
        "clusters": clusters,
        "dr_policy_value": round(mean, 12),
        "cluster_robust_std_error": round(std_error, 12) if math.isfinite(std_error) else "inf",
        "lower_confidence_bound": round(lower, 12) if math.isfinite(lower) else "-inf",
        "experiment_chain_hash": experiment_integrity.chain_hash,
        "randomization_chain_hash": randomization_integrity.chain_hash,
        "episode_evidence": [asdict(item) for item in episode_evidence],
        "failures": unique_failures,
    }
    return SequentialPolicyValueReport(
        qualified=not unique_failures,
        reward_contract_hash=reward_contract_hash,
        target_policy_hash=target_policy_hash,
        q_manifest_hash=q_manifest.manifest_hash,
        episodes=episodes,
        complete_episodes=len(episode_evidence),
        completion_rate=completion_rate,
        effective_sample_size=ess,
        clusters=clusters,
        dr_policy_value=mean,
        cluster_robust_std_error=std_error,
        lower_confidence_bound=lower,
        experiment_chain_hash=experiment_integrity.chain_hash,
        randomization_chain_hash=randomization_integrity.chain_hash,
        episode_evidence=tuple(episode_evidence),
        report_hash=_hash(material),
        failures=unique_failures,
    )
