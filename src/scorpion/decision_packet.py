from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .accuracy import AssociationEvidence, DecisionEvidence
from .domain import EventKind, SignalEvent
from .resilience import OperationalMode, ResilienceAssessment
from .sequence_guard import SequenceAssessment
from .source_intelligence import SourceBehaviorShift

PACKET_VERSION = "v1"


class DecisionDisposition(StrEnum):
    OBSERVE = "OBSERVE"
    READY_FOR_OPERATOR_REVIEW = "READY_FOR_OPERATOR_REVIEW"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED_SYSTEM = "BLOCKED_SYSTEM"


@dataclass(frozen=True, slots=True)
class SelectiveReviewPolicy:
    minimum_parser_confidence: float = 0.97
    minimum_association_confidence: float = 0.95
    suspicious_source_shift_score: float = 1.0


@dataclass(frozen=True, slots=True)
class OperatorDecisionPacket:
    packet_id: str
    packet_version: str
    event_id: str
    message_id: str
    event_kind: EventKind
    contract_key: str | None
    disposition: DecisionDisposition
    evidence_strength: float
    parser_rule: str
    parser_confidence: float
    association_method: str
    association_confidence: float
    system_mode: OperationalMode
    sequence_findings: tuple[str, ...]
    resilience_signals: tuple[str, ...]
    source_shift_score: float
    reason_codes: tuple[str, ...]
    created_ts_utc: datetime


def _packet_id(event_id: str, disposition: DecisionDisposition, reasons: tuple[str, ...]) -> str:
    payload = f"{PACKET_VERSION}|{event_id}|{disposition.value}|{'|'.join(reasons)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def build_decision_packet(
    event: SignalEvent,
    parser: DecisionEvidence,
    association: AssociationEvidence,
    resilience: ResilienceAssessment,
    sequence: SequenceAssessment,
    *,
    source_shift: SourceBehaviorShift | None = None,
    policy: SelectiveReviewPolicy | None = None,
    created_ts_utc: datetime | None = None,
) -> OperatorDecisionPacket:
    policy = policy or SelectiveReviewPolicy()
    source_shift_score = source_shift.score if source_shift is not None else 0.0
    reasons: list[str] = []

    if resilience.mode is OperationalMode.HALTED:
        disposition = DecisionDisposition.BLOCKED_SYSTEM
        reasons.append("system_halted")
    elif event.kind is EventKind.IGNORE:
        disposition = DecisionDisposition.OBSERVE
        reasons.append("non_actionable_ignore")
    elif event.kind is EventKind.AMBIGUOUS:
        disposition = DecisionDisposition.REVIEW_REQUIRED
        reasons.append("ambiguous_interpretation")
    elif sequence.critical:
        disposition = DecisionDisposition.REVIEW_REQUIRED
        reasons.append("critical_sequence_integrity")
    elif resilience.mode is OperationalMode.DEGRADED:
        disposition = DecisionDisposition.REVIEW_REQUIRED
        reasons.append("system_degraded")
    elif sequence.requires_review:
        disposition = DecisionDisposition.REVIEW_REQUIRED
        reasons.append("sequence_warning")
    elif parser.confidence < policy.minimum_parser_confidence:
        disposition = DecisionDisposition.REVIEW_REQUIRED
        reasons.append("parser_confidence_below_policy")
    elif association.confidence < policy.minimum_association_confidence:
        disposition = DecisionDisposition.REVIEW_REQUIRED
        reasons.append("association_confidence_below_policy")
    elif source_shift_score >= policy.suspicious_source_shift_score:
        disposition = DecisionDisposition.REVIEW_REQUIRED
        reasons.append("source_behavior_shift")
    else:
        disposition = DecisionDisposition.READY_FOR_OPERATOR_REVIEW
        reasons.append("evidence_meets_review_policy")

    if parser.conflicts:
        reasons.append("parser_conflict_evidence")
    if association.candidate_count > 1:
        reasons.append("multiple_association_candidates")

    source_factor = max(0.25, 1.0 - min(source_shift_score, 1.5) / 2.0)
    sequence_factor = 0.25 if sequence.critical else (0.65 if sequence.requires_review else 1.0)
    resilience_factor = {
        OperationalMode.NORMAL: 1.0,
        OperationalMode.DEGRADED: 0.60,
        OperationalMode.HALTED: 0.0,
    }[resilience.mode]
    evidence_strength = _clamp(
        parser.confidence
        * association.confidence
        * source_factor
        * sequence_factor
        * resilience_factor
    )

    reason_codes = tuple(dict.fromkeys(reasons))
    created = (created_ts_utc or datetime.now(UTC)).astimezone(UTC)
    return OperatorDecisionPacket(
        packet_id=_packet_id(event.event_id, disposition, reason_codes),
        packet_version=PACKET_VERSION,
        event_id=event.event_id,
        message_id=event.message_id,
        event_kind=event.kind,
        contract_key=event.contract_key,
        disposition=disposition,
        evidence_strength=evidence_strength,
        parser_rule=parser.rule_id,
        parser_confidence=parser.confidence,
        association_method=association.method,
        association_confidence=association.confidence,
        system_mode=resilience.mode,
        sequence_findings=tuple(finding.code for finding in sequence.findings),
        resilience_signals=tuple(signal.code for signal in resilience.signals),
        source_shift_score=source_shift_score,
        reason_codes=reason_codes,
        created_ts_utc=created,
    )
