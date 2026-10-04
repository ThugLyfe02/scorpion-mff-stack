import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

import scorpion.research_randomization_attestation as randomization_module
from scorpion.causal_allocator_calibration_v2 import build_causal_allocator_calibration_v2
from scorpion.counterfactual_learning_efficiency_v2 import (
    CausalLearningPolicyV2,
    evaluate_causal_learning_efficiency_v2,
)
from scorpion.crossfit_provenance import (
    CertifiedCrossFittedOutcomePrediction,
    CrossFittedCensoringPrediction,
    build_crossfit_provenance_manifest,
)
from scorpion.information_value import (
    InformationValuePolicy,
    LearningValueAssessment,
    allocate_learning_budget,
)
from scorpion.research_breakthrough import (
    BreakthroughEvidence,
    BreakthroughPolicy,
    evaluate_breakthrough_candidate,
)
from scorpion.research_experimentation import (
    ResearchExperimentPolicy,
    ResearchIntervention,
    ResearchTreatmentBundle,
    record_research_experiment_outcome,
)
from scorpion.research_hypothesis_memory import (
    HypothesisMemoryPolicy,
    HypothesisReuseAction,
    HypothesisStatus,
    evaluate_hypothesis_reuse,
    register_research_hypothesis,
    resolve_research_hypothesis,
    verify_hypothesis_memory,
)
from scorpion.research_insight_outbox import (
    acknowledge_research_insight,
    list_pending_research_insights,
    publish_breakthrough_candidate,
    verify_research_insight_outbox,
)
from scorpion.research_randomization_attestation import (
    assign_attested_research_treatment,
    verify_randomization_integrity,
)
from scorpion.sequential_learning_ope import (
    CertifiedSequentialQPrediction,
    SequentialOPEPolicy,
    SequentialTargetPolicyDecision,
    evaluate_sequential_research_policy,
)

BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
CONTROL = ResearchTreatmentBundle((ResearchIntervention.CONTROL,), 0)
DEEP = ResearchTreatmentBundle((ResearchIntervention.DEEP_SHADOW,), 5)
DISTRIBUTION = ((CONTROL, 0.5), (DEEP, 0.5))


def _experiment_policy() -> ResearchExperimentPolicy:
    return ResearchExperimentPolicy(minimum_propensity=0.10)


def _seed_for_side(*, experiment: str, wave: str, cluster: str, deep: bool) -> str:
    distribution_hash = randomization_module._distribution_hash(DISTRIBUTION)
    for number in range(100_000):
        seed = f"{number:064x}"
        draw = randomization_module._derive_draw(
            entropy_hex=seed,
            experiment_id=experiment,
            wave_id=wave,
            cluster_id=cluster,
            distribution_hash=distribution_hash,
        )
        if (draw >= 0.5) is deep:
            return seed
    raise AssertionError("unable to find deterministic test entropy")


def _patch_seed(monkeypatch: pytest.MonkeyPatch, seed: str) -> None:
    monkeypatch.setattr(randomization_module.secrets, "token_hex", lambda _: seed)


def test_attested_cluster_randomization_is_consistent_and_append_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "attested.db"
    experiment = "exp-1"
    wave = "wave-1"
    cluster = "cluster-1"
    seed = _seed_for_side(experiment=experiment, wave=wave, cluster=cluster, deep=True)
    _patch_seed(monkeypatch, seed)
    first = assign_attested_research_treatment(
        path,
        experiment_id=experiment,
        wave_id=wave,
        cluster_id=cluster,
        episode_id="episode-a",
        event_id="event-a",
        step_index=0,
        regime_key="trend",
        context_fingerprint="ctx-a",
        allocation_hash="alloc",
        reward_contract_hash="reward-v2",
        distribution=DISTRIBUTION,
        maturity_ts_utc=BASE + timedelta(hours=1),
        assigned_ts_utc=BASE,
        policy=_experiment_policy(),
    )
    second = assign_attested_research_treatment(
        path,
        experiment_id=experiment,
        wave_id=wave,
        cluster_id=cluster,
        episode_id="episode-b",
        event_id="event-b",
        step_index=0,
        regime_key="trend",
        context_fingerprint="ctx-b",
        allocation_hash="alloc",
        reward_contract_hash="reward-v2",
        distribution=DISTRIBUTION,
        maturity_ts_utc=BASE + timedelta(hours=1),
        assigned_ts_utc=BASE,
        policy=_experiment_policy(),
    )
    assert first.random_draw == second.random_draw
    assert first.chosen_treatment == second.chosen_treatment == DEEP
    verification = verify_randomization_integrity(path)
    assert verification.valid
    assert verification.units == 1
    assert verification.assignment_attestations == 2
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE research_randomization_units SET random_draw=0.01")


