from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from scorpion.adaptive_control_audit import audit_adaptive_control_path
from scorpion.auto_trainer import TrainingRunReport, TrainingRunStatus
from scorpion.canary import CanaryReport, CanaryStatus
from scorpion.drift_retraining import ChallengerRetrainingPlan, RetrainingPlanStatus
from scorpion.fail_safe_control import NoTradeSafetyLatch, SafetyLatchState, SafetyMode
from scorpion.governance import PromotionDecision, PromotionStatus
from scorpion.kill_switch_drills import run_isolated_kill_switch_recovery_drill
from scorpion.liquidity_capacity import LiquidityCapacityReport
from scorpion.live_sizing_safety import (
    LiveRiskState,
    LiveSizingRequest,
    LiveSizingSafetyPolicy,
    evaluate_live_sizing_request,
)
from scorpion.production_gate import (
    ProductionApprovalRole,
    ProductionGateEvidence,
    ProductionGateRegistry,
    ProductionPromotionPolicy,
    build_production_promotion_dossier,
)
from scorpion.promotion_evidence_schema import (
    build_promotion_evidence_bundle,
    evaluate_formalized_production_authorization,
    validate_promotion_evidence_bundle,
)
from scorpion.shadow_lifecycle import ShadowReleaseRecord, ShadowReleaseState
from scorpion.sizing_lab import RiskSimulation, SizingEnvelope, SizingReadiness
from scorpion.sizing_stress import stress_test_live_sizing_envelope

NOW = datetime(2026, 9, 10, 16, 0, tzinfo=UTC)
DATASET = "d" * 64
ARTIFACT = "a" * 64


def _promotion_evidence() -> ProductionGateEvidence:
    training = TrainingRunReport(
        run_id="training-v22",
        model_id="entry-quality-model",
        trainer_version="trainer-v22",
        task="BINARY_CLASSIFICATION",
        dataset_fingerprint=DATASET,
        split_hash="s" * 64,
        feature_schema_hash="f" * 64,
        training_samples=600,
        validation_samples=160,
        artifact_sha256=ARTIFACT,
        artifact_loader_key="scorpion.models.entry_quality",
        artifact_model_version="model-v22",
        metrics=(),
        exact_reproducibility_verified=True,
        status=TrainingRunStatus.SHADOW_READY,
        failures=(),
        created_ts_utc=NOW - timedelta(hours=3),
    )
    plan = ChallengerRetrainingPlan(
        plan_id="plan-v22",
        parent_release_id="live-release-v21",
        dataset_fingerprint=DATASET,
        status=RetrainingPlanStatus.READY_RESEARCH_CHALLENGER,
        confirmations=("page_hinkley", "bayesian_changepoint"),
        samples_since_drift=800,
        post_drift_training_samples=600,
        validation_samples=160,
        pre_drift_anchor_samples=200,
        purge_embargo_seconds=1800.0,
        severe_drift=False,
        reasons=("corroborated_drift_retraining_ready",),
        created_ts_utc=NOW - timedelta(hours=4),
    )
    shadow = ShadowReleaseRecord(
        shadow_release_id="shadow-v22",
        component="entry-quality-model",
        training_run_id=training.run_id,
        artifact_sha256=ARTIFACT,
        parent_release_id="live-release-v21",
        state=ShadowReleaseState.ACTIVE_SHADOW,
        created_ts_utc=NOW - timedelta(hours=2),
        activated_ts_utc=NOW - timedelta(hours=1),
        quarantine_reason="",
    )
    canary = CanaryReport(
        status=CanaryStatus.READY_FOR_OPERATOR_REVIEW,
        pairs=400,
        champion_accuracy=0.95,
        challenger_accuracy=0.97,
        paired_accuracy_delta=0.02,
        delta_lower_95=0.01,
        action_escalations=0,
        contract_divergences=0,
        latency_ratio=1.04,
        failures=(),
    )
    capacity = LiquidityCapacityReport((), (), 2.0, True)
    safety = SafetyLatchState(
        component="entry-quality-model",
        mode=SafetyMode.NORMAL,
        source_release_id="live-release-v21",
        reason="healthy",
        updated_ts_utc=NOW - timedelta(minutes=1),
        updated_by="safety-monitor",
    )
    return ProductionGateEvidence(
        component="entry-quality-model",
        retraining_plan=plan,
        training=training,
        shadow=shadow,
        promotion=PromotionDecision(
            PromotionStatus.READY_FOR_OPERATOR_REVIEW,
            (),
            "all independent evidence gates passed",
        ),
        canary=canary,
        capacity=capacity,
        safety_state=safety,
        runtime_certified=True,
        drift_active=False,
        policy_fingerprint="p" * 64,
        research_manifest_hash="m" * 64,
        canary_observed_ts_utc=NOW - timedelta(minutes=1),
        capacity_observed_ts_utc=NOW - timedelta(minutes=2),
        runtime_certified_ts_utc=NOW - timedelta(minutes=1),
        observed_ts_utc=NOW,
    )


