from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .accuracy import DecisionEvidence
from .calibration import (
    CalibratedDecision,
    CalibrationStatus,
    RuleCalibration,
    assess_rule_confidence,
)
from .domain import EventKind, SignalEvent
from .ensemble import EnsembleAssessment
from .semantic import (
    ContractValidation,
    ContractValidationStatus,
    QuoteQualityStatus,
    QuoteValidation,
)


class SemanticDisposition(StrEnum):
    OBSERVE = "OBSERVE"
    READY_FOR_OPERATOR_REVIEW = "READY_FOR_OPERATOR_REVIEW"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class SemanticGatePolicy:
    minimum_effective_confidence: float = 0.95
    maximum_novelty_score: float = 0.70
    maximum_ensemble_entropy: float = 0.30
    minimum_ensemble_vote_share: float = 0.80
    minimum_ensemble_support_confidence: float = 0.75
    require_trusted_actionable_calibration: bool = True


@dataclass(frozen=True, slots=True)
class SemanticEvidenceEnvelope:
    event_id: str
    disposition: SemanticDisposition
    calibrated: CalibratedDecision
    novelty_score: float
    ensemble_consensus_kind: EventKind | None
    ensemble_vote_share: float | None
    ensemble_support_confidence: float | None
    ensemble_entropy: float | None
    contract_status: ContractValidationStatus | None
    quote_status: QuoteQualityStatus | None
    reason_codes: tuple[str, ...]


_ACTIONABLE = {
    EventKind.ENTRY,
    EventKind.ADD,
    EventKind.TRIM,
    EventKind.EXIT,
    EventKind.STOP,
}


def build_semantic_evidence_envelope(
    event: SignalEvent,
    parser_evidence: DecisionEvidence,
    *,
    calibration: RuleCalibration | None = None,
    novelty_score: float = 0.0,
    ensemble: EnsembleAssessment | None = None,
    contract: ContractValidation | None = None,
    quote: QuoteValidation | None = None,
    policy: SemanticGatePolicy | None = None,
) -> SemanticEvidenceEnvelope:
    """Combine independent semantic evidence without granting it execution authority.

    This is deliberately fail-closed for actionable events when configured evidence is weak.
    It never mutates book state, proposed effects, approvals, or broker state.
    """
    policy = policy or SemanticGatePolicy()
    if not 0.0 <= novelty_score <= 1.0:
        raise ValueError("novelty_score must be between 0 and 1")
    if not 0.0 <= policy.minimum_effective_confidence <= 1.0:
        raise ValueError("minimum_effective_confidence must be between 0 and 1")
    if not 0.0 <= policy.maximum_novelty_score <= 1.0:
        raise ValueError("maximum_novelty_score must be between 0 and 1")

    calibrated = assess_rule_confidence(parser_evidence, calibration)
    actionable = event.kind in _ACTIONABLE
    reasons: list[str] = []

    if event.kind is EventKind.IGNORE:
        disposition = SemanticDisposition.OBSERVE
    elif event.kind is EventKind.AMBIGUOUS:
        disposition = SemanticDisposition.REVIEW_REQUIRED
        reasons.append("ambiguous_interpretation")
    else:
        if actionable and policy.require_trusted_actionable_calibration:
            if calibrated.status is CalibrationStatus.UNCALIBRATED:
                reasons.append("calibration_uncalibrated")
            elif calibrated.status is CalibrationStatus.PROVISIONAL:
                reasons.append("calibration_provisional")
            elif calibrated.status is CalibrationStatus.DEGRADED:
                reasons.append("calibration_degraded")

        if calibrated.effective_confidence < policy.minimum_effective_confidence:
            reasons.append("effective_confidence_below_policy")
        if novelty_score >= policy.maximum_novelty_score:
            reasons.append("novel_wording")

        if ensemble is not None:
            if ensemble.actionable_disagreement:
                reasons.append("ensemble_actionability_disagreement")
            if ensemble.entropy >= policy.maximum_ensemble_entropy:
                reasons.append("ensemble_entropy")
            if ensemble.consensus_confidence < policy.minimum_ensemble_vote_share:
                reasons.append("ensemble_vote_share_below_policy")
            if ensemble.support_confidence < policy.minimum_ensemble_support_confidence:
                reasons.append("ensemble_support_below_policy")

        if contract is not None and contract.status is ContractValidationStatus.REVIEW:
            reasons.append("contract_semantics")
        if quote is not None:
            if quote.status is QuoteQualityStatus.INVALID:
                reasons.append("quote_invalid")
            elif quote.status is QuoteQualityStatus.REVIEW:
                reasons.append("quote_semantics")

        disposition = (
            SemanticDisposition.REVIEW_REQUIRED
            if reasons
            else SemanticDisposition.READY_FOR_OPERATOR_REVIEW
        )

    return SemanticEvidenceEnvelope(
        event_id=event.event_id,
        disposition=disposition,
        calibrated=calibrated,
        novelty_score=novelty_score,
        ensemble_consensus_kind=ensemble.consensus_kind if ensemble is not None else None,
        ensemble_vote_share=ensemble.consensus_confidence if ensemble is not None else None,
        ensemble_support_confidence=ensemble.support_confidence if ensemble is not None else None,
        ensemble_entropy=ensemble.entropy if ensemble is not None else None,
        contract_status=contract.status if contract is not None else None,
        quote_status=quote.status if quote is not None else None,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )
