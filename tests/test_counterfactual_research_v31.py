import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from scorpion.counterfactual_allocator_calibration import (
    build_counterfactual_allocator_calibration,
)
from scorpion.counterfactual_learning_efficiency import (
    CounterfactualLearningPolicy,
    CounterfactualLearningReport,
    CounterfactualLearningStatus,
    CounterfactualSequenceEstimate,
    CounterfactualTreatmentEstimate,
    CrossFittedOutcomePrediction,
    evaluate_counterfactual_learning_efficiency,
)
from scorpion.information_value import (
    InformationValuePolicy,
    LearningValueAssessment,
    allocate_learning_budget,
)
from scorpion.learning_path_planner import LearningPathPolicy, plan_learning_path
from scorpion.research_breakthrough import (
    BreakthroughEvidence,
    BreakthroughPolicy,
    BreakthroughStatus,
    evaluate_breakthrough_candidate,
)
from scorpion.research_experimentation import (
    ResearchExperimentPolicy,
    ResearchIntervention,
    ResearchTreatmentBundle,
    assign_research_treatment,
    record_research_experiment_outcome,
    verify_research_experiment_ledger,
)

BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
CONTROL = ResearchTreatmentBundle((ResearchIntervention.CONTROL,), 0)
DEEP = ResearchTreatmentBundle((ResearchIntervention.DEEP_SHADOW,), 5)
REVIEW = ResearchTreatmentBundle((ResearchIntervention.HUMAN_REVIEW,), 8)


def _experiment_policy() -> ResearchExperimentPolicy:
    return ResearchExperimentPolicy(minimum_propensity=0.10)


def test_assignment_ledger_enforces_support_maturity_and_append_only(tmp_path):
    path = tmp_path / "research.db"
    with pytest.raises(ValueError, match="exploration floor"):
        assign_research_treatment(
            path,
            episode_id="bad",
            event_id="e-bad",
            step_index=0,
            regime_key="trend",
            context_fingerprint="ctx",
            allocation_hash="alloc",
            reward_contract_hash="reward-v1",
            distribution=((CONTROL, 0.99), (DEEP, 0.01)),
            random_draw=0.5,
            maturity_ts_utc=BASE + timedelta(hours=1),
            assigned_ts_utc=BASE,
            policy=_experiment_policy(),
        )
    assignment = assign_research_treatment(
        path,
        episode_id="episode-1",
        event_id="e-1",
        step_index=0,
        regime_key="trend",
        context_fingerprint="ctx",
        allocation_hash="alloc",
        reward_contract_hash="reward-v1",
        distribution=((CONTROL, 0.5), (DEEP, 0.5)),
        random_draw=0.75,
        maturity_ts_utc=BASE + timedelta(hours=1),
        assigned_ts_utc=BASE,
        policy=_experiment_policy(),
    )
    assert assignment.chosen_treatment == DEEP
    assert assignment.chosen_propensity == 0.5
    with pytest.raises(ValueError, match="before maturity"):
        record_research_experiment_outcome(
            path,
            assignment_id=assignment.assignment_id,
            realized_value=0.8,
            evidence_hash="eval-1",
            realized_ts_utc=BASE + timedelta(minutes=30),
        )
    record_research_experiment_outcome(
        path,
        assignment_id=assignment.assignment_id,
        realized_value=0.8,
        evidence_hash="eval-1",
        realized_ts_utc=BASE + timedelta(hours=2),
    )
    verification = verify_research_experiment_ledger(path)
    assert verification.valid
    assert verification.assignments == 1
    assert verification.outcomes == 1
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.DatabaseError):
        db.execute(
            "UPDATE research_assignments SET regime_key='tampered' WHERE assignment_id=?",
            (assignment.assignment_id,),
        )


