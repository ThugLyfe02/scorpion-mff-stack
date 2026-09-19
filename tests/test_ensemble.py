from scorpion.domain import EventKind
from scorpion.ensemble import assess_ensemble
from scorpion.shadow import ShadowPrediction


def prediction(kind: EventKind, confidence: float = 0.9, model: str = "m") -> ShadowPrediction:
    return ShadowPrediction(model, "v1", kind, confidence, 5.0)


def test_unanimous_shadow_ensemble_is_low_uncertainty():
    assessment = assess_ensemble(
        [prediction(EventKind.ENTRY, model="a"), prediction(EventKind.ENTRY, model="b")]
    )
    assert assessment.consensus_kind is EventKind.ENTRY
    assert assessment.consensus_confidence == 1.0
    assert assessment.support_confidence == 0.9
    assert assessment.actionable_disagreement is False
    assert assessment.needs_adjudication is False


def test_actionable_disagreement_forces_adjudication():
    assessment = assess_ensemble(
        [prediction(EventKind.ENTRY, model="a"), prediction(EventKind.IGNORE, model="b")]
    )
    assert assessment.actionable_disagreement is True
    assert assessment.needs_adjudication is True
    assert assessment.entropy == 1.0


def test_zero_weight_prediction_is_excluded_from_entropy_and_disagreement():
    assessment = assess_ensemble(
        [prediction(EventKind.ENTRY, model="a"), prediction(EventKind.IGNORE, model="b")],
        weights={"a@v1": 1.0, "b@v1": 0.0},
    )
    assert assessment.consensus_kind is EventKind.ENTRY
    assert assessment.distribution == {"ENTRY": 1.0}
    assert assessment.entropy == 0.0
    assert assessment.actionable_disagreement is False


def test_unanimous_low_confidence_models_do_not_masquerade_as_strong_support():
    assessment = assess_ensemble(
        [
            prediction(EventKind.ENTRY, confidence=0.4, model="a"),
            prediction(EventKind.ENTRY, confidence=0.4, model="b"),
        ]
    )
    assert assessment.consensus_confidence == 1.0
    assert assessment.support_confidence == 0.4
    assert assessment.needs_adjudication is True
