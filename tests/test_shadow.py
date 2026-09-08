from scorpion.domain import EventKind
from scorpion.parser import parse_message
from scorpion.shadow import DisagreementKind, ShadowPrediction, compare_shadow


def test_shadow_model_cannot_mutate_deterministic_decision(raw_factory):
    event = parse_message(raw_factory("Nice bounce +10%"))
    prediction = ShadowPrediction(
        model_name="shadow-test",
        model_version="1",
        predicted_kind=EventKind.EXIT,
        confidence=0.99,
        latency_ms=12.0,
    )
    comparison = compare_shadow(event, prediction)
    assert event.kind is EventKind.AMBIGUOUS
    assert comparison.disagreement is DisagreementKind.SHADOW_ACTION_MORE_AGGRESSIVE
