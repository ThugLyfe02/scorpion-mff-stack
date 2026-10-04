import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from scorpion.accepted_label_truth import (
    AcceptedLabelMethod,
    accept_label_revision,
    build_accepted_truth_snapshot,
    verify_accepted_truth_ledger,
)
from scorpion.contextual_ensemble import ContextualEnsembleObservation, ContextualEnsemblePolicy
from scorpion.domain import EventKind
from scorpion.hard_example_curriculum import CurriculumExample, HardExampleCurriculumPolicy
from scorpion.information_value import (
    InformationValuePolicy,
    LearningAction,
    LearningValueAssessment,
    allocate_learning_budget,
)
from scorpion.realized_learning_yield import (
    RealizedLearningOutcome,
    RealizedYieldPolicy,
    YieldAttributionMethod,
    YieldCalibrationStatus,
    calibrate_realized_learning_yield,
    record_realized_learning_outcome,
    verify_realized_learning_yield_ledger,
)
from scorpion.trainable_model import FeatureDType, FeatureField, FeatureSchema, TrainingExample
from scorpion.truth_lineage import (
    build_truth_bound_curriculum,
    evaluate_truth_bound_contextual_ensemble,
    fingerprint_truth_bound_training_dataset,
    fit_truth_bound_contextual_ensemble,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _accept(
    path,
    event_id: str,
    label: EventKind,
    *,
    source_revision: str | None = None,
    expected: str | None = None,
    when: datetime = NOW,
):
    return accept_label_revision(
        path,
        event_id=event_id,
        source_revision_id=source_revision or f"source:{event_id}:v1",
        label=label,
        confidence=0.97,
        method=AcceptedLabelMethod.MANUAL_ADJUDICATION,
        evidence_ids=(f"annotation:{event_id}:a", f"annotation:{event_id}:b"),
        accepted_by="reviewer-final",
        reason="accepted research truth",
        expected_current_revision_id=expected,
        created_ts_utc=when,
    )


def test_accepted_truth_revisions_are_cas_guarded_and_historically_reconstructable(tmp_path):
    path = tmp_path / "truth.db"
    first = _accept(path, "event-1", EventKind.ENTRY)
    snapshot_v1 = build_accepted_truth_snapshot(path)
    second = _accept(
        path,
        "event-1",
        EventKind.IGNORE,
        source_revision="source:event-1:v2",
        expected=first.revision_id,
        when=NOW + timedelta(minutes=1),
    )
    current = build_accepted_truth_snapshot(path)
    historical = build_accepted_truth_snapshot(path, as_of_sequence=1)

    assert first.revision_id != second.revision_id
    assert current.by_event["event-1"].label is EventKind.IGNORE
    assert historical.by_event["event-1"].label is EventKind.ENTRY
    assert snapshot_v1.snapshot_hash == historical.snapshot_hash
    assert current.snapshot_hash != historical.snapshot_hash
    assert verify_accepted_truth_ledger(path).valid

    with pytest.raises(ValueError, match="head changed"):
        _accept(
            path,
            "event-1",
            EventKind.ADD,
            expected=first.revision_id,
            when=NOW + timedelta(minutes=2),
        )


def test_accepted_truth_ledger_detects_privileged_revision_tampering(tmp_path):
    path = tmp_path / "truth-tamper.db"
    revision = _accept(path, "event-1", EventKind.ENTRY)
    with sqlite3.connect(str(path)) as db:
        db.execute("DROP TRIGGER accepted_label_revisions_no_update")
        db.execute(
            "UPDATE accepted_label_revisions SET label='IGNORE' WHERE revision_id=?",
            (revision.revision_id,),
        )
    report = verify_accepted_truth_ledger(path)
    assert not report.valid
    assert any("revision_hash_mismatch" in failure for failure in report.failures)


def test_truth_lineage_binds_curriculum_dataset_oof_and_holdout_to_exact_revisions(tmp_path):
    path = tmp_path / "truth-lineage.db"
    labels = {
        "train-1": EventKind.ENTRY,
        "train-2": EventKind.IGNORE,
        "train-3": EventKind.ENTRY,
        "train-4": EventKind.IGNORE,
        "hold-1": EventKind.ENTRY,
        "hold-2": EventKind.IGNORE,
        "hold-3": EventKind.ENTRY,
        "hold-4": EventKind.IGNORE,
    }
    revisions = {event: _accept(path, event, label) for event, label in labels.items()}
    snapshot = build_accepted_truth_snapshot(path)

    curriculum_examples = tuple(
        CurriculumExample(
            event_id=event,
            label=labels[event],
            label_source_id=revisions[event].revision_id,
            label_confidence=0.95,
            information_value=0.8,
            residual_hotspot_score=1.0,
            epistemic=0.5,
            aleatoric=0.1,
            slices={"channel": "mff", "regime": "trend"},
        )
        for event in ("train-1", "train-2", "train-3", "train-4")
    )
    bound_curriculum = build_truth_bound_curriculum(
        curriculum_examples,
        snapshot=snapshot,
        source_dataset_fingerprint="source-dataset-v1",
        policy=HardExampleCurriculumPolicy(
            maximum_examples=4,
            maximum_label_fraction=0.75,
            maximum_slice_fraction=1.0,
        ),
    )

    schema = FeatureSchema("v1", (FeatureField("x", FeatureDType.FLOAT),))
    training = tuple(
        TrainingExample(
            sample_id=event,
            observed_ts_utc=NOW,
            features=(("x", float(index)),),
            target=labels[event].value,
        )
        for index, event in enumerate(("train-1", "train-2", "train-3", "train-4"))
    )
    bound_dataset = fingerprint_truth_bound_training_dataset(
        training,
        schema=schema,
        snapshot=snapshot,
        parent_lineage_hash=bound_curriculum.identity.lineage_hash,
    )
    policy = ContextualEnsemblePolicy(
        context_dimensions=("regime",),
        minimum_global_samples=4,
        minimum_oof_folds=2,
        minimum_context_samples=2,
        minimum_context_folds=2,
        prior_strength=5.0,
        skill_scale=2.0,
        maximum_context_weight_shift=0.25,
        maximum_contexts=5,
        minimum_holdout_samples=4,
        minimum_holdout_days=2,
        bootstrap_resamples=100,
        bootstrap_block_days=1,
    )

    def observation(event: str, index: int, holdout: bool) -> ContextualEnsembleObservation:
        truth = labels[event]
        other = EventKind.IGNORE if truth is EventKind.ENTRY else EventKind.ENTRY
        strong = {truth: 0.85, other: 0.15}
        weak = {truth: 0.65, other: 0.35}
        return ContextualEnsembleObservation(
            event_id=event,
            fold_id=f"fold-{index % 2}",
            observed_ts_utc=NOW + timedelta(days=index if holdout else index // 2),
            truth=truth,
            model_probabilities={"model-a": strong, "model-b": weak},
            slices={"regime": "trend"},
        )

    training_rows = tuple(
        observation(event, index, False)
        for index, event in enumerate(("train-1", "train-2", "train-3", "train-4"))
    )
    bound_fit = fit_truth_bound_contextual_ensemble(
        training_rows,
        snapshot=snapshot,
        policy=policy,
        parent_lineage_hash=bound_dataset.identity.lineage_hash,
    )
    holdout_rows = tuple(
        observation(event, index, True)
        for index, event in enumerate(("hold-1", "hold-2", "hold-3", "hold-4"))
    )
    bound_evaluation = evaluate_truth_bound_contextual_ensemble(
        bound_fit,
        holdout_rows,
        snapshot=snapshot,
        policy=policy,
    )
    assert bound_dataset.identity.parent_lineage_hash == bound_curriculum.identity.lineage_hash
    assert bound_fit.identity.parent_lineage_hash == bound_dataset.identity.lineage_hash
    assert bound_evaluation.identity.parent_lineage_hash == bound_fit.identity.lineage_hash
    assert bound_evaluation.identity.truth_snapshot_hash == snapshot.snapshot_hash

    revised = _accept(
        path,
        "train-1",
        EventKind.IGNORE,
        source_revision="source:train-1:v2",
        expected=revisions["train-1"].revision_id,
        when=NOW + timedelta(hours=1),
    )
    assert revised.revision_id != revisions["train-1"].revision_id
    new_snapshot = build_accepted_truth_snapshot(path)
    with pytest.raises(ValueError, match="accepted truth"):
        build_truth_bound_curriculum(
            curriculum_examples,
            snapshot=new_snapshot,
            source_dataset_fingerprint="source-dataset-v1",
        )


def _yield_outcome(
    index: int,
    *,
    action: LearningAction,
    good: bool,
    segment: str = "mff:trend",
) -> RealizedLearningOutcome:
    return RealizedLearningOutcome(
        allocation_hash=f"allocation-{index}",
        event_id=f"event-{index}",
        action=action,
        segment=segment,
        cost_units=5 if action is LearningAction.DEEP_SHADOW else 8,
        expected_utility=0.8,
        accepted_truth_revision_id=f"truth-{index}" if good else "",
        truth_snapshot_hash=f"snapshot-{index}" if good else "",
        label_usable=good,
        curriculum_fingerprint=f"curriculum-{index}" if good else "",
        evaluation_id=f"eval-{index}" if good else "",
        attribution_method=(
            YieldAttributionMethod.PAIRED_OOF_ABLATION
            if good
            else YieldAttributionMethod.LABEL_ONLY
        ),
        oof_log_loss_gain=0.03 if good else 0.0,
        oof_brier_gain=0.02 if good else 0.0,
        false_action_rate_delta=-0.01 if good else 0.0,
        wrong_action_rate_delta=-0.01 if good else 0.0,
        realized_ts_utc=NOW + timedelta(minutes=index),
    )


def test_realized_learning_yield_shrinks_sparse_segments_and_reweights_allocator(tmp_path):
    path = tmp_path / "yield.db"
    for index in range(6):
        record_realized_learning_outcome(
            path,
            _yield_outcome(index, action=LearningAction.DEEP_SHADOW, good=True),
        )
    for index in range(6, 12):
        record_realized_learning_outcome(
            path,
            _yield_outcome(index, action=LearningAction.HUMAN_REVIEW, good=False),
        )
    record_realized_learning_outcome(
        path,
        _yield_outcome(
            20,
            action=LearningAction.DEEP_SHADOW,
            good=False,
            segment="rare-segment",
        ),
    )
    policy = RealizedYieldPolicy(
        minimum_outcomes=10,
        minimum_action_outcomes=3,
        minimum_segment_outcomes=3,
        action_prior_strength=2.0,
        segment_prior_strength=10.0,
        minimum_multiplier=0.25,
        maximum_multiplier=1.75,
        maximum_observation_ratio=2.0,
    )
    calibration = calibrate_realized_learning_yield(path, policy=policy)
    assert calibration.status is YieldCalibrationStatus.QUALIFIED
    deep = calibration.multiplier_for(LearningAction.DEEP_SHADOW.value, "mff:trend")
    review = calibration.multiplier_for(LearningAction.HUMAN_REVIEW.value, "mff:trend")
    rare = calibration.multiplier_for(LearningAction.DEEP_SHADOW.value, "rare-segment")
    assert deep > review
    assert rare == calibration.multiplier_for(LearningAction.DEEP_SHADOW.value)

    assessment = LearningValueAssessment(
        "candidate",
        0.8,
        0.8,
        1.0,
        0.4,
        0.8,
        0.9,
        False,
        ("epistemic_uncertainty",),
        "mff:trend",
    )
    base_policy = InformationValuePolicy(
        minimum_light_utility=0.0,
        minimum_deep_utility=0.0,
        minimum_review_utility=0.0,
    )
    uncalibrated = allocate_learning_budget(
        (assessment,),
        budget_units=8,
        policy=base_policy,
    )
    calibrated = allocate_learning_budget(
        (assessment,),
        budget_units=8,
        policy=base_policy,
        yield_calibration=calibration,
    )
    assert uncalibrated.decisions[0].action is LearningAction.HUMAN_REVIEW
    assert calibrated.decisions[0].action is LearningAction.DEEP_SHADOW
    assert calibrated.yield_calibration_hash == calibration.calibration_hash
    assert calibrated.allocation_hash != uncalibrated.allocation_hash


def test_realized_learning_yield_tampering_blocks_calibration(tmp_path):
    path = tmp_path / "yield-tamper.db"
    for index in range(4):
        record_realized_learning_outcome(
            path,
            _yield_outcome(index, action=LearningAction.DEEP_SHADOW, good=True),
        )
    with sqlite3.connect(str(path)) as db:
        db.execute("DROP TRIGGER realized_learning_outcomes_no_update")
        db.execute(
            "UPDATE realized_learning_outcomes SET expected_utility=0.01 WHERE sequence=1"
        )
    verification = verify_realized_learning_yield_ledger(path)
    assert not verification.valid
    calibration = calibrate_realized_learning_yield(
        path,
        policy=RealizedYieldPolicy(
            minimum_outcomes=2,
            minimum_action_outcomes=2,
            minimum_segment_outcomes=2,
        ),
    )
    assert calibration.status is YieldCalibrationStatus.BLOCKED
    assert calibration.multiplier_for(LearningAction.DEEP_SHADOW.value) == 1.0