def _sizing_policy() -> LiveSizingSafetyPolicy:
    return LiveSizingSafetyPolicy(
        maximum_risk_fraction=0.02,
        capacity_reference_risk_fraction=0.01,
        maximum_daily_loss_fraction=0.03,
        maximum_drawdown_fraction=0.10,
        maximum_gross_exposure_fraction=0.50,
        maximum_cluster_exposure_fraction=0.20,
        maximum_quote_age_ms=500.0,
        maximum_decision_latency_ms=750.0,
    )


def _research_sizing() -> SizingEnvelope:
    sim = RiskSimulation(0.02, 0.0, 0.01, 1.2, 0.95, 0.04)
    return SizingEnvelope(
        segment="all",
        readiness=SizingReadiness.READY_FOR_RESEARCH,
        max_research_risk_fraction=0.02,
        selected_simulation=sim,
        simulations=(sim,),
        reason="research envelope",
    )


def _risk_state(observed: datetime) -> LiveRiskState:
    return LiveRiskState(
        observed_ts_utc=observed,
        safety_state=SafetyLatchState(
            component="entry-quality-model",
            mode=SafetyMode.NORMAL,
            source_release_id="live-release-v21",
            reason="healthy",
            updated_ts_utc=observed,
            updated_by="safety-monitor",
        ),
        research_sizing=_research_sizing(),
        capacity=LiquidityCapacityReport((), (), 2.0, True),
        daily_loss_fraction=0.005,
        current_drawdown_fraction=0.02,
        gross_exposure_fraction=0.10,
        cluster_exposure_fraction=0.05,
        quote_age_ms=50.0,
        decision_latency_ms=100.0,
        quote_consensus_ok=True,
        runtime_certified=True,
        canary_healthy=True,
        drift_active=False,
        active_release_id="live-release-v21",
    )


def test_formal_promotion_evidence_schema_is_complete_dag_and_tamper_evident():
    evidence = _promotion_evidence()
    bundle = build_promotion_evidence_bundle(evidence)
    report = validate_promotion_evidence_bundle(bundle, now=NOW + timedelta(minutes=5))
    assert report.valid is True
    assert report.records == report.required_kinds == 11

    tampered = replace(bundle, component="different-component")
    broken = validate_promotion_evidence_bundle(tampered, now=NOW + timedelta(minutes=5))
    assert broken.valid is False
    assert "promotion_evidence_bundle_hash_mismatch" in broken.failures


def test_formalized_authorization_rejects_bundle_bound_to_old_evidence(tmp_path):
    evidence = _promotion_evidence()
    policy = ProductionPromotionPolicy()
    dossier = build_production_promotion_dossier(evidence, policy=policy)
    registry = ProductionGateRegistry(tmp_path / "production.db")
    registry.persist_dossier(dossier)
    registry.approve(
        dossier.dossier_id,
        role=ProductionApprovalRole.MODEL_REVIEWER,
        operator="operator-model",
        now=NOW + timedelta(minutes=1),
    )
    registry.approve(
        dossier.dossier_id,
        role=ProductionApprovalRole.RISK_REVIEWER,
        operator="operator-risk",
        now=NOW + timedelta(minutes=2),
    )
    bundle = build_promotion_evidence_bundle(evidence, production_policy=policy)
    authorized = evaluate_formalized_production_authorization(
        registry,
        dossier,
        current_evidence=evidence,
        bundle=bundle,
        production_policy=policy,
        now=NOW + timedelta(minutes=3),
    )
    assert authorized.authorized is True

    with pytest.raises(ValueError, match="not bound to current evidence"):
        evaluate_formalized_production_authorization(
            registry,
            dossier,
            current_evidence=replace(evidence, drift_active=True),
            bundle=bundle,
            production_policy=policy,
            now=NOW + timedelta(minutes=3),
        )


