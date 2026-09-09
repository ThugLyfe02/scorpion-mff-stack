from scorpion.domain import EventKind
from scorpion.ensemble_diversity import (
    EnsembleDiversityPolicy,
    EnsembleDiversityStatus,
    OOFModelDecision,
    evaluate_ensemble_diversity,
)


def _rows(*, clones: bool) -> tuple[OOFModelDecision, ...]:
    rows: list[OOFModelDecision] = []
    for index in range(120):
        truth = EventKind.ENTRY if index % 4 == 0 else EventKind.IGNORE
        a_prediction = truth if index % 11 else EventKind.AMBIGUOUS
        b_prediction = a_prediction if clones else (truth if index % 13 else EventKind.AMBIGUOUS)
        c_prediction = truth if index % 17 else EventKind.AMBIGUOUS
        rows.extend(
            (
                OOFModelDecision(f"e-{index}", "a", truth, a_prediction),
                OOFModelDecision(f"e-{index}", "b", truth, b_prediction),
                OOFModelDecision(f"e-{index}", "c", truth, c_prediction),
            )
        )
    return tuple(rows)


def test_ensemble_diversity_counts_clone_models_as_one_cluster():
    report = evaluate_ensemble_diversity(
        _rows(clones=True),
        policy=EnsembleDiversityPolicy(
            minimum_events=100,
            minimum_independent_clusters=2,
            prediction_agreement_redundancy=0.95,
            maximum_cluster_weight=0.75,
        ),
    )
    assert report.status is EnsembleDiversityStatus.QUALIFIED
    assert report.independent_clusters == 2
    assert report.largest_cluster_size == 2
    clone_cluster = next(item for item in report.clusters if set(item.members) == {"a", "b"})
    assert clone_cluster.normalized_weight == 2 / 3


def test_ensemble_diversity_rejects_weight_concentration_in_clone_cluster():
    report = evaluate_ensemble_diversity(
        _rows(clones=True),
        model_weights={"a": 0.45, "b": 0.45, "c": 0.10},
        policy=EnsembleDiversityPolicy(
            minimum_events=100,
            minimum_independent_clusters=2,
            prediction_agreement_redundancy=0.95,
            maximum_cluster_weight=0.75,
        ),
    )
    assert report.status is EnsembleDiversityStatus.FAILED
    assert report.maximum_cluster_weight == 0.90
    assert any("cluster_weight" in item for item in report.failures)


def test_ensemble_diversity_accepts_genuinely_different_error_patterns():
    report = evaluate_ensemble_diversity(
        _rows(clones=False),
        policy=EnsembleDiversityPolicy(
            minimum_events=100,
            minimum_independent_clusters=2,
            prediction_agreement_redundancy=0.995,
            error_correlation_redundancy=0.95,
            maximum_cluster_weight=0.75,
        ),
    )
    assert report.status is EnsembleDiversityStatus.QUALIFIED
    assert report.independent_clusters >= 2
