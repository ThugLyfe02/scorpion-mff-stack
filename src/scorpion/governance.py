from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .adaptive_ensemble import AdaptiveEnsembleSnapshot
from .canary import CanaryReport, CanaryStatus
from .conformal import ConformalEvaluation
from .feature_stability import FeatureStabilityReport
from .mondrian_conformal import MondrianEvaluation
from .oof_stacking import CrossFittedStackingReport
from .regime_mixture import RegimeMixtureReport
from .selective import SelectivePolicy
from .sequential_evidence import ExecutionEvidenceMonitorSnapshot, SequentialEvidenceStatus
from .temporal_crossfit import TemporalCrossFitReport
from .tournament import CandidateScore
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
    sequential_evidence: ExecutionEvidenceMonitorSnapshot | None = None
    temporal_crossfit: TemporalCrossFitReport | None = None
    stacking: CrossFittedStackingReport | None = None
    adaptive_ensemble: AdaptiveEnsembleSnapshot | None = None
    feature_stability: FeatureStabilityReport | None = None
    regime_mixture: RegimeMixtureReport | None = None


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
