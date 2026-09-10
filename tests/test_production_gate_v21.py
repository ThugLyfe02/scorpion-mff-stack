import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from scorpion.auto_trainer import TrainingRunReport, TrainingRunStatus
from scorpion.canary import CanaryReport, CanaryStatus
from scorpion.drift_retraining import ChallengerRetrainingPlan, RetrainingPlanStatus
from scorpion.fail_safe_control import SafetyLatchState, SafetyMode
from scorpion.governance import PromotionDecision, PromotionStatus
from scorpion.liquidity_capacity import LiquidityCapacityReport
from scorpion.production_gate import (
    ProductionApprovalRole,
    ProductionAuthorizationStatus,
    ProductionGateRegistry,
    ProductionGateStatus,
    ProductionPromotionPolicy,
    ProductionGateEvidence,
    build_production_promotion_dossier,
)
from scorpion.shadow_lifecycle import ShadowReleaseRecord, ShadowReleaseState

NOW = datetime(2026, 9, 10, 16, 0, tzinfo=UTC)
DATASET = "d" * 64
ARTIFACT = "a" * 64


def _training() -> TrainingRunReport:
    return TrainingRunReport(
        run_id="training-run-v21",
        model_id="entry-quality-model",
        trainer_version="trainer-v2",
        task="BINARY_CLASSIFICATION",
        dataset_fingerprint=DATASET,
        split_hash="s" * 64,
        feature_schema_hash="f" * 64,
        training_samples=500,
        validation_samples=120,
        artifact_sha256=ARTIFACT,
        artifact_loader_key="scorpion.models.entry_quality",
        artifact_model_version="model-v21",
        metrics=(),
        exact_reproducibility_verified=True,
        status=TrainingRunStatus.SHADOW_READY,
        failures=(),
        created_ts_utc=NOW - timedelta(hours=3),
    )


def _plan() -> ChallengerRetrainingPlan:
    return ChallengerRetrainingPlan(
        plan_id="plan-v21",
        parent_release_id="live-parent-v20",
        dataset_fingerprint=DATASET,
        status=RetrainingPlanStatus.READY_RESEARCH_CHALLENGER,
        confirmations=("page_hinkley", "bayesian_changepoint"),
        samples_since_drift=700,
        post_drift_training_samples=500,
        validation_samples=120,
        pre_drift_anchor_samples=200,
        purge_embargo_seconds=1800.0,
        severe_drift=False,
        reasons=("corroborated_drift_retraining_ready",),
        created_ts_utc=NOW - timedelta(hours=4),
    )


def _shadow() -> ShadowReleaseRecord:
    return ShadowReleaseRecord(
        shadow_release_id="shadow-v21",
        component="entry-quality-model",
        training_run_id="training-run-v21",
        artifact_sha256=ARTIFACT,
        parent_release_id="live-parent-v20",
        state=ShadowReleaseState.ACTIVE_SHADOW,
        created_ts_utc=NOW - timedelta(hours=2),
        activated_ts_utc=NOW - timedelta(hours=1),
        quarantine_reason="",
    )


def _promotion() -> PromotionDecision:
    return PromotionDecision(
        status=PromotionStatus.READY_FOR_OPERATOR_REVIEW,
        failures=(),
        reason="all independent evidence gates passed",
    )


def _canary() -> CanaryReport:
    return CanaryReport(
        status=CanaryStatus.READY_FOR_OPERATOR_REVIEW,
        pairs=300,
        champion_accuracy=0.95,
        challenger_accuracy=0.97,
        paired_accuracy_delta=0.02,
        delta_lower_95=0.01,
        action_escalations=0,
        contract_divergences=0,
        latency_ratio=1.05,
        failures=(),
    )


def _capacity() -> LiquidityCapacityReport:
    return LiquidityCapacityReport(
        scenarios=(),
        frontier=(),
        max_robust_clip_multiplier=2.0,
        robust=True,
    )


def _safety(mode: SafetyMode = SafetyMode.NORMAL) -> SafetyLatchState:
    return SafetyLatchState(
        component="entry-quality-model",
        mode=mode,
        source_release_id="live-parent-v20",
        reason="healthy" if mode is SafetyMode.NORMAL else "fault injected",
        updated_ts_utc=NOW - timedelta(minutes=1),
        updated_by="safety-monitor",
    )


def _evidence() -> ProductionGateEvidence:
    return ProductionGateEvidence(
        component="entry-quality-model",
        retraining_plan=_plan(),
        training=_training(),
        shadow=_shadow(),
        promotion=_promotion(),
        canary=_canary(),
        capacity=_capacity(),
        safety_state=_safety(),
        runtime_certified=True,
        drift_active=False,
        policy_fingerprint="p" * 64,
        research_manifest_hash="m" * 64,
        canary_observed_ts_utc=NOW - timedelta(minutes=1),
        capacity_observed_ts_utc=NOW - timedelta(minutes=2),
        runtime_certified_ts_utc=NOW - timedelta(minutes=1),
        observed_ts_utc=NOW,
    )