def _build_counterfactual_history(tmp_path):
    path = tmp_path / "counterfactual.db"
    predictions: list[CrossFittedOutcomePrediction] = []
    distribution = ((CONTROL, 0.5), (DEEP, 0.5))
    for index in range(24):
        assigned = BASE + timedelta(hours=index)
        assignment = assign_research_treatment(
            path,
            episode_id=f"episode-{index}",
            event_id=f"event-{index}",
            step_index=0,
            regime_key="trend",
            context_fingerprint=f"ctx-{index % 3}",
            allocation_hash="alloc-v1",
            reward_contract_hash="reward-v1",
            distribution=distribution,
            random_draw=0.25 if index % 2 == 0 else 0.75,
            maturity_ts_utc=assigned + timedelta(hours=1),
            assigned_ts_utc=assigned,
            policy=_experiment_policy(),
        )
        value = 0.10 if assignment.chosen_treatment == CONTROL else 0.90
        record_research_experiment_outcome(
            path,
            assignment_id=assignment.assignment_id,
            realized_value=value,
            evidence_hash=f"evaluation-{index}",
            realized_ts_utc=assigned + timedelta(hours=2),
        )
        predictions.append(
            CrossFittedOutcomePrediction(
                assignment_id=assignment.assignment_id,
                model_fingerprint="crossfit-v1",
                predicted_values={CONTROL.treatment_key: 0.15, DEEP.treatment_key: 0.85},
            )
        )
    return path, tuple(predictions)


def test_doubly_robust_attribution_drives_allocator_from_causal_evidence(tmp_path):
    path, predictions = _build_counterfactual_history(tmp_path)
    policy = CounterfactualLearningPolicy(
        minimum_resolved_assignments=12,
        minimum_treatment_assignments=4,
        minimum_effective_sample_size=4.0,
        minimum_sequence_assignments=3,
        minimum_sequence_effective_sample_size=2.0,
        decay_half_life_days=100.0,
        regime_prior_strength=2.0,
    )
    report = evaluate_counterfactual_learning_efficiency(
        str(path),
        predictions=predictions,
        reward_contract_hash="reward-v1",
        current_regime="trend",
        as_of_ts_utc=BASE + timedelta(days=3),
        policy=policy,
    )
    assert report.status is CounterfactualLearningStatus.QUALIFIED
    estimates = {item.treatment_key: item for item in report.treatment_estimates}
    assert estimates[DEEP.treatment_key].simultaneous_lower_bound > estimates[
        CONTROL.treatment_key
    ].posterior_mean
    calibration = build_counterfactual_allocator_calibration(report)
    assert calibration.multiplier_for("DEEP_SHADOW") > 1.0

    assessment = LearningValueAssessment(
        "x",
        0.8,
        0.8,
        1.0,
        0.5,
        0.70,
        0.80,
        False,
        (),
    )
    allocation = allocate_learning_budget(
        (assessment,),
        budget_units=8,
        policy=InformationValuePolicy(
            minimum_light_utility=0.0,
            minimum_deep_utility=0.0,
            minimum_review_utility=0.0,
        ),
        yield_calibration=calibration,
    )
    assert allocation.decisions[0].action.value == "DEEP_SHADOW"
    assert allocation.yield_calibration_hash == calibration.calibration_hash


def test_counterfactual_evaluator_blocks_censored_feedback(tmp_path):
    path = tmp_path / "censored.db"
    predictions: list[CrossFittedOutcomePrediction] = []
    for index in range(10):
        assignment = assign_research_treatment(
            path,
            episode_id=f"e-{index}",
            event_id=f"event-{index}",
            step_index=0,
            regime_key="trend",
            context_fingerprint="ctx",
            allocation_hash="alloc",
            reward_contract_hash="reward-v1",
            distribution=((CONTROL, 0.5), (DEEP, 0.5)),
            random_draw=0.25 if index % 2 == 0 else 0.75,
            maturity_ts_utc=BASE + timedelta(hours=1),
            assigned_ts_utc=BASE,
            policy=_experiment_policy(),
        )
        predictions.append(
            CrossFittedOutcomePrediction(
                assignment.assignment_id,
                "crossfit-v1",
                {CONTROL.treatment_key: 0.2, DEEP.treatment_key: 0.8},
            )
        )
        if index < 4:
            record_research_experiment_outcome(
                path,
                assignment_id=assignment.assignment_id,
                realized_value=0.8,
                evidence_hash=f"eval-{index}",
                realized_ts_utc=BASE + timedelta(hours=2),
            )
    report = evaluate_counterfactual_learning_efficiency(
        str(path),
        predictions=tuple(predictions),
        reward_contract_hash="reward-v1",
        current_regime="trend",
        as_of_ts_utc=BASE + timedelta(days=1),
        policy=CounterfactualLearningPolicy(
            minimum_resolved_assignments=4,
            minimum_resolution_rate=0.8,
            minimum_treatment_assignments=2,
            minimum_effective_sample_size=1.0,
        ),
    )
    assert report.status is CounterfactualLearningStatus.INSUFFICIENT
    assert "counterfactual_outcome_resolution_rate_below_floor" in report.failures


