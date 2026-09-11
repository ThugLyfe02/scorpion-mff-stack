from datetime import UTC, datetime, timedelta

import pytest

from scorpion.contextual_ensemble import (
    ContextualEnsembleObservation,
    ContextualEnsemblePolicy,
    ContextualEnsembleStatus,
    evaluate_contextual_ensemble,
    fit_contextual_ensemble,
    predict_contextual_ensemble,
)
from scorpion.domain import EventKind

BASE = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
LABELS = (EventKind.ENTRY, EventKind.IGNORE)


def _probabilities(truth: EventKind, confidence: float) -> dict[EventKind, float]:
    other = EventKind.IGNORE if truth is EventKind.ENTRY else EventKind.ENTRY
    return {truth: confidence, other: 1.0 - confidence}


def _row(
    index: int,
    *,
    regime: str,
    truth: EventKind,
    a_confidence: float,
    b_confidence: float,
    fold: str | None = None,
    day_offset: int | None = None,
) -> ContextualEnsembleObservation:
    return ContextualEnsembleObservation(
        event_id=f"event-{index}",
        fold_id=fold or f"fold-{index % 3}",
        observed_ts_utc=BASE + timedelta(days=day_offset if day_offset is not None else index // 8),
        truth=truth,
        model_probabilities={
            "model-a": _probabilities(truth, a_confidence),
            "model-b": _probabilities(truth, b_confidence),
        },
        slices={"regime": regime, "channel": "mff"},
    )


def _policy(**overrides: object) -> ContextualEnsemblePolicy:
    values: dict[str, object] = {
        "context_dimensions": ("regime",),
        "interaction_order": 1,
        "minimum_global_samples": 12,
        "minimum_oof_folds": 3,
        "minimum_context_samples": 6,
        "minimum_context_folds": 3,
        "prior_strength": 5.0,
        "skill_scale": 4.0,
        "maximum_context_weight_shift": 0.30,
        "minimum_context_oof_log_loss_gain": 0.0,
        "maximum_contexts": 20,
        "minimum_holdout_samples": 12,
        "minimum_holdout_days": 3,
        "minimum_mean_log_loss_improvement": 0.001,
        "minimum_bootstrap_lower_bound": 0.0,
        "maximum_accuracy_regression": 0.0,
        "maximum_brier_regression": 0.0,
        "maximum_actionable_false_positive_regression": 0.0,
        "bootstrap_resamples": 200,
        "bootstrap_block_days": 2,
        "bootstrap_alpha": 0.05,
        "bootstrap_seed": 7,
    }
    values.update(overrides)
    return ContextualEnsemblePolicy(**values)  # type: ignore[arg-type]


def _training_rows() -> tuple[ContextualEnsembleObservation, ...]:
    rows: list[ContextualEnsembleObservation] = []
    for index in range(30):
        truth = EventKind.ENTRY if index % 2 == 0 else EventKind.IGNORE
        rows.append(
            _row(
                index,
                regime="trend",
                truth=truth,
                a_confidence=0.95,
                b_confidence=0.55,
            )
        )
    for offset in range(30):
        index = 30 + offset
        truth = EventKind.ENTRY if index % 2 == 0 else EventKind.IGNORE
        rows.append(
            _row(
                index,
                regime="mean-revert",
                truth=truth,
                a_confidence=0.55,
                b_confidence=0.95,
            )
        )
    return tuple(rows)


def _holdout(*, reversed_specialists: bool = False) -> tuple[ContextualEnsembleObservation, ...]:
    rows: list[ContextualEnsembleObservation] = []
    for offset in range(12):
        index = 100 + offset
        truth = EventKind.ENTRY if index % 2 == 0 else EventKind.IGNORE
        a_confidence, b_confidence = (0.55, 0.95) if reversed_specialists else (0.95, 0.55)
        rows.append(
            _row(
                index,
                regime="trend",
                truth=truth,
                a_confidence=a_confidence,
                b_confidence=b_confidence,
                day_offset=offset // 4,
            )
        )
    for offset in range(12):
        index = 112 + offset
        truth = EventKind.ENTRY if index % 2 == 0 else EventKind.IGNORE
        a_confidence, b_confidence = (0.95, 0.55) if reversed_specialists else (0.55, 0.95)
        rows.append(
            _row(
                index,
                regime="mean-revert",
                truth=truth,
                a_confidence=a_confidence,
                b_confidence=b_confidence,
                day_offset=3 + offset // 4,
            )
        )
    return tuple(rows)


def test_contextual_fit_learns_oof_specialists_and_caps_weight_shift():
    policy = _policy()
    fit = fit_contextual_ensemble(_training_rows(), policy=policy)
    global_weights = dict(fit.global_weights)
    contexts = {record.context_key: record for record in fit.contexts}
    trend = contexts[(('regime', 'trend'),)]
    mean_revert = contexts[(('regime', 'mean-revert'),)]
    assert dict(trend.weights)["model-a"] > global_weights["model-a"]
    assert dict(mean_revert.weights)["model-b"] > global_weights["model-b"]
    for record in fit.contexts:
        for model_id, weight in record.weights:
            assert abs(weight - global_weights[model_id]) <= policy.maximum_context_weight_shift + 1e-9
        assert record.folds >= policy.minimum_context_folds


def test_sparse_context_is_not_allowed_to_specialize():
    policy = _policy(minimum_context_samples=8)
    rows = list(_training_rows())
    for offset in range(3):
        rows.append(
            _row(
                500 + offset,
                regime="rare",
                truth=EventKind.ENTRY,
                a_confidence=0.99,
                b_confidence=0.51,
            )
        )
    fit = fit_contextual_ensemble(tuple(rows), policy=policy)
    assert all(record.context_key != (("regime", "rare"),) for record in fit.contexts)


def test_unknown_context_falls_back_to_global_weights():
    policy = _policy()
    fit = fit_contextual_ensemble(_training_rows(), policy=policy)
    row = _row(
        900,
        regime="unknown",
        truth=EventKind.ENTRY,
        a_confidence=0.7,
        b_confidence=0.6,
    )
    prediction = predict_contextual_ensemble(fit, row)
    assert prediction.selected_context is None
    assert prediction.weights == fit.global_weights


def test_fit_hash_is_order_invariant():
    policy = _policy()
    rows = _training_rows()
    first = fit_contextual_ensemble(rows, policy=policy)
    second = fit_contextual_ensemble(tuple(reversed(rows)), policy=policy)
    assert first.training_fingerprint == second.training_fingerprint
    assert first.fit_hash == second.fit_hash


def test_frozen_holdout_overlap_is_rejected():
    policy = _policy()
    training = _training_rows()
    fit = fit_contextual_ensemble(training, policy=policy)
    with pytest.raises(ValueError, match="holdout overlaps"):
        evaluate_contextual_ensemble(fit, training[:12], policy=policy)


def test_contextual_specialization_must_survive_untouched_holdout():
    policy = _policy()
    fit = fit_contextual_ensemble(_training_rows(), policy=policy)
    report = evaluate_contextual_ensemble(fit, _holdout(), policy=policy)
    assert report.status is ContextualEnsembleStatus.READY_FOR_SHADOW_RESEARCH
    assert report.mean_log_loss_improvement > 0
    assert report.bootstrap_lower_bound >= 0
    assert report.contextual_log_loss < report.global_log_loss
    assert report.contextual_brier <= report.global_brier
    assert report.contextual_usage_rate == 1.0


def test_reversed_holdout_specialists_block_contextual_ensemble():
    policy = _policy()
    fit = fit_contextual_ensemble(_training_rows(), policy=policy)
    report = evaluate_contextual_ensemble(
        fit,
        _holdout(reversed_specialists=True),
        policy=policy,
    )
    assert report.status is ContextualEnsembleStatus.BLOCKED
    assert "insufficient_mean_log_loss_improvement" in report.failures
    assert report.contextual_log_loss > report.global_log_loss


def test_actionable_false_positive_regression_is_an_explicit_firewall():
    policy = _policy(
        minimum_holdout_samples=6,
        minimum_holdout_days=1,
        minimum_mean_log_loss_improvement=-10.0,
        minimum_bootstrap_lower_bound=-10.0,
        maximum_accuracy_regression=1.0,
        maximum_brier_regression=1.0,
        maximum_actionable_false_positive_regression=0.0,
    )
    fit = fit_contextual_ensemble(_training_rows(), policy=policy)
    holdout = tuple(
        ContextualEnsembleObservation(
            event_id=f"fp-{index}",
            fold_id=f"holdout-{index % 3}",
            observed_ts_utc=BASE + timedelta(days=index // 2),
            truth=EventKind.IGNORE,
            model_probabilities={
                "model-a": {EventKind.ENTRY: 0.90, EventKind.IGNORE: 0.10},
                "model-b": {EventKind.ENTRY: 0.05, EventKind.IGNORE: 0.95},
            },
            slices={"regime": "trend", "channel": "mff"},
        )
        for index in range(6)
    )
    report = evaluate_contextual_ensemble(fit, holdout, policy=policy)
    assert report.status is ContextualEnsembleStatus.BLOCKED
    assert "actionable_false_positive_regression" in report.failures
    assert (
        report.contextual_actionable_false_positive_rate
        > report.global_actionable_false_positive_rate
    )
