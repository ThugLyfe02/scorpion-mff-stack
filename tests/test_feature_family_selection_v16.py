from scorpion.domain import EventKind
from scorpion.feature_family_selection import (
    FeatureFamilyCandidate,
    FeatureFamilySelectionPolicy,
    FeatureFamilyStatus,
    evaluate_feature_families,
)
from scorpion.incremental_oof_value import IncrementalOOFExample, IncrementalOOFPolicy

LABELS = (EventKind.ENTRY, EventKind.IGNORE)


def _probabilities(truth: EventKind, confidence: float):
    other = EventKind.IGNORE if truth is EventKind.ENTRY else EventKind.ENTRY
    return {truth: confidence, other: 1.0 - confidence}


def _family(family_id: str, challenger_by_fold: tuple[float, ...]):
    rows: list[IncrementalOOFExample] = []
    for fold, challenger_confidence in enumerate(challenger_by_fold):
        for index in range(20):
            truth = EventKind.ENTRY if index % 2 == 0 else EventKind.IGNORE
            rows.append(
                IncrementalOOFExample(
                    event_id=f"{family_id}-{fold}-{index}",
                    fold=fold,
                    truth=truth,
                    incumbent_probabilities=_probabilities(truth, 0.70),
                    challenger_probabilities=_probabilities(truth, challenger_confidence),
                )
            )
    return FeatureFamilyCandidate(family_id, tuple(rows))


def test_feature_family_search_selects_only_stable_incremental_family():
    candidates = (
        _family("stable_microstructure", (0.84,) * 8),
        _family("flat_metadata", (0.70,) * 8),
        _family("lucky_context", (0.82, 0.82, 0.82, 0.82, 0.82, 0.62, 0.62, 0.62)),
    )
    report = evaluate_feature_families(
        candidates,
        labels=LABELS,
        policy=FeatureFamilySelectionPolicy(
            false_discovery_rate=0.05,
            minimum_folds_for_sign_test=6,
            incremental_policy=IncrementalOOFPolicy(
                minimum_samples=100,
                minimum_folds=6,
                minimum_positive_fold_ratio=0.75,
                bootstrap_trials=500,
                maximum_false_action_rate_delta=0.0,
                maximum_wrong_action_rate_delta=0.0,
            ),
        ),
    )
    assert report.selected_families == ("stable_microstructure",)
    evidence = {item.family_id: item for item in report.evidence}
    assert evidence["stable_microstructure"].status is FeatureFamilyStatus.SELECTED
    assert evidence["stable_microstructure"].fdr_q_value <= 0.05
    assert evidence["flat_metadata"].status is FeatureFamilyStatus.REJECTED
    assert evidence["lucky_context"].status is FeatureFamilyStatus.REJECTED


def test_feature_family_search_requires_enough_independent_time_folds():
    candidate = _family("too_short", (0.85, 0.85, 0.85))
    report = evaluate_feature_families(
        (candidate,),
        labels=LABELS,
        policy=FeatureFamilySelectionPolicy(
            minimum_folds_for_sign_test=4,
            incremental_policy=IncrementalOOFPolicy(
                minimum_samples=40,
                minimum_folds=3,
                bootstrap_trials=200,
            ),
        ),
    )
    item = report.evidence[0]
    assert item.status is FeatureFamilyStatus.INSUFFICIENT
    assert any("insufficient_sign_test_folds" in failure for failure in item.failures)
