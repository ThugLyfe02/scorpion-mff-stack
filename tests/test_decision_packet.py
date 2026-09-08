from scorpion.accuracy import AssociationEvidence, DecisionEvidence
from scorpion.decision_packet import DecisionDisposition, build_decision_packet
from scorpion.domain import EventKind
from scorpion.parser import parse_message_with_evidence
from scorpion.resilience import OperationalMode, ResilienceAssessment
from scorpion.sequence_guard import SequenceAssessment, SequenceFinding, SequenceSeverity


def _association(confidence: float = 1.0) -> AssociationEvidence:
    return AssociationEvidence("exact_contract", confidence, 1)


def _normal() -> ResilienceAssessment:
    return ResilienceAssessment(OperationalMode.NORMAL, (), ())


def test_high_evidence_entry_is_ready_for_operator_review(raw_factory):
    parsed = parse_message_with_evidence(raw_factory("QQQ 719C TODAY @ 1.01"))
    packet = build_decision_packet(
        parsed.event,
        parsed.evidence,
        _association(),
        _normal(),
        SequenceAssessment(()),
    )
    assert packet.event_kind is EventKind.ENTRY
    assert packet.disposition is DecisionDisposition.READY_FOR_OPERATOR_REVIEW
    assert packet.evidence_strength > 0.95


def test_sequence_warning_forces_review(raw_factory):
    parsed = parse_message_with_evidence(raw_factory("QQQ 719C TODAY @ 1.01"))
    sequence = SequenceAssessment(
        (SequenceFinding("source_timestamp_regression", SequenceSeverity.WARNING, "late"),)
    )
    packet = build_decision_packet(
        parsed.event,
        parsed.evidence,
        _association(),
        _normal(),
        sequence,
    )
    assert packet.disposition is DecisionDisposition.REVIEW_REQUIRED
    assert "sequence_warning" in packet.reason_codes


def test_halted_system_blocks_progression(raw_factory):
    parsed = parse_message_with_evidence(raw_factory("QQQ 719C TODAY @ 1.01"))
    halted = ResilienceAssessment(OperationalMode.HALTED, (), ())
    packet = build_decision_packet(
        parsed.event,
        parsed.evidence,
        _association(),
        halted,
        SequenceAssessment(()),
    )
    assert packet.disposition is DecisionDisposition.BLOCKED_SYSTEM
    assert packet.evidence_strength == 0.0


def test_low_parser_confidence_forces_review(raw_factory):
    parsed = parse_message_with_evidence(raw_factory("QQQ 719C TODAY @ 1.01"))
    evidence = DecisionEvidence(
        rule_id=parsed.evidence.rule_id,
        confidence=0.80,
        matched_terms=parsed.evidence.matched_terms,
        conflicts=(),
        normalized_text=parsed.evidence.normalized_text,
        latency_us=parsed.evidence.latency_us,
    )
    packet = build_decision_packet(
        parsed.event,
        evidence,
        _association(),
        _normal(),
        SequenceAssessment(()),
    )
    assert packet.disposition is DecisionDisposition.REVIEW_REQUIRED
