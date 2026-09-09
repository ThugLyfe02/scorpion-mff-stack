from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .adaptive_ensemble import AdaptiveEnsembleSnapshot
from .bayesian_changepoint import BayesianChangePointReport
from .canary import CanaryReport, CanaryStatus
from .conformal import ConformalEvaluation
from .ensemble_diversity import EnsembleDiversityReport
from .feature_family_selection import FeatureFamilySelectionReport
from .feature_stability import FeatureStabilityReport
from .hyperparameter_plateau import HyperparameterPlateauReport
from .incremental_oof_value import IncrementalOOFReport
from .label_consensus import LabelConsensusReport
from .label_noise_audit import LabelNoiseReport
from .mondrian_conformal import MondrianEvaluation
from .nested_temporal_selection import NestedSelectionReport
from .oof_stacking import CrossFittedStackingReport
from .parameter_surface import ParameterSurfaceReport
from .pareto_selection import ParetoSelectionReport
from .regime_mixture import RegimeMixtureReport
from .return_distribution_dominance import DistributionDominanceReport
from .safe_policy_improvement import SafePolicyImprovementReport
from .selective import SelectivePolicy
from .sequential_evidence import ExecutionEvidenceMonitorSnapshot, SequentialEvidenceStatus
from .shift_weighted_conformal import ShiftWeightedConformalEvaluation
from .temporal_crossfit import TemporalCrossFitReport
from .tournament import CandidateScore
from .uncertainty_decomposition import UncertaintyHealthReport
from .uncertainty_envelope import ExecutionUncertaintyEnvelope
from .walk_forward import WalkForwardReport


class PromotionStatus(StrEnum):
    READY_FOR_OPERATOR_REVIEW = "READY_FOR_OPERATOR_REVIEW"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    candidate: CandidateScore
    selective_policy: SelectivePolicy
    walk_forward: WalkForwardReport
    research_manifest_hash: str
    online_drifted: bool = False
    dataset_complete: bool = False
    quote_coverage_ok: bool = False
    depth_coverage_ok: bool = False
    canary: CanaryReport | None = None
    execution_uncertainty: ExecutionUncertaintyEnvelope | None = None
    runtime_certified: bool | None = None
    anytime_accuracy_lower_bound: float | None = None
    required_anytime_accuracy_lower_bound: float = 0.95
    conformal: ConformalEvaluation | None = None
    mondrian_conformal: MondrianEvaluation | None = None
    shift_weighted_conformal: ShiftWeightedConformalEvaluation | None = None
    sequential_evidence: ExecutionEvidenceMonitorSnapshot | None = None
    temporal_crossfit: TemporalCrossFitReport | None = None
    stacking: CrossFittedStackingReport | None = None
    incremental_oof: IncrementalOOFReport | None = None
    feature_family_selection: FeatureFamilySelectionReport | None = None
    required_feature_family_id: str | None = None
    nested_selection: NestedSelectionReport | None = None
    hyperparameter_plateau: HyperparameterPlateauReport | None = None
    parameter_surface: ParameterSurfaceReport | None = None
    pareto_selection: ParetoSelectionReport | None = None
    ensemble_diversity: EnsembleDiversityReport | None = None
    adaptive_ensemble: AdaptiveEnsembleSnapshot | None = None
    feature_stability: FeatureStabilityReport | None = None
    regime_mixture: RegimeMixtureReport | None = None
    label_consensus: LabelConsensusReport | None = None
    minimum_label_consensus_rate: float = 0.90
    label_noise: LabelNoiseReport | None = None
    uncertainty_health: UncertaintyHealthReport | None = None
    safe_policy_improvement: SafePolicyImprovementReport | None = None
    return_distribution: DistributionDominanceReport | None = None
    bayesian_changepoint: BayesianChangePointReport | None = None


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    status: PromotionStatus
    failures: tuple[str, ...]
    reason: str


