from datetime import date, timedelta

from scorpion.adjudication_pool import append_annotation, consensus_from_pool, load_annotations
from scorpion.domain import EventKind
from scorpion.label_consensus import (
    Annotation,
    ConsensusStatus,
    LabelConsensusPolicy,
    evaluate_label_consensus,
)
from scorpion.safe_policy_improvement import (
    PairedPolicyOutcome,
    PolicyImprovementStatus,
    SafePolicyImprovementPolicy,
    evaluate_safe_policy_improvement,
)
from scorpion.uncertainty_decomposition import (
    UncertaintyHealthPolicy,
    UncertaintyStatus,
    decompose_ensemble_uncertainty,
    evaluate_uncertainty_health,
)


def test_label_consensus_downweights_consistently_wrong_reviewer():
    annotations: list[Annotation] = []
    for index in range(30):
        truth = "ENTRY" if index % 3 == 0 else "IGNORE"
        wrong = "IGNORE" if truth == "ENTRY" else "ENTRY"
        annotations.extend(
            (
                Annotation(f"e-{index}", "r1", truth),
                Annotation(f"e-{index}", "r2", truth),
                Annotation(f"e-{index}", "r3", wrong),
            )
        )
    report = evaluate_label_consensus(
        tuple(annotations),
        policy=LabelConsensusPolicy(
            minimum_annotations_per_event=2,
            minimum_consensus_probability=0.75,
            maximum_entropy=0.70,
            iterations=25,
        ),
    )
    reliability = {item.reviewer_id: item.expected_accuracy for item in report.reviewer_reliability}
    assert report.accepted_events == 30
    assert reliability["r1"] > reliability["r3"]
    assert reliability["r2"] > reliability["r3"]
    assert all(item.status is ConsensusStatus.ACCEPTED for item in report.event_consensus)


def test_adjudication_pool_is_append_only_and_consensus_replayable(tmp_path):
    path = tmp_path / "labels.db"
    assert append_annotation(path, event_id="e1", reviewer_id="r1", label="ENTRY") is True
    assert append_annotation(path, event_id="e1", reviewer_id="r2", label="ENTRY") is True
    assert append_annotation(path, event_id="e1", reviewer_id="r3", label="IGNORE") is True
    assert append_annotation(path, event_id="e1", reviewer_id="r1", label="ENTRY") is False
    assert len(load_annotations(path)) == 3
    report = consensus_from_pool(
        path,
        policy=LabelConsensusPolicy(
            minimum_annotations_per_event=2,
            minimum_consensus_probability=0.60,
            maximum_entropy=0.80,
        ),
    )
    assert report.events == 1
    assert report.event_consensus[0].label == "ENTRY"


def test_uncertainty_decomposition_distinguishes_epistemic_from_aleatoric():
    labels = (EventKind.ENTRY, EventKind.IGNORE)
    conflicted = decompose_ensemble_uncertainty(
        {
            "a": {EventKind.ENTRY: 0.99, EventKind.IGNORE: 0.01},
            "b": {EventKind.ENTRY: 0.01, EventKind.IGNORE: 0.99},
        },
        labels=labels,
    )
    assert conflicted.status is UncertaintyStatus.CONFLICTED
    assert conflicted.normalized_epistemic > conflicted.normalized_aleatoric

    ambiguous = decompose_ensemble_uncertainty(
        {
            "a": {EventKind.ENTRY: 0.50, EventKind.IGNORE: 0.50},
            "b": {EventKind.ENTRY: 0.52, EventKind.IGNORE: 0.48},
        },
        labels=labels,
    )
    assert ambiguous.status is UncertaintyStatus.INHERENTLY_AMBIGUOUS
    assert ambiguous.normalized_aleatoric > ambiguous.normalized_epistemic


def test_uncertainty_health_blocks_conflicted_candidate_sample():
    labels = (EventKind.ENTRY, EventKind.IGNORE)
    rows = tuple(
        decompose_ensemble_uncertainty(
            (
                {
                    "a": {EventKind.ENTRY: 0.99, EventKind.IGNORE: 0.01},
                    "b": {EventKind.ENTRY: 0.01, EventKind.IGNORE: 0.99},
                }
                if index < 20
                else {
                    "a": {EventKind.ENTRY: 0.02, EventKind.IGNORE: 0.98},
                    "b": {EventKind.ENTRY: 0.03, EventKind.IGNORE: 0.97},
                }
            ),
            labels=labels,
        )
        for index in range(100)
    )
    health = evaluate_uncertainty_health(
        rows,
        policy=UncertaintyHealthPolicy(
            minimum_samples=100,
            maximum_conflicted_rate=0.05,
            maximum_needs_more_data_rate=0.20,
            maximum_inherently_ambiguous_rate=0.20,
        ),
    )
    assert health.qualified is False
    assert health.conflicted_rate == 0.20
    assert any("conflicted_rate" in item for item in health.failures)


def _paired_outcomes(*, escalation: bool = False, unstable: bool = False):
    start = date(2026, 1, 2)
    rows: list[PairedPolicyOutcome] = []
    for day_index in range(25):
        improvement = -0.06 if unstable and day_index % 4 == 0 else 0.02
        for event_index in range(5):
            rows.append(
                PairedPolicyOutcome(
                    event_id=f"{day_index}-{event_index}",
                    market_date=start + timedelta(days=day_index),
                    incumbent_reward=0.00,
                    challenger_reward=improvement,
                    incumbent_actionable=False if escalation and day_index == 0 and event_index == 0 else True,
                    challenger_actionable=True,
                )
            )
    return tuple(rows)


def test_safe_policy_improvement_requires_stable_paired_lower_bound():
    policy = SafePolicyImprovementPolicy(
        minimum_events=100,
        minimum_days=20,
        bootstrap_trials=500,
        block_days=3,
        minimum_positive_day_ratio=0.70,
    )
    stable = evaluate_safe_policy_improvement(_paired_outcomes(), policy=policy)
    assert stable.status is PolicyImprovementStatus.QUALIFIED
    assert stable.lower_confidence_bound > 0

    unstable = evaluate_safe_policy_improvement(
        _paired_outcomes(unstable=True),
        policy=policy,
    )
    assert unstable.status is PolicyImprovementStatus.FAILED
    assert unstable.positive_day_ratio < 0.80


def test_safe_policy_improvement_rejects_action_escalation_even_with_reward_gain():
    report = evaluate_safe_policy_improvement(
        _paired_outcomes(escalation=True),
        policy=SafePolicyImprovementPolicy(
            minimum_events=100,
            minimum_days=20,
            bootstrap_trials=300,
            maximum_action_escalations=0,
        ),
    )
    assert report.status is PolicyImprovementStatus.FAILED
    assert report.action_escalations == 1
    assert any("action_escalations" in item for item in report.failures)
