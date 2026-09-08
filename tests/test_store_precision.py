from scorpion.accuracy import AssociationEvidence, DecisionEvidence, score_decisions
from scorpion.domain import EventKind
from scorpion.parser import parse_message
from scorpion.shadow import ShadowPrediction, compare_shadow
from scorpion.store import Store


def test_decision_audit_and_adjudication_round_trip(tmp_path, raw_factory):
    store = Store(tmp_path / "audit.db")
    event = parse_message(raw_factory("Nice bounce +10%", message_id="audit"))
    assert store.append_signal(event) is True
    store.append_decision_audit(
        event,
        DecisionEvidence("action.percentage_only", 0.3, latency_us=100),
        AssociationEvidence("not_followup", 1.0, 0),
        pipeline_latency_us=900,
    )
    store.record_adjudication(
        event.event_id,
        EventKind.AMBIGUOUS,
        reviewer="ryan",
    )
    report = score_decisions(store.adjudicated_samples())
    assert report.total == 1
    assert report.accuracy == 1.0
    health = store.decision_health()
    assert health["count"] == 1
    assert health["parser_p95_us"] == 100.0


def test_shadow_prediction_is_observation_only(tmp_path, raw_factory):
    store = Store(tmp_path / "shadow.db")
    event = parse_message(raw_factory("Nice bounce +10%", message_id="shadow"))
    prediction = ShadowPrediction(
        model_name="candidate",
        model_version="v1",
        predicted_kind=EventKind.EXIT,
        confidence=0.9,
        latency_ms=7.0,
    )
    store.append_shadow_prediction(event.event_id, prediction, compare_shadow(event, prediction))
    assert store.load_signals() == []
