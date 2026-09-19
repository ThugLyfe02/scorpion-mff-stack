from datetime import UTC, date, datetime
from decimal import Decimal

from scorpion.accuracy import DecisionEvidence
from scorpion.calibration import CalibrationObservation, calibrate_rules
from scorpion.domain import EventKind, SignalEvent
from scorpion.ensemble import assess_ensemble
from scorpion.semantic_envelope import (
    SemanticDisposition,
    build_semantic_evidence_envelope,
)
from scorpion.shadow import ShadowPrediction


def _event(kind: EventKind = EventKind.ENTRY) -> SignalEvent:
    ts = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    return SignalEvent(
        event_id="event",
        message_id="message",
        kind=kind,
        channel_id="channel",
        author_id="author",
        source_ts_utc=ts,
        received_ts_utc=ts,
        ticker="AAPL",
        option_side="CALL",
        strike=Decimal("200"),
        expiry=date(2026, 9, 8),
        referenced_price=Decimal("1.00"),
    )


def _trusted_calibration():
    observations = [
        CalibrationObservation("entry.complete", 0.998, True, actionable=True)
        for _ in range(100)
    ]
    return calibrate_rules(observations)["entry.complete"]


def test_trusted_low_novelty_actionable_event_can_reach_operator_review():
    envelope = build_semantic_evidence_envelope(
        _event(),
        DecisionEvidence("entry.complete", 0.998),
        calibration=_trusted_calibration(),
        novelty_score=0.05,
    )
    assert envelope.disposition is SemanticDisposition.READY_FOR_OPERATOR_REVIEW
    assert envelope.reason_codes == ()


def test_uncalibrated_actionable_event_fails_closed_to_review():
    envelope = build_semantic_evidence_envelope(
        _event(),
        DecisionEvidence("entry.complete", 0.998),
    )
    assert envelope.disposition is SemanticDisposition.REVIEW_REQUIRED
    assert "calibration_uncalibrated" in envelope.reason_codes


def test_low_absolute_ensemble_support_requires_review_even_with_unanimity():
    ensemble = assess_ensemble(
        (
            ShadowPrediction("a", "v1", EventKind.ENTRY, 0.4, 5.0),
            ShadowPrediction("b", "v1", EventKind.ENTRY, 0.4, 5.0),
        )
    )
    envelope = build_semantic_evidence_envelope(
        _event(),
        DecisionEvidence("entry.complete", 0.998),
        calibration=_trusted_calibration(),
        ensemble=ensemble,
    )
    assert envelope.disposition is SemanticDisposition.REVIEW_REQUIRED
    assert "ensemble_support_below_policy" in envelope.reason_codes


def test_ignore_event_remains_observation_only():
    envelope = build_semantic_evidence_envelope(
        _event(EventKind.IGNORE),
        DecisionEvidence("ignore.no_supported_signal", 0.995),
        novelty_score=1.0,
    )
    assert envelope.disposition is SemanticDisposition.OBSERVE
