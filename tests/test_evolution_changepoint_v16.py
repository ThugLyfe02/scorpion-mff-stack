from datetime import UTC, datetime, timedelta

from scorpion.adaptive_ensemble import (
    AdaptiveEnsembleSnapshot,
    AdaptiveEnsembleStatus,
    AdaptiveModelWeight,
)
from scorpion.bayesian_changepoint import (
    BayesianChangePointPolicy,
    evaluate_bayesian_changepoint,
)
from scorpion.evolution_controller import (
    EvolutionBaseline,
    EvolutionControlPolicy,
    EvolutionObservation,
    evaluate_evolution_need,
)
from scorpion.evolution_ledger import EvolutionTrigger
from scorpion.feature_stability import FeatureStabilityReport, FeatureStabilityStatus
from scorpion.regime_mixture import RegimeMixtureReport, RegimeMixtureStatus

NOW = datetime(2026, 9, 9, 16, 0, tzinfo=UTC)


def _features() -> FeatureStabilityReport:
    return FeatureStabilityReport(
        samples=500,
        folds=5,
        features=(),
        qualified_features=("spread", "quote_age"),
        rejected_features=(),
        status=FeatureStabilityStatus.QUALIFIED,
        failures=(),
    )


def _ensemble() -> AdaptiveEnsembleSnapshot:
    return AdaptiveEnsembleSnapshot(
        updates=500,
        drift_active=False,
        weights=(
            AdaptiveModelWeight("semantic", 0.6, 10.0),
            AdaptiveModelWeight("sequence", 0.4, 12.0),
        ),
        effective_models=1.9,
        entropy=0.67,
        max_weight=0.6,
        status=AdaptiveEnsembleStatus.TRUSTED,
        failures=(),
    )


def _regimes() -> RegimeMixtureReport:
    return RegimeMixtureReport(
        models=("semantic", "sequence"),
        regimes=("TIGHT", "WIDE"),
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


def _baseline() -> EvolutionBaseline:
    return EvolutionBaseline(
        candidate_id="baseline",
        created_ts_utc=NOW - timedelta(hours=1),
        dataset_samples=1000,
        stable_features=("quote_age", "spread"),
        model_weights=(("semantic", 0.6), ("sequence", 0.4)),
        regimes=("TIGHT", "WIDE"),
        fill_model_trusted=True,
    )


def _change(rows: tuple[bool, ...]):
    return evaluate_bayesian_changepoint(
        rows,
        policy=BayesianChangePointPolicy(
            window_size=120,
            minimum_segment=15,
            changepoint_prior_probability=0.10,
            watch_posterior_probability=0.40,
            changepoint_posterior_probability=0.80,
            minimum_rate_change=0.20,
        ),
    )


def test_abrupt_degradation_bypasses_evolution_cooldown_as_drift():
    observation = EvolutionObservation(
        observed_ts_utc=NOW,
        dataset_samples=1010,
        feature_stability=_features(),
        adaptive_ensemble=_ensemble(),
        regime_mixture=_regimes(),
        fill_model_trusted=True,
        drift_active=False,
        abrupt_changepoint=_change((False,) * 60 + (True,) * 40),
    )
    decision = evaluate_evolution_need(
        _baseline(),
        observation,
        policy=EvolutionControlPolicy(
            minimum_new_samples=100,
            cooldown=timedelta(hours=6),
        ),
    )
    assert decision.cooldown_active is True
    assert decision.should_generate is True
    assert decision.primary_trigger is EvolutionTrigger.DRIFT
    assert any("bayesian_degradation_changepoint" in reason for reason in decision.reasons)


def test_abrupt_improvement_does_not_trigger_defensive_evolution_by_itself():
    observation = EvolutionObservation(
        observed_ts_utc=NOW,
        dataset_samples=1010,
        feature_stability=_features(),
        adaptive_ensemble=_ensemble(),
        regime_mixture=_regimes(),
        fill_model_trusted=True,
        drift_active=False,
        abrupt_changepoint=_change((True,) * 50 + (False,) * 50),
    )
    decision = evaluate_evolution_need(
        _baseline(),
        observation,
        policy=EvolutionControlPolicy(
            minimum_new_samples=100,
            cooldown=timedelta(hours=6),
        ),
    )
    assert decision.should_generate is False
    assert EvolutionTrigger.DRIFT not in decision.triggers