def evaluate_promotion(evidence: PromotionEvidence) -> PromotionDecision:
    """Decide whether a candidate has enough evidence for *human* promotion review.

    This function never deploys or activates a parser/model/strategy. It exists to keep
    research, validation, canary observation, runtime certification, and deployment authority
    separated even as the system becomes more automated.
    """
    if not 0.0 <= evidence.required_anytime_accuracy_lower_bound <= 1.0:
        raise ValueError("required_anytime_accuracy_lower_bound must be between 0 and 1")
    if evidence.anytime_accuracy_lower_bound is not None and not (
        0.0 <= evidence.anytime_accuracy_lower_bound <= 1.0
    ):
        raise ValueError("anytime_accuracy_lower_bound must be between 0 and 1")
    if not 0.0 <= evidence.minimum_label_consensus_rate <= 1.0:
        raise ValueError("minimum_label_consensus_rate must be between 0 and 1")
    if (
        evidence.required_feature_family_id is not None
        and not evidence.required_feature_family_id.strip()
    ):
        raise ValueError("required_feature_family_id cannot be blank")

    failures: list[str] = []
    if not evidence.candidate.qualified:
        failures.append("candidate_tournament_not_qualified")
        failures.extend(f"candidate:{item}" for item in evidence.candidate.failures)
    if not evidence.selective_policy.enabled:
        failures.append("selective_risk_policy_not_empirically_enabled")
    if not evidence.walk_forward.passed:
        failures.append("walk_forward_out_of_sample_failed")
        failures.extend(f"walk_forward:{item}" for item in evidence.walk_forward.failures)
    if not evidence.research_manifest_hash.strip():
        failures.append("research_manifest_missing")
    if evidence.online_drifted:
        failures.append("online_drift_active")
    if evidence.bayesian_changepoint is not None and evidence.bayesian_changepoint.degradation:
        failures.append(
            "bayesian_degradation_changepoint_active:"
            f"p={evidence.bayesian_changepoint.posterior_changepoint_probability:.6f}"
        )
    if not evidence.dataset_complete:
        failures.append("dataset_not_complete")
    if not evidence.quote_coverage_ok:
        failures.append("quote_coverage_insufficient")
    if not evidence.depth_coverage_ok:
        failures.append("depth_coverage_insufficient")
    if evidence.canary is not None:
        if evidence.canary.status is CanaryStatus.QUARANTINE:
            failures.append("shadow_canary_quarantined")
            failures.extend(f"canary:{item}" for item in evidence.canary.failures)
        elif evidence.canary.status is not CanaryStatus.READY_FOR_OPERATOR_REVIEW:
            failures.append("shadow_canary_not_mature")
            failures.extend(f"canary:{item}" for item in evidence.canary.failures)
    if evidence.execution_uncertainty is not None and not evidence.execution_uncertainty.robust:
        failures.append("execution_uncertainty_not_robust")
        failures.extend(
            f"uncertainty:{item}" for item in evidence.execution_uncertainty.failures
        )
    if evidence.runtime_certified is False:
        failures.append("runtime_certification_failed")
    if (
        evidence.anytime_accuracy_lower_bound is not None
        and evidence.anytime_accuracy_lower_bound
        < evidence.required_anytime_accuracy_lower_bound
    ):
        failures.append("anytime_accuracy_confidence_sequence_below_requirement")
    if evidence.conformal is not None and not evidence.conformal.qualified:
        failures.append("conformal_prediction_set_not_qualified")
        failures.extend(f"conformal:{item}" for item in evidence.conformal.failures)
    if evidence.mondrian_conformal is not None and not evidence.mondrian_conformal.qualified:
        failures.append("class_conditional_conformal_not_qualified")
        failures.extend(
            f"mondrian_conformal:{item}" for item in evidence.mondrian_conformal.failures
        )
    if (
        evidence.shift_weighted_conformal is not None
        and not evidence.shift_weighted_conformal.qualified
    ):
        failures.append("shift_weighted_conformal_not_qualified")
        failures.extend(
            f"shift_conformal:{item}"
            for item in evidence.shift_weighted_conformal.failures
        )
    if (
        evidence.sequential_evidence is not None
        and evidence.sequential_evidence.status is SequentialEvidenceStatus.ALARM
    ):
        failures.append("anytime_valid_sequential_degradation_alarm")
    if evidence.temporal_crossfit is not None and not evidence.temporal_crossfit.passed:
        failures.append("temporal_crossfit_not_qualified")
        failures.extend(
            f"temporal_crossfit:{item}" for item in evidence.temporal_crossfit.failures
        )
    if evidence.stacking is not None and not evidence.stacking.qualified:
        failures.append("chronological_oof_stacking_not_qualified")
        failures.extend(f"stacking:{item}" for item in evidence.stacking.failures)
    if evidence.incremental_oof is not None and not evidence.incremental_oof.qualified:
        failures.append("incremental_oof_value_not_qualified")
        failures.extend(
            f"incremental_oof:{item}" for item in evidence.incremental_oof.failures
        )
    if evidence.required_feature_family_id is not None:
        if evidence.feature_family_selection is None:
            failures.append("feature_family_selection_missing_for_required_family")
        elif (
            evidence.required_feature_family_id
            not in evidence.feature_family_selection.selected_families
        ):
            failures.append(
                "required_feature_family_not_selected:"
                f"{evidence.required_feature_family_id}"
            )
    if evidence.nested_selection is not None and not evidence.nested_selection.qualified:
        failures.append("nested_temporal_selection_not_qualified")
        failures.extend(
            f"nested_selection:{item}" for item in evidence.nested_selection.failures
        )
    if (
        evidence.hyperparameter_plateau is not None
        and not evidence.hyperparameter_plateau.qualified
    ):
        failures.append("hyperparameter_plateau_not_qualified")
        failures.extend(
            f"hyperparameter_plateau:{item}"
            for item in evidence.hyperparameter_plateau.failures
        )
    if evidence.parameter_surface is not None and not evidence.parameter_surface.qualified:
        failures.append("parameter_surface_not_qualified")
        failures.extend(
            f"parameter_surface:{item}" for item in evidence.parameter_surface.failures
        )
    if evidence.pareto_selection is not None:
        if not evidence.pareto_selection.qualified:
            failures.append("pareto_selection_not_qualified")
            failures.extend(
                f"pareto_selection:{item}" for item in evidence.pareto_selection.failures
            )
        elif evidence.candidate.name not in evidence.pareto_selection.frontier:
            failures.append(
                "candidate_not_on_safe_pareto_frontier:"
                f"{evidence.candidate.name}"
            )
    if evidence.ensemble_diversity is not None and not evidence.ensemble_diversity.qualified:
        failures.append("ensemble_diversity_not_qualified")
        failures.extend(
            f"ensemble_diversity:{item}" for item in evidence.ensemble_diversity.failures
        )
    if evidence.adaptive_ensemble is not None and not evidence.adaptive_ensemble.trusted:
        failures.append("adaptive_ensemble_not_trusted")
        failures.extend(
            f"adaptive_ensemble:{item}" for item in evidence.adaptive_ensemble.failures
        )
        if evidence.adaptive_ensemble.drift_active:
            failures.append("adaptive_ensemble_frozen_by_drift")
    if evidence.feature_stability is not None and not evidence.feature_stability.qualified:
        failures.append("temporal_feature_stability_not_qualified")
        failures.extend(
            f"feature_stability:{item}" for item in evidence.feature_stability.failures
        )
    if evidence.regime_mixture is not None and not evidence.regime_mixture.qualified:
        failures.append("regime_conditioned_mixture_not_qualified")
        failures.extend(f"regime_mixture:{item}" for item in evidence.regime_mixture.failures)
    if evidence.label_consensus is not None:
        total = evidence.label_consensus.events
        acceptance_rate = evidence.label_consensus.accepted_events / total if total else 0.0
        if acceptance_rate < evidence.minimum_label_consensus_rate:
            failures.append(
                "label_consensus_rate_below_requirement:"
                f"{acceptance_rate:.6f}<{evidence.minimum_label_consensus_rate:.6f}"
            )
    if evidence.label_noise is not None and not evidence.label_noise.qualified:
        failures.append("out_of_fold_label_noise_not_qualified")
        failures.extend(f"label_noise:{item}" for item in evidence.label_noise.failures)
    if evidence.uncertainty_health is not None and not evidence.uncertainty_health.qualified:
        failures.append("ensemble_uncertainty_health_not_qualified")
        failures.extend(
            f"uncertainty_health:{item}" for item in evidence.uncertainty_health.failures
        )
    if (
        evidence.safe_policy_improvement is not None
        and not evidence.safe_policy_improvement.qualified
    ):
        failures.append("safe_policy_improvement_not_qualified")
        failures.extend(
            f"policy_improvement:{item}"
            for item in evidence.safe_policy_improvement.failures
        )
    if evidence.return_distribution is not None and not evidence.return_distribution.qualified:
        failures.append("return_distribution_dominance_not_qualified")
        failures.extend(
            f"return_distribution:{item}" for item in evidence.return_distribution.failures
        )

    if failures:
        return PromotionDecision(
            PromotionStatus.BLOCKED,
            tuple(failures),
            "candidate remains research-only until every supplied independent evidence gate passes",
        )
    return PromotionDecision(
        PromotionStatus.READY_FOR_OPERATOR_REVIEW,
        (),
        (
            "all supplied automated evidence gates passed; explicit operator review is still "
            "required before any production policy/model change"
        ),
    )
