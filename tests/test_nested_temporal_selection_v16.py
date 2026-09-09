from scorpion.nested_temporal_selection import (
    CandidateFoldObservation,
    NestedSelectionPolicy,
    NestedSelectionStatus,
    evaluate_nested_temporal_selection,
)


def _rows(stable: bool) -> tuple[CandidateFoldObservation, ...]:
    rows: list[CandidateFoldObservation] = []
    for fold in range(6):
        rows.append(
            CandidateFoldObservation(
                candidate_id="steady",
                fold=fold,
                reward=0.03,
                false_action_rate=0.0,
                wrong_action_rate=0.0,
            )
        )
        rows.append(
            CandidateFoldObservation(
                candidate_id="flashy",
                fold=fold,
                reward=(0.02 if stable else (0.10 if fold < 2 else -0.08)),
                false_action_rate=0.0,
                wrong_action_rate=0.0,
            )
        )
    return tuple(rows)


def test_nested_temporal_selection_passes_stable_prior_fold_winner():
    report = evaluate_nested_temporal_selection(
        _rows(stable=True),
        policy=NestedSelectionPolicy(
            minimum_candidates=2,
            minimum_folds=6,
            minimum_training_folds=2,
            minimum_oos_folds=4,
            minimum_positive_fold_ratio=0.75,
            maximum_mean_oracle_regret=0.02,
        ),
    )
    assert report.status is NestedSelectionStatus.QUALIFIED
    assert report.mean_oos_reward > 0
    assert report.positive_fold_ratio == 1.0
    assert all(item.selected_candidate == "steady" for item in report.selections)


def test_nested_temporal_selection_exposes_tuning_winner_collapse():
    report = evaluate_nested_temporal_selection(
        _rows(stable=False),
        policy=NestedSelectionPolicy(
            minimum_candidates=2,
            minimum_folds=6,
            minimum_training_folds=2,
            minimum_oos_folds=4,
            minimum_positive_fold_ratio=0.75,
            maximum_mean_oracle_regret=0.04,
        ),
    )
    assert report.selections[0].selected_candidate == "flashy"
    assert report.status is NestedSelectionStatus.FAILED
    assert report.positive_fold_ratio < 0.75 or report.mean_oracle_regret > 0.04


def test_nested_temporal_selection_excludes_unsafe_high_reward_candidate():
    rows: list[CandidateFoldObservation] = []
    for fold in range(6):
        rows.extend(
            (
                CandidateFoldObservation("safe", fold, 0.02, 0.0, 0.0),
                CandidateFoldObservation("unsafe", fold, 0.10, 0.05, 0.0),
            )
        )
    report = evaluate_nested_temporal_selection(
        tuple(rows),
        policy=NestedSelectionPolicy(
            minimum_candidates=2,
            minimum_folds=6,
            minimum_training_folds=2,
            minimum_oos_folds=4,
            maximum_false_action_rate=0.005,
        ),
    )
    assert report.status is NestedSelectionStatus.QUALIFIED
    assert all(item.selected_candidate == "safe" for item in report.selections)