def _build_v2_history(tmp_path, monkeypatch: pytest.MonkeyPatch, *, censor_deep: bool = False):
    path = tmp_path / ("causal-censored.db" if censor_deep else "causal-v2.db")
    assignments = []
    fold_map: dict[str, str] = {}
    for index in range(24):
        experiment = "exp-v2"
        wave = f"wave-{index}"
        cluster = f"cluster-{index}"
        deep = index % 2 == 1
        seed = _seed_for_side(experiment=experiment, wave=wave, cluster=cluster, deep=deep)
        _patch_seed(monkeypatch, seed)
        assigned = BASE + timedelta(hours=index)
        assignment = assign_attested_research_treatment(
            path,
            experiment_id=experiment,
            wave_id=wave,
            cluster_id=cluster,
            episode_id=f"episode-{index}",
            event_id=f"event-{index}",
            step_index=0,
            regime_key="trend" if index < 18 else "mean-revert",
            context_fingerprint=f"ctx-{index % 4}",
            allocation_hash="alloc-v2",
            reward_contract_hash="reward-v2",
            distribution=DISTRIBUTION,
            maturity_ts_utc=assigned + timedelta(hours=1),
            assigned_ts_utc=assigned,
            policy=_experiment_policy(),
        )
        assignments.append(assignment)
        fold_map[assignment.assignment_id] = "fold-a" if index % 2 == 0 else "fold-b"
        should_resolve = (
            not censor_deep
            or assignment.chosen_treatment == CONTROL
            or index in {1, 3}
        )
        if should_resolve:
            record_research_experiment_outcome(
                path,
                assignment_id=assignment.assignment_id,
                realized_value=0.10 if assignment.chosen_treatment == CONTROL else 0.90,
                evidence_hash=f"eval-{index}",
                realized_ts_utc=assigned + timedelta(hours=2),
            )
    outcome_manifest = build_crossfit_provenance_manifest(
        model_fingerprint="outcome-v2",
        dataset_fingerprint="dataset-v2",
        truth_snapshot_hash="truth-v2",
        assignment_to_fold=fold_map,
        training_truth_snapshot_hash_by_fold={
            "fold-a": "truth-train-a",
            "fold-b": "truth-train-b",
        },
        created_ts_utc=BASE + timedelta(days=2),
    )
    censor_manifest = build_crossfit_provenance_manifest(
        model_fingerprint="censor-v2",
        dataset_fingerprint="dataset-v2",
        truth_snapshot_hash="truth-v2",
        assignment_to_fold=fold_map,
        training_truth_snapshot_hash_by_fold={
            "fold-a": "truth-train-a",
            "fold-b": "truth-train-b",
        },
        created_ts_utc=BASE + timedelta(days=2),
    )
    outcome_predictions = tuple(
        CertifiedCrossFittedOutcomePrediction(
            assignment_id=assignment.assignment_id,
            model_fingerprint="outcome-v2",
            manifest_hash=outcome_manifest.manifest_hash,
            fold_id=fold_map[assignment.assignment_id],
            predicted_values={CONTROL.treatment_key: 0.20, DEEP.treatment_key: 0.80},
        )
        for assignment in assignments
    )
    censor_predictions = tuple(
        CrossFittedCensoringPrediction(
            assignment_id=assignment.assignment_id,
            model_fingerprint="censor-v2",
            manifest_hash=censor_manifest.manifest_hash,
            fold_id=fold_map[assignment.assignment_id],
            resolution_probability=0.95 if not censor_deep else 0.70,
        )
        for assignment in assignments
    )
    return (
        path,
        tuple(assignments),
        outcome_predictions,
        outcome_manifest,
        censor_predictions,
        censor_manifest,
    )


