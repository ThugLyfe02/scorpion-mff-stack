from scorpion.adaptive_ensemble import (
    AdaptiveEnsembleSnapshot,
    AdaptiveEnsembleStatus,
    AdaptiveModelWeight,
)
from scorpion.evolution_ledger import (
    EvolutionCandidateStatus,
    EvolutionTrigger,
    build_evolution_candidate,
    list_evolution_candidates,
    persist_evolution_candidate,
)
from scorpion.feature_stability import FeatureStabilityReport, FeatureStabilityStatus
from scorpion.regime_mixture import RegimeMixtureReport, RegimeMixtureStatus


def _stable_features() -> FeatureStabilityReport:
    return FeatureStabilityReport(
        samples=240,
        folds=4,
        features=(),
        qualified_features=("quote_age", "spread", "source_lag"),
        rejected_features=("unstable_noise",),
        status=FeatureStabilityStatus.QUALIFIED,
        failures=(),
    )


def _trusted_ensemble(*, drift: bool = False) -> AdaptiveEnsembleSnapshot:
    return AdaptiveEnsembleSnapshot(
        updates=300,
        drift_active=drift,
        weights=(
            AdaptiveModelWeight("semantic", 0.65, 12.0),
            AdaptiveModelWeight("sequence", 0.35, 15.0),
        ),
        effective_models=1.85,
        entropy=0.615,
        max_weight=0.65,
        status=(
            AdaptiveEnsembleStatus.FROZEN_DRIFT
            if drift
            else AdaptiveEnsembleStatus.TRUSTED
        ),
        failures=(),
    )


def _regime_report() -> RegimeMixtureReport:
    return RegimeMixtureReport(
        models=("semantic", "sequence"),
        regimes=("TIGHT", "WIDE"),
        folds=(),
        regime_performance=(),
        oos_samples=240,
        oos_accuracy=0.97,
        false_action_rate=0.0,
        wrong_action_rate=0.0,
        fallback_rate=0.05,
        status=RegimeMixtureStatus.QUALIFIED,
        failures=(),
    )


def test_evolution_candidate_is_content_addressed_and_deduplicated(tmp_path):
    kwargs = {
        "parent_release_id": "parser-v8",
        "trigger": EvolutionTrigger.NEW_DATA,
        "code_revision": "abc123",
        "policy_fingerprint": "policy-sha",
        "dataset_fingerprint": "dataset-sha",
        "feature_set_version": "causal-features-v2",
        "feature_stability": _stable_features(),
        "adaptive_ensemble": _trusted_ensemble(),
        "regime_mixture": _regime_report(),
    }
    first = build_evolution_candidate(**kwargs)
    second = build_evolution_candidate(**kwargs)
    assert first.candidate_id == second.candidate_id
    assert first.status is EvolutionCandidateStatus.RESEARCH_CANDIDATE
    assert first.stable_features == ("quote_age", "source_lag", "spread")

    path = tmp_path / "evolution.db"
    assert persist_evolution_candidate(path, first) is True
    assert persist_evolution_candidate(path, second) is False
    rows = list_evolution_candidates(path)
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == first.candidate_id
    assert rows[0]["status"] == EvolutionCandidateStatus.RESEARCH_CANDIDATE.value


def test_evolution_cycle_records_blocked_candidate_when_drift_freezes_learning():
    candidate = build_evolution_candidate(
        parent_release_id="parser-v8",
        trigger=EvolutionTrigger.DRIFT,
        code_revision="abc123",
        policy_fingerprint="policy-sha",
        dataset_fingerprint="dataset-sha",
        feature_set_version="causal-features-v2",
        feature_stability=_stable_features(),
        adaptive_ensemble=_trusted_ensemble(drift=True),
        regime_mixture=_regime_report(),
    )
    assert candidate.status is EvolutionCandidateStatus.BLOCKED_EVIDENCE
    assert "adaptive_ensemble_not_trusted" in candidate.failures
    assert "adaptive_ensemble_frozen_by_drift" in candidate.failures