def test_kill_switch_drill_proves_cold_start_restart_recovery_and_corruption(tmp_path):
    report = run_isolated_kill_switch_recovery_drill(
        tmp_path,
        component="entry-quality-model",
        operator="operator-risk",
        now=NOW,
    )
    assert report.passed is True
    assert report.cold_start_fail_closed is True
    assert report.trip_blocks_execution is True
    assert report.restart_preserves_trip is True
    assert report.integrity_verified_before_recovery is True
    assert report.operator_recovery_restores_normal is True
    assert report.restart_preserves_recovery is True
    assert report.corruption_detected is True


def test_safety_integrity_checkpoint_detects_event_deletion(tmp_path):
    path = tmp_path / "safety.db"
    latch = NoTradeSafetyLatch(path)
    latch.clear_no_trade(
        "entry-quality-model",
        operator="operator-risk",
        reason="initialize",
        now=NOW,
    )
    latch.trip_no_trade(
        "entry-quality-model",
        reason="fault",
        now=NOW + timedelta(seconds=1),
    )
    assert latch.verify_integrity("entry-quality-model").valid is True

    import sqlite3

    with sqlite3.connect(path) as db:
        db.execute(
            """
            DELETE FROM component_safety_events
            WHERE event_id=(SELECT event_id FROM component_safety_events
                            WHERE component=? ORDER BY created_ts_utc LIMIT 1)
            """,
            ("entry-quality-model",),
        )
    report = NoTradeSafetyLatch(path).verify_integrity("entry-quality-model")
    assert report.valid is False
    assert "safety_integrity_checkpoint_mismatch" in report.failures


def test_sizing_stress_envelope_is_monotonic_and_compound_failures_block():
    request = LiveSizingRequest(
        request_id="size-v22",
        component="entry-quality-model",
        release_id="live-release-v21",
        operator="operator-risk",
        requested_risk_fraction=0.01,
        created_ts_utc=NOW + timedelta(minutes=5),
    )
    state = _risk_state(NOW + timedelta(minutes=5))
    report = stress_test_live_sizing_envelope(request, state, policy=_sizing_policy())
    assert report.passed is True
    assert report.unsafe_permits == ()
    assert report.monotonicity_violations == ()
    compound = {item.scenario: item for item in report.points}
    assert compound["compound_microstructure_stress"].decision.permitted is False
    assert compound["compound_portfolio_stress"].decision.permitted is False


def test_adaptive_control_audit_binds_identity_time_and_authority(tmp_path):
    evidence = _promotion_evidence()
    policy = ProductionPromotionPolicy()
    dossier = build_production_promotion_dossier(evidence, policy=policy)
    registry = ProductionGateRegistry(tmp_path / "production.db")
    registry.persist_dossier(dossier)
    registry.approve(
        dossier.dossier_id,
        role=ProductionApprovalRole.MODEL_REVIEWER,
        operator="operator-model",
        now=NOW + timedelta(minutes=1),
    )
    registry.approve(
        dossier.dossier_id,
        role=ProductionApprovalRole.RISK_REVIEWER,
        operator="operator-risk",
        now=NOW + timedelta(minutes=2),
    )
    authorization = registry.evaluate_authorization(
        dossier,
        current_evidence=evidence,
        policy=policy,
        now=NOW + timedelta(minutes=3),
    )
    bundle = build_promotion_evidence_bundle(evidence, production_policy=policy)
    request = LiveSizingRequest(
        request_id="size-v22",
        component="entry-quality-model",
        release_id="live-release-v21",
        operator="operator-risk",
        requested_risk_fraction=0.01,
        created_ts_utc=NOW + timedelta(minutes=5),
    )
    state = _risk_state(NOW + timedelta(minutes=5))
    decision = evaluate_live_sizing_request(request, state, policy=_sizing_policy())
    audit = audit_adaptive_control_path(
        evidence=evidence,
        bundle=bundle,
        dossier=dossier,
        authorization=authorization,
        sizing_request=request,
        risk_state=state,
        sizing_decision=decision,
        now=NOW + timedelta(minutes=5),
    )
    assert audit.passed is True

    conflict_state = replace(state, active_release_id="wrong-release")
    conflict = audit_adaptive_control_path(
        evidence=evidence,
        bundle=bundle,
        dossier=dossier,
        authorization=authorization,
        sizing_request=request,
        risk_state=conflict_state,
        sizing_decision=decision,
        now=NOW + timedelta(minutes=5),
    )
    assert conflict.passed is False
    assert "sizing_request_not_bound_to_active_release" in conflict.identity_failures
