from scorpion.domain import EventKind
from scorpion.targeted_regression_firewall import (
    RegressionFirewallStatus,
    TargetedRegressionExample,
    TargetedRegressionPolicy,
    evaluate_targeted_regression_firewall,
)

LABELS = (EventKind.ENTRY, EventKind.IGNORE)


def _probabilities(truth: EventKind, confidence: float):
    other = EventKind.IGNORE if truth is EventKind.ENTRY else EventKind.ENTRY
    return {truth: confidence, other: 1.0 - confidence}


def _rows(
    *,
    target_challenger: float = 0.85,
    protected_challenger: float = 0.76,
    false_action: bool = False,
):
    rows: list[TargetedRegressionExample] = []
    for slice_key in ("hotspot:wide-spread", "protected:normal"):
        for index in range(60):
            truth = EventKind.ENTRY if index % 2 == 0 else EventKind.IGNORE
            incumbent_confidence = 0.60 if slice_key.startswith("hotspot") else 0.75
            challenger_confidence = (
                target_challenger
                if slice_key.startswith("hotspot")
                else protected_challenger
            )
            challenger = _probabilities(truth, challenger_confidence)
            if false_action and slice_key.startswith("protected") and index == 1:
                truth = EventKind.IGNORE
                challenger = {EventKind.ENTRY: 0.90, EventKind.IGNORE: 0.10}
            rows.append(
                TargetedRegressionExample(
                    event_id=f"{slice_key}-{index}",
                    slice_key=slice_key,
                    truth=truth,
                    incumbent_probabilities=_probabilities(truth, incumbent_confidence),
                    challenger_probabilities=challenger,
                    incumbent_latency_ms=1.0,
                    challenger_latency_ms=1.5,
                )
            )
    return tuple(rows)


def _policy():
    return TargetedRegressionPolicy(
        minimum_target_samples=50,
        minimum_protected_samples=50,
        bootstrap_trials=500,
        minimum_target_log_loss_improvement=0.0,
        maximum_protected_log_loss_degradation=0.01,
        maximum_global_brier_delta=0.0,
        maximum_false_action_rate_delta=0.0,
        maximum_wrong_action_rate_delta=0.0,
        maximum_mean_latency_delta_ms=1.0,
    )


def test_targeted_fix_passes_when_hotspot_improves_and_protected_slice_is_safe():
    report = evaluate_targeted_regression_firewall(
        _rows(),
        target_slices=("hotspot:wide-spread",),
        labels=LABELS,
        policy=_policy(),
    )
    assert report.status is RegressionFirewallStatus.QUALIFIED
    assert report.qualified is True
    target = next(item for item in report.slices if item.target)
    assert target.bootstrap_lower_improvement > 0
    assert report.false_action_rate_delta == 0.0
    assert report.wrong_action_rate_delta == 0.0


def test_targeted_fix_is_rejected_when_protected_population_regresses():
    report = evaluate_targeted_regression_firewall(
        _rows(protected_challenger=0.55),
        target_slices=("hotspot:wide-spread",),
        labels=LABELS,
        policy=_policy(),
    )
    assert report.status is RegressionFirewallStatus.FAILED
    assert any("protected_slice_regression" in item for item in report.failures)


def test_targeted_fix_is_rejected_for_new_false_action_even_if_hotspot_improves():
    report = evaluate_targeted_regression_firewall(
        _rows(false_action=True),
        target_slices=("hotspot:wide-spread",),
        labels=LABELS,
        policy=TargetedRegressionPolicy(
            minimum_target_samples=50,
            minimum_protected_samples=50,
            bootstrap_trials=400,
            maximum_protected_log_loss_degradation=0.20,
            maximum_global_brier_delta=0.20,
            maximum_false_action_rate_delta=0.0,
            maximum_wrong_action_rate_delta=0.0,
            maximum_mean_latency_delta_ms=1.0,
        ),
    )
    assert report.status is RegressionFirewallStatus.FAILED
    assert report.false_action_rate_delta > 0
    assert any("false_action_rate_regression" in item for item in report.failures)


def test_missing_target_slice_fails_closed():
    report = evaluate_targeted_regression_firewall(
        tuple(row for row in _rows() if row.slice_key == "protected:normal"),
        target_slices=("hotspot:wide-spread",),
        labels=LABELS,
        policy=_policy(),
    )
    assert report.status is RegressionFirewallStatus.INSUFFICIENT
    assert "target_slice_missing:hotspot:wide-spread" in report.failures