def _v2_policy() -> CausalLearningPolicyV2:
    return CausalLearningPolicyV2(
        minimum_matured_assignments=12,
        minimum_treatment_assignments=4,
        minimum_treatment_resolution_rate=0.45,
        maximum_resolution_rate_gap=0.40,
        minimum_effective_sample_size=3.0,
        minimum_clusters=4,
        decay_half_life_days=100.0,
        regime_prior_strength=2.0,
    )


def test_v2_causal_learning_reprices_allocator_from_strict_evidence(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    path, _, outcomes, outcome_manifest, censoring, censor_manifest = _build_v2_history(
        tmp_path,
        monkeypatch,
    )
    report = evaluate_causal_learning_efficiency_v2(
        str(path),
        outcome_predictions=outcomes,
        outcome_manifest=outcome_manifest,
        censoring_predictions=censoring,
        censoring_manifest=censor_manifest,
        reward_contract_hash="reward-v2",
        current_regime="trend",
        as_of_ts_utc=BASE + timedelta(days=4),
        policy=_v2_policy(),
    )
    assert report.qualified
    estimates = {item.treatment_key: item for item in report.treatment_estimates}
    assert estimates[DEEP.treatment_key].posterior_mean > estimates[
        CONTROL.treatment_key
    ].posterior_mean
    assert report.maximum_resolution_rate_gap == 0.0
    calibration = build_causal_allocator_calibration_v2(report)
    assert calibration.multiplier_for("DEEP_SHADOW") > 1.0
    assessment = LearningValueAssessment(
        event_id="next-event",
        expected_information_value=0.8,
        learnability=0.8,
        ambiguity_discount=1.0,
        light_utility=0.4,
        deep_utility=0.7,
        review_utility=0.75,
        intrinsically_ambiguous=False,
        reasons=(),
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
    assert allocation.yield_calibration_hash == calibration.calibration_hash


def test_v2_blocks_differential_censoring_and_broken_holdout_proof(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    path, _, outcomes, outcome_manifest, censoring, censor_manifest = _build_v2_history(
        tmp_path,
        monkeypatch,
        censor_deep=True,
    )
    report = evaluate_causal_learning_efficiency_v2(
        str(path),
        outcome_predictions=outcomes,
        outcome_manifest=outcome_manifest,
        censoring_predictions=censoring,
        censoring_manifest=censor_manifest,
        reward_contract_hash="reward-v2",
        current_regime="trend",
        as_of_ts_utc=BASE + timedelta(days=4),
        policy=_v2_policy(),
    )
    assert not report.qualified
    assert "treatment_specific_resolution_rate_below_floor" in report.failures
    assert "differential_censoring_gap_above_ceiling" in report.failures

    holdout_dir = tmp_path / "holdout"
    holdout_dir.mkdir()
    good_path, _, good_predictions, good_manifest, good_censoring, good_censor_manifest = (
        _build_v2_history(holdout_dir, monkeypatch)
    )
    first = good_predictions[0]
    wrong_fold = "fold-b" if first.fold_id == "fold-a" else "fold-a"
    broken = (
        CertifiedCrossFittedOutcomePrediction(
            assignment_id=first.assignment_id,
            model_fingerprint=first.model_fingerprint,
            manifest_hash=first.manifest_hash,
            fold_id=wrong_fold,
            predicted_values=first.predicted_values,
        ),
    ) + good_predictions[1:]
    broken_report = evaluate_causal_learning_efficiency_v2(
        str(good_path),
        outcome_predictions=broken,
        outcome_manifest=good_manifest,
        censoring_predictions=good_censoring,
        censoring_manifest=good_censor_manifest,
        reward_contract_hash="reward-v2",
        current_regime="trend",
        as_of_ts_utc=BASE + timedelta(days=4),
        policy=_v2_policy(),
    )
    assert not broken_report.qualified
    assert "outcome_crossfit_holdout_proof_failed" in broken_report.failures


def test_sequential_dr_evaluates_whole_research_policy(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "sequential.db"
    assignments = []
    fold_map: dict[str, str] = {}
    for episode in range(6):
        previous = ""
        for step in range(2):
            experiment = "seq-exp"
            wave = f"episode-{episode}-step-{step}"
            cluster = f"cluster-{episode}"
            seed = _seed_for_side(
                experiment=experiment,
                wave=wave,
                cluster=cluster,
                deep=(episode + step) % 2 == 1,
            )
            _patch_seed(monkeypatch, seed)
            assigned = BASE + timedelta(hours=episode * 3 + step)
            assignment = assign_attested_research_treatment(
                path,
                experiment_id=experiment,
                wave_id=wave,
                cluster_id=cluster,
                episode_id=f"seq-{episode}",
                event_id=f"seq-event-{episode}-{step}",
                step_index=step,
                regime_key="trend",
                context_fingerprint=f"seq-ctx-{episode}-{step}",
                allocation_hash="seq-alloc",
                reward_contract_hash="seq-reward",
                distribution=DISTRIBUTION,
                maturity_ts_utc=assigned + timedelta(minutes=30),
                expected_previous_assignment_id=previous,
                assigned_ts_utc=assigned,
                policy=_experiment_policy(),
            )
            previous = assignment.assignment_id
            assignments.append(assignment)
            fold_map[assignment.assignment_id] = (
                "fold-a" if episode % 2 == 0 else "fold-b"
            )
            record_research_experiment_outcome(
                path,
                assignment_id=assignment.assignment_id,
                realized_value=0.4 if assignment.chosen_treatment == CONTROL else 0.8,
                evidence_hash=f"seq-eval-{episode}-{step}",
                realized_ts_utc=assigned + timedelta(hours=1),
            )
    manifest = build_crossfit_provenance_manifest(
        model_fingerprint="q-v1",
        dataset_fingerprint="seq-dataset",
        truth_snapshot_hash="seq-truth",
        assignment_to_fold=fold_map,
        training_truth_snapshot_hash_by_fold={
            "fold-a": "seq-train-a",
            "fold-b": "seq-train-b",
        },
        created_ts_utc=BASE + timedelta(days=2),
    )
    target = tuple(
        SequentialTargetPolicyDecision(
            assignment_id=assignment.assignment_id,
            probabilities={CONTROL.treatment_key: 0.5, DEEP.treatment_key: 0.5},
        )
        for assignment in assignments
    )
    q_predictions = tuple(
        CertifiedSequentialQPrediction(
            assignment_id=assignment.assignment_id,
            model_fingerprint="q-v1",
            manifest_hash=manifest.manifest_hash,
            fold_id=fold_map[assignment.assignment_id],
            q_values={CONTROL.treatment_key: 0.4, DEEP.treatment_key: 0.8},
            state_value=0.6,
            next_state_value=0.6 if assignment.step_index == 0 else 0.0,
        )
        for assignment in assignments
    )
    report = evaluate_sequential_research_policy(
        str(path),
        target_policy=target,
        q_predictions=q_predictions,
        q_manifest=manifest,
        reward_contract_hash="seq-reward",
        as_of_ts_utc=BASE + timedelta(days=4),
        policy=SequentialOPEPolicy(
            minimum_complete_episodes=4,
            minimum_effective_sample_size=4.0,
            minimum_clusters=4,
        ),
    )
    assert report.qualified
    assert report.complete_episodes == 6
    assert report.clusters == 6
    assert report.lower_confidence_bound > 0


def test_negative_hypothesis_memory_prevents_rediscovery_and_tracks_alpha(tmp_path):
    path = tmp_path / "hypotheses.db"
    hypothesis = register_research_hypothesis(
        path,
        family_id="family-parser-context",
        canonical_claim="  Adding Context Feature X Improves OOS Error  ",
        mechanism_scope=("parser", "context-x"),
        intervention_scope=("feature",),
        created_ts_utc=BASE,
    )
    resolve_research_hypothesis(
        path,
        hypothesis_id=hypothesis.hypothesis_id,
        status=HypothesisStatus.FALSIFIED,
        evidence_ids=("eval-1", "eval-2"),
        dataset_fingerprints=("dataset-a",),
        regime_keys=("trend",),
        time_blocks=("week-1", "week-2"),
        selection_family_id="selection-family-1",
        familywise_alpha_spent=0.03,
        reason="no replicated OOS improvement",
        resolved_ts_utc=BASE + timedelta(days=2),
    )
    same_context = evaluate_hypothesis_reuse(
        path,
        family_id="family-parser-context",
        canonical_claim="adding context feature x improves oos error",
        current_regime="trend",
        current_dataset_fingerprint="dataset-a",
        as_of_ts_utc=BASE + timedelta(days=20),
    )
    assert same_context.action is HypothesisReuseAction.DO_NOT_REPEAT
    shifted = evaluate_hypothesis_reuse(
        path,
        family_id="family-parser-context",
        canonical_claim="adding context feature x improves oos error",
        current_regime="mean-revert",
        current_dataset_fingerprint="dataset-a",
        as_of_ts_utc=BASE + timedelta(days=20),
    )
    assert shifted.action is HypothesisReuseAction.REOPEN_CONTEXT_SHIFT
    with pytest.raises(ValueError, match="already registered"):
        register_research_hypothesis(
            path,
            family_id="family-parser-context",
            canonical_claim="adding context feature x improves oos error",
            mechanism_scope=("parser",),
            intervention_scope=("feature",),
        )
    second = register_research_hypothesis(
        path,
        family_id="family-parser-context",
        canonical_claim="feature y improves calibration",
        mechanism_scope=("calibration",),
        intervention_scope=("feature-y",),
        created_ts_utc=BASE + timedelta(days=3),
    )
    with pytest.raises(ValueError, match="alpha budget exhausted"):
        resolve_research_hypothesis(
            path,
            hypothesis_id=second.hypothesis_id,
            status=HypothesisStatus.INCONCLUSIVE,
            evidence_ids=("eval-3",),
            dataset_fingerprints=("dataset-a",),
            regime_keys=("trend",),
            time_blocks=("week-3",),
            selection_family_id="selection-family-1",
            familywise_alpha_spent=0.03,
            reason="uncertain",
            policy=HypothesisMemoryPolicy(familywise_alpha_budget=0.05),
        )
    assert verify_hypothesis_memory(path).valid


def test_breakthrough_outbox_is_durable_and_acknowledge_only(tmp_path):
    path = tmp_path / "insights.db"
    evidence = tuple(
        BreakthroughEvidence(
            hypothesis_id="h-breakthrough",
            experiment_id=f"experiment-{index}",
            dataset_fingerprint=f"dataset-{index % 2}",
            truth_snapshot_hash=f"truth-{index}",
            counterfactual_report_hash=f"cf-{index}",
            time_block=f"week-{index}",
            regime_key="trend" if index % 2 == 0 else "mean-revert",
            effect_size=0.08,
            simultaneous_lower_bound=0.02,
            selection_adjusted=True,
            safety_regression=False,
            observed_ts_utc=BASE + timedelta(days=index),
        )
        for index in range(4)
    )
    report = evaluate_breakthrough_candidate(
        evidence,
        policy=BreakthroughPolicy(minimum_replications=3),
    )
    insight = publish_breakthrough_candidate(
        path,
        report=report,
        summary="Replicated causal research gain across independent datasets and regimes.",
        created_ts_utc=BASE + timedelta(days=5),
    )
    assert [item.insight_id for item in list_pending_research_insights(path)] == [
        insight.insight_id
    ]
    acknowledge_research_insight(
        path,
        insight_id=insight.insight_id,
        operator="research-reviewer",
        note="reviewed; create next research RFC",
        acknowledged_ts_utc=BASE + timedelta(days=6),
    )
    assert not list_pending_research_insights(path)
    assert verify_research_insight_outbox(path).valid
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM research_insights")
