from scorpion.domain import EventKind
from scorpion.incremental_oof_value import (
    IncrementalOOFExample,
    IncrementalOOFPolicy,
    IncrementalOOFStatus,
    evaluate_incremental_oof_value,
)

LABELS = (EventKind.ENTRY, EventKind.IGNORE)


def _distribution(truth: EventKind, confidence: float) -> dict[EventKind, float]:
    other = EventKind.IGNORE if truth is EventKind.ENTRY else EventKind.ENTRY
    return {truth: confidence, other: 1.0 - confidence}


def _examples(*, unsafe: bool = False) -> tuple[IncrementalOOFExample, ...]:
    rows: list[IncrementalOOFExample] = []
    for fold in range(4):
        for index in range(40):
            truth = EventKind.ENTRY if index % 5 == 0 else EventKind.IGNORE
            challenger = _distribution(truth, 0.92)
            if unsafe and fold == 3 and index == 39:
                truth = EventKind.IGNORE
                challenger = {EventKind.ENTRY: 0.99, EventKind.IGNORE: 0.01}
            rows.append(
                IncrementalOOFExample(
                    event_id=f"{fold}-{index}",
                    fold=fold,
                    truth=truth,
                    incumbent_probabilities=_distribution(truth, 0.75),
                    challenger_probabilities=challenger,
                )
            )
    return tuple(rows)


def test_incremental_oof_requires_paired_held_out_improvement():
    report = evaluate_incremental_oof_value(
        _examples(),
        labels=LABELS,
        policy=IncrementalOOFPolicy(
            minimum_samples=100,
            minimum_folds=3,
            bootstrap_trials=500,
            minimum_positive_fold_ratio=0.75,
        ),
    )
    assert report.status is IncrementalOOFStatus.QUALIFIED
    assert report.mean_log_loss_improvement > 0
    assert report.bootstrap_lower_log_loss_improvement > 0
    assert report.mean_brier_improvement > 0
    assert report.positive_fold_ratio == 1.0
    assert report.false_action_rate_delta == 0.0


def test_incremental_oof_rejects_new_false_action_despite_better_average_fit():
    report = evaluate_incremental_oof_value(
        _examples(unsafe=True),
        labels=LABELS,
        policy=IncrementalOOFPolicy(
            minimum_samples=100,
            minimum_folds=3,
            bootstrap_trials=500,
            minimum_positive_fold_ratio=0.75,
            maximum_false_action_rate_delta=0.0,
        ),
    )
    assert report.mean_log_loss_improvement > 0
    assert report.mean_brier_improvement > 0
    assert report.false_action_rate_delta > 0
    assert report.status is IncrementalOOFStatus.FAILED
    assert any("false_action_rate_delta" in item for item in report.failures)
