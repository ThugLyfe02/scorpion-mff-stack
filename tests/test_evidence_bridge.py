from scorpion.accuracy import AssociationEvidence, DecisionEvidence
from scorpion.domain import EventKind
from scorpion.evidence_bridge import (
    load_model_reliability,
    load_rule_calibration,
    reliability_weights,
)
from scorpion.shadow import ShadowComparison, ShadowPrediction, compare_shadow
from scorpion.store import Store


def test_persisted_adjudications_drive_rule_calibration(tmp_path, raw_factory):
    store = Store(tmp_path / "evidence.db")
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    from scorpion.parser import parse_message_with_evidence

    parsed = parse_message_with_evidence(raw)
    store.append_raw(raw)
    store.append_signal(parsed.event)
    store.append_decision_audit(
        parsed.event,
        DecisionEvidence("entry.complete", 0.998),
        AssociationEvidence("not_required", 1.0, 0),
        pipeline_latency_us=100,
    )
    store.record_adjudication(parsed.event.event_id, EventKind.ENTRY, reviewer="reviewer")
    calibration = load_rule_calibration(store.path)
    assert calibration["entry.complete"].samples == 1
    assert calibration["entry.complete"].correct == 1


def test_shadow_reliability_becomes_ensemble_weight(tmp_path, raw_factory):
    store = Store(tmp_path / "shadow.db")
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    from scorpion.parser import parse_message_with_evidence

    parsed = parse_message_with_evidence(raw)
    store.append_raw(raw)
    store.append_signal(parsed.event)
    prediction = ShadowPrediction("model", "v1", EventKind.ENTRY, 0.9, 5.0)
    comparison: ShadowComparison = compare_shadow(parsed.event, prediction)
    store.append_shadow_prediction(parsed.event.event_id, prediction, comparison)
    store.record_adjudication(parsed.event.event_id, EventKind.ENTRY, reviewer="reviewer")
    reliability = load_model_reliability(store.path)
    assert reliability["model@v1"].correct == 1
    weights = reliability_weights(reliability, minimum_samples=20)
    assert 0.0 < weights["model@v1"] <= 0.25
