from datetime import UTC, datetime, timedelta

from scorpion.adaptive_ensemble import (
    AdaptiveEnsembleSnapshot,
    AdaptiveEnsembleStatus,
    AdaptiveModelWeight,
)
from scorpion.evolution_controller import (
    EvolutionBaseline,
    EvolutionControlPolicy,
    EvolutionObservation,
    evaluate_evolution_need,
    maybe_generate_evolution_candidate,
)
from scorpion.evolution_ledger import EvolutionCandidateStatus, EvolutionTrigger
from scorpion.feature_stability import FeatureStabilityReport, FeatureStabilityStatus
from scorpion.regime_mixture import RegimeMixtureReport, RegimeMixtureStatus

NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)


def _features(names: tuple[str, ...] = ("spread", "quote_age")) -> FeatureStabilityReport:
    return FeatureStabilityReport(
        samples=500,
        folds=5,
        features=(),
        qualified_features=names,
        rejected_features=(),
        status=FeatureStabilityStatus.QUALIFIED,
        failures=(),
    )


def _ensemble(*, drift: bool = False, weights: tuple[float, float] = (0.6, 0.4)) -> AdaptiveEnsembleSnapshot:
    return AdaptiveEnsembleSnapshot(
        updates=500,
        drift_active=drift,
        weights=(
            AdaptiveModelWeight("semantic", weights[0], 10.0),
            AdaptiveModelWeight("sequence", weights[1], 12.0),
        ),
        effective_models=1.9,
        entropy=0.67,
        max_weight=max(weights),
        status=(
            AdaptiveEnsembleStatus.FROZEN_DRIFT
            if drift
            else AdaptiveEnsembleStatus.TRUSTED
        ),
        failures=(),
    )


def _regimes(names: tuple[str, ...] = ("TIGHT", "WIDE")) -> RegimeMixtureReport:
    return RegimeMixtureReport(
        models=("semantic", "sequence"),
        regimes=names,
        folds=(),
        regime_performance=(),
        oos_samples=400,
        oos_accuracy=0.96,
        false_action_rate=0.0,
        wrong_action_rate=0.0,
        fallback_rate=0.05,
        status=RegimeMixtureStatus.QUALIFIED,
        failures=(),
    )


def _observation(
    *,
    samples: int,
    observed: datetime = NOW,
    drift: bool = False,
    fill_trusted: bool = True,
    features: tuple[str, ...] = ("spread", "quote_age"),
    regimes: tuple[str, ...] = ("TIGHT", "WIDE"),
    weights: tuple[float, float] = (0.6, 0.4),
) -> EvolutionObservation:
    return EvolutionObservation(
        observed_ts_utc=observed,
        dataset_samples=samples,
        feature_stability=_features(features),
        adaptive_ensemble=_ensemble(drift=drift, weights=weights),
        regime_mixture=_regimes(regimes),
        fill_model_trusted=fill_trusted,
        drift_active=drift,
    )


def _baseline() -> EvolutionBaseline:
    return EvolutionBaseline(
        candidate_id="candidate-1",
        created_ts_utc=NOW - timedelta(hours=1),
        dataset_samples=1000,
        stable_features=("quote_age", "spread"),
        model_weights=(("semantic", 0.6), ("sequence", 0.4)),
        regimes=("TIGHT", "WIDE"),
        fill_model_trusted=True,
    )


def test_initial_evolution_waits_for_minimum_new_data():
    policy = EvolutionControlPolicy(minimum_new_samples=100)
    insufficient = evaluate_evolution_need(None, _observation(samples=99), policy=policy)
    ready = evaluate_evolution_need(None, _observation(samples=100), policy=policy)
    assert insufficient.should_generate is False
    assert ready.should_generate is True
    assert ready.primary_trigger is EvolutionTrigger.NEW_DATA


def test_noncritical_changes_are_suppressed_during_cooldown():
    decision = evaluate_evolution_need(
        _baseline(),
        _observation(samples=1200, weights=(0.8, 0.2)),
        policy=EvolutionControlPolicy(
            minimum_new_samples=100,
            minimum_weight_l1_change=0.2,
            cooldown=timedelta(hours=6),
        ),
    )
    assert EvolutionTrigger.NEW_DATA in decision.triggers
    assert EvolutionTrigger.MODEL_DEGRADATION in decision.triggers
    assert decision.cooldown_active is True
    assert decision.should_generate is False
    assert "cooldown_suppressed_noncritical_evolution" in decision.reasons


def test_drift_bypasses_cooldown_but_candidate_remains_evidence_blocked(tmp_path):
    decision, candidate = maybe_generate_evolution_candidate(
        str(tmp_path / "evolution.db"),
        baseline=_baseline(),
        observation=_observation(samples=1020, drift=True),
        parent_release_id="parser-v8",
        code_revision="abc123",
        policy_fingerprint="policy-sha",
        dataset_fingerprint="dataset-sha",
        feature_set_version="causal-features-v2",
        control_policy=EvolutionControlPolicy(cooldown=timedelta(hours=6)),
    )
    assert decision.should_generate is True
    assert decision.primary_trigger is EvolutionTrigger.DRIFT
    assert candidate is not None
    assert candidate.status is EvolutionCandidateStatus.BLOCKED_EVIDENCE
    assert "adaptive_ensemble_frozen_by_drift" in candidate.failures