def test_production_gate_requires_distinct_model_and_risk_approvers(tmp_path):
    evidence = _evidence()
    policy = ProductionPromotionPolicy()
    dossier = build_production_promotion_dossier(evidence, policy=policy)
    assert dossier.status is ProductionGateStatus.READY_FOR_APPROVAL

    registry = ProductionGateRegistry(tmp_path / "promotion.db")
    assert registry.persist_dossier(dossier) is True
    pending = registry.evaluate_authorization(
        dossier,
        current_evidence=evidence,
        policy=policy,
        now=NOW + timedelta(minutes=1),
    )
    assert pending.status is ProductionAuthorizationStatus.PENDING_APPROVALS

    registry.approve(
        dossier.dossier_id,
        role=ProductionApprovalRole.MODEL_REVIEWER,
        operator="operator-model",
        now=NOW + timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="multiple production approval roles"):
        registry.approve(
            dossier.dossier_id,
            role=ProductionApprovalRole.RISK_REVIEWER,
            operator="operator-model",
            now=NOW + timedelta(minutes=3),
        )
    registry.approve(
        dossier.dossier_id,
        role=ProductionApprovalRole.RISK_REVIEWER,
        operator="operator-risk",
        now=NOW + timedelta(minutes=3),
    )
    authorized = registry.evaluate_authorization(
        dossier,
        current_evidence=evidence,
        policy=policy,
        now=NOW + timedelta(minutes=4),
    )
    assert authorized.authorized is True
    assert authorized.authorization_id


def test_post_approval_evidence_change_invalidates_authorization(tmp_path):
    evidence = _evidence()
    policy = ProductionPromotionPolicy()
    dossier = build_production_promotion_dossier(evidence, policy=policy)
    registry = ProductionGateRegistry(tmp_path / "promotion.db")
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

    drifted = replace(evidence, drift_active=True)
    decision = registry.evaluate_authorization(
        dossier,
        current_evidence=drifted,
        policy=policy,
        now=NOW + timedelta(minutes=3),
    )
    assert decision.status is ProductionAuthorizationStatus.BLOCKED
    assert "production_evidence_changed_since_dossier" in decision.failures


def test_production_gate_blocks_stale_or_fail_closed_evidence():
    evidence = _evidence()
    stale_canary = replace(
        evidence,
        canary_observed_ts_utc=NOW - timedelta(hours=1),
        safety_state=_safety(SafetyMode.NO_TRADE),
    )
    dossier = build_production_promotion_dossier(stale_canary)
    assert dossier.status is ProductionGateStatus.BLOCKED
    assert "canary_evidence_stale" in dossier.failures
    assert "component_fail_closed_no_trade" in dossier.failures


def test_canary_must_postdate_shadow_activation():
    evidence = replace(
        _evidence(),
        canary_observed_ts_utc=NOW - timedelta(hours=2),
    )
    dossier = build_production_promotion_dossier(
        evidence,
        policy=ProductionPromotionPolicy(maximum_live_evidence_age=timedelta(hours=3)),
    )
    assert "canary_evidence_predates_shadow_activation" in dossier.failures


def test_adversarial_dossier_tampering_is_detected_before_approval(tmp_path):
    evidence = _evidence()
    dossier = build_production_promotion_dossier(evidence)
    path = tmp_path / "promotion.db"
    registry = ProductionGateRegistry(path)
    registry.persist_dossier(dossier)

    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE production_promotion_dossiers SET dossier_json=? WHERE dossier_id=?",
            ('{"tampered":true}', dossier.dossier_id),
        )
        db.commit()

    with pytest.raises(ValueError, match="integrity mismatch"):
        registry.approve(
            dossier.dossier_id,
            role=ProductionApprovalRole.MODEL_REVIEWER,
            operator="operator-model",
            now=NOW + timedelta(minutes=1),
        )


def test_expired_dossier_cannot_be_authorized(tmp_path):
    evidence = _evidence()
    policy = ProductionPromotionPolicy(dossier_ttl=timedelta(minutes=5))
    dossier = build_production_promotion_dossier(evidence, policy=policy)
    registry = ProductionGateRegistry(tmp_path / "promotion.db")
    registry.persist_dossier(dossier)
    decision = registry.evaluate_authorization(
        dossier,
        current_evidence=evidence,
        policy=policy,
        now=NOW + timedelta(minutes=6),
    )
    assert decision.status is ProductionAuthorizationStatus.EXPIRED
