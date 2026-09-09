from scorpion.domain import EventKind
from scorpion.shift_weighted_conformal import (
    ShiftWeightedCalibrationRow,
    ShiftWeightedConformalPolicy,
    ShiftWeightedConformalStatus,
    evaluate_shift_weighted_conformal,
    fit_shift_weighted_conformal,
)

LABELS = (EventKind.ENTRY, EventKind.IGNORE)


def _row(index: int, weight: float = 1.0) -> ShiftWeightedCalibrationRow:
    truth = EventKind.ENTRY if index % 5 == 0 else EventKind.IGNORE
    probabilities = (
        {EventKind.ENTRY: 0.95, EventKind.IGNORE: 0.05}
        if truth is EventKind.ENTRY
        else {EventKind.ENTRY: 0.03, EventKind.IGNORE: 0.97}
    )
    return ShiftWeightedCalibrationRow(probabilities, truth, weight)


def test_shift_weighted_conformal_passes_with_broad_effective_support():
    rows = tuple(_row(index, 1.0 + (index % 3) * 0.2) for index in range(200))
    policy = ShiftWeightedConformalPolicy(
        alpha=0.05,
        minimum_effective_sample_size=100,
        maximum_normalized_weight_share=0.02,
        minimum_evaluation_samples=100,
        minimum_empirical_coverage=0.95,
    )
    calibrator = fit_shift_weighted_conformal(rows, labels=LABELS, policy=policy)
    assert calibrator.status is ShiftWeightedConformalStatus.QUALIFIED
    evaluation_probabilities = [item.probabilities for item in rows[:100]]
    evaluation_truth = [item.truth for item in rows[:100]]
    evaluation = evaluate_shift_weighted_conformal(
        calibrator,
        evaluation_probabilities,
        evaluation_truth,
        policy=policy,
    )
    assert evaluation.status is ShiftWeightedConformalStatus.QUALIFIED
    assert evaluation.empirical_coverage >= 0.95


def test_shift_weighted_conformal_rejects_degenerate_importance_weights():
    rows = tuple(
        _row(index, 1000.0 if index == 0 else 0.01)
        for index in range(200)
    )
    calibrator = fit_shift_weighted_conformal(
        rows,
        labels=LABELS,
        policy=ShiftWeightedConformalPolicy(
            alpha=0.05,
            minimum_effective_sample_size=100,
            maximum_normalized_weight_share=0.05,
            maximum_raw_weight=1000.0,
        ),
    )
    assert calibrator.status is ShiftWeightedConformalStatus.INSUFFICIENT
    assert calibrator.effective_sample_size < 10
    assert calibrator.max_normalized_weight_share > 0.90
    assert any("effective_sample_size" in item for item in calibrator.failures)
