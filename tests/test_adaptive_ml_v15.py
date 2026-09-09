from scorpion.adaptive_ensemble import (
    AdaptiveEnsemblePolicy,
    AdaptiveEnsembleStatus,
    update_adaptive_ensemble,
)
from scorpion.domain import EventKind
from scorpion.feature_stability import (
    FeatureStabilityExample,
    FeatureStabilityPolicy,
    FeatureStabilityStatus,
    evaluate_feature_stability,
)
from scorpion.regime_mixture import (
    RegimeExpertExample,
    RegimeMixturePolicy,
    RegimeMixtureStatus,
    evaluate_regime_mixture,
)


def _binary_distribution(truth: EventKind, *, good: bool) -> dict[EventKind, float]:
    if truth is EventKind.ENTRY:
        return (
            {EventKind.ENTRY: 0.95, EventKind.IGNORE: 0.05}
            if good
            else {EventKind.ENTRY: 0.05, EventKind.IGNORE: 0.95}
        )
    return (
        {EventKind.ENTRY: 0.02, EventKind.IGNORE: 0.98}
        if good
        else {EventKind.ENTRY: 0.95, EventKind.IGNORE: 0.05}
    )


def test_adaptive_ensemble_learns_better_model_and_freezes_during_drift():
    policy = AdaptiveEnsemblePolicy(
        learning_rate=0.5,
        forgetting_factor=1.0,
        minimum_updates=20,
        maximum_single_model_weight=1.0,
        minimum_effective_models=1.0,
    )
    snapshot = None
    for index in range(30):
        truth = EventKind.ENTRY if index % 5 == 0 else EventKind.IGNORE
        snapshot = update_adaptive_ensemble(
            snapshot,
            {
                "good": _binary_distribution(truth, good=True),
                "bad": _binary_distribution(truth, good=False),
            },
            truth,
            policy=policy,
        )
    assert snapshot is not None
    weights = {item.model_id: item.weight for item in snapshot.weights}
    assert snapshot.status is AdaptiveEnsembleStatus.TRUSTED
    assert weights["good"] > 0.99
    assert weights["good"] > weights["bad"]

    frozen = update_adaptive_ensemble(
        snapshot,
        {
            "good": _binary_distribution(EventKind.ENTRY, good=False),
            "bad": _binary_distribution(EventKind.ENTRY, good=True),
        },
        EventKind.ENTRY,
        drift_active=True,
        policy=policy,
    )
    assert frozen.status is AdaptiveEnsembleStatus.FROZEN_DRIFT
    assert frozen.updates == snapshot.updates
    assert frozen.weights == snapshot.weights


def test_feature_stability_rejects_time_flipping_predictor():
    rows: list[FeatureStabilityExample] = []
    for fold in range(4):
        for index in range(40):
            target = index % 2 == 0
            stable = (2.0 if target else 0.0) + fold * 0.01 + (index % 3) * 0.001
            if fold % 2 == 0:
                flipping = 2.0 if target else 0.0
            else:
                flipping = 0.0 if target else 2.0
            rows.append(
                FeatureStabilityExample(
                    event_id=f"{fold}-{index}",
                    fold=fold,
                    features={"stable": stable, "flipping": flipping},
                    target=target,
                )
            )
    report = evaluate_feature_stability(
        rows,
        policy=FeatureStabilityPolicy(
            minimum_folds=4,
            minimum_fold_samples=30,
            minimum_total_samples=160,
            minimum_direction_agreement=0.75,
            minimum_median_absolute_effect=0.5,
            maximum_single_fold_effect_share=0.40,
        ),
    )
    assert report.status is FeatureStabilityStatus.QUALIFIED
    assert "stable" in report.qualified_features
    assert "flipping" in report.rejected_features
    flipping = next(item for item in report.features if item.feature == "flipping")
    assert flipping.status is FeatureStabilityStatus.FAILED
    assert flipping.direction_agreement <= 0.5


def _specialist_distribution(
    truth: EventKind,
    *,
    regime: str,
    specialist: str,
) -> dict[EventKind, float]:
    good = regime == specialist
    return _binary_distribution(truth, good=good)


def test_regime_mixture_learns_specialists_from_prior_folds_only():
    examples: list[RegimeExpertExample] = []
    for fold in range(4):
        for index in range(60):
            regime = "TIGHT" if index % 2 == 0 else "WIDE"
            truth = EventKind.ENTRY if index % 5 == 0 else EventKind.IGNORE
            examples.append(
                RegimeExpertExample(
                    event_id=f"{fold}-{index}",
                    fold=fold,
                    regime=regime,
                    truth=truth,
                    model_probabilities={
                        "tight-specialist": _specialist_distribution(
                            truth,
                            regime=regime,
                            specialist="TIGHT",
                        ),
                        "wide-specialist": _specialist_distribution(
                            truth,
                            regime=regime,
                            specialist="WIDE",
                        ),
                    },
                )
            )
    report = evaluate_regime_mixture(
        examples,
        labels=(EventKind.ENTRY, EventKind.IGNORE),
        policy=RegimeMixturePolicy(
            minimum_folds=3,
            minimum_training_samples=60,
            minimum_regime_training_samples=25,
            minimum_oos_samples=100,
            learning_rate=1.0,
            wrong_action_penalty=6.0,
            maximum_false_action_rate=0.001,
            maximum_wrong_action_rate=0.001,
            minimum_oos_accuracy=0.99,
        ),
    )
    assert report.status is RegimeMixtureStatus.QUALIFIED
    assert report.oos_accuracy > 0.99
    assert report.false_action_rate == 0.0
    assert report.wrong_action_rate == 0.0
    assert report.fallback_rate == 0.0
    assert {item.regime for item in report.regime_performance} == {"TIGHT", "WIDE"}