def test_learning_path_prefers_supported_positive_sequence_synergy():
    deep_estimate = CounterfactualTreatmentEstimate(
        DEEP.treatment_key,
        5,
        20,
        15.0,
        0.6,
        0.7,
        0.5,
        0.65,
        0.05,
        0.55,
        0.11,
    )
    review_estimate = CounterfactualTreatmentEstimate(
        REVIEW.treatment_key,
        8,
        20,
        15.0,
        0.5,
        0.55,
        0.5,
        0.525,
        0.05,
        0.425,
        0.053125,
    )
    report = CounterfactualLearningReport(
        status=CounterfactualLearningStatus.QUALIFIED,
        current_regime="trend",
        reward_contract_hash="reward-v1",
        assignments=50,
        matured_assignments=50,
        resolved_assignments=50,
        resolution_rate=1.0,
        outcome_model_fingerprint="crossfit-v1",
        assignment_chain_hash="chain",
        treatment_estimates=(deep_estimate, review_estimate),
        sequence_estimates=(
            CounterfactualSequenceEstimate(
                DEEP.treatment_key,
                REVIEW.treatment_key,
                10,
                8.0,
                0.9,
                0.375,
                0.25,
            ),
        ),
        report_hash="report",
        failures=(),
    )
    plan = plan_learning_path(
        report,
        policy=LearningPathPolicy(maximum_steps=2, maximum_budget_units=13),
    )
    assert plan.best is not None
    assert tuple(step.treatment_key for step in plan.best.steps) == (
        DEEP.treatment_key,
        REVIEW.treatment_key,
    )
    assert plan.best.steps[1].transition_value > 0


def _breakthrough(index: int, *, safety: bool = False, adjusted: bool = True):
    return BreakthroughEvidence(
        hypothesis_id="h-1",
        experiment_id=f"experiment-{index}",
        dataset_fingerprint=f"dataset-{index % 2}",
        truth_snapshot_hash=f"truth-{index}",
        counterfactual_report_hash=f"cf-{index}",
        time_block=f"week-{index}",
        regime_key="trend" if index % 2 == 0 else "mean-revert",
        effect_size=0.08,
        simultaneous_lower_bound=0.02,
        selection_adjusted=adjusted,
        safety_regression=safety,
        observed_ts_utc=BASE + timedelta(days=index),
    )


def test_breakthrough_requires_independent_adjusted_safe_replication():
    evidence = tuple(_breakthrough(index) for index in range(4))
    report = evaluate_breakthrough_candidate(
        evidence,
        policy=BreakthroughPolicy(minimum_replications=3),
    )
    assert report.status is BreakthroughStatus.BREAKTHROUGH_CANDIDATE
    assert report.independent_datasets == 2
    assert report.regimes == 2

    safety_block = evaluate_breakthrough_candidate(evidence + (_breakthrough(9, safety=True),))
    assert safety_block.status is BreakthroughStatus.BLOCKED
    selection_block = evaluate_breakthrough_candidate(
        evidence + (_breakthrough(10, adjusted=False),)
    )
    assert selection_block.status is BreakthroughStatus.BLOCKED
