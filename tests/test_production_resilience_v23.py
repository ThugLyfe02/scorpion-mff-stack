import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import scorpion.fail_safe_control as fail_safe_module
from scorpion.deployment_state_machine import (
    DeploymentStateMachine,
    DeploymentStateMachinePolicy,
    RolloutHealthEvidence,
    RolloutState,
)
from scorpion.fail_safe_control import NoTradeSafetyLatch, SafetyMode
from scorpion.governance import PromotionDecision, PromotionStatus
from scorpion.production_bottleneck_audit import (
    BottleneckFinding,
    BottleneckSeverity,
    ProductionBottleneckAuditReport,
    ProductionBottleneckSnapshot,
    audit_production_bottlenecks,
)
from scorpion.production_gate import (
    ProductionAuthorization,
    ProductionAuthorizationStatus,
    ProductionGateStatus,
    ProductionPromotionDossier,
)
from scorpion.promotion_evidence_schema import PromotionEvidenceValidationReport
from scorpion.release_guard import ReleaseRegistry, ReleaseState
from scorpion.store import Store

NOW = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
COMPONENT = "entry-quality-model"
POLICY_HASH = "p" * 64
MANIFEST_HASH = "m" * 64


def _promotion() -> PromotionDecision:
    return PromotionDecision(
        status=PromotionStatus.READY_FOR_OPERATOR_REVIEW,
        failures=(),
        reason="independent evidence ready",
    )


def _seed_heartbeat(
    path: Path,
    component: str,
    *,
    consumer_alive: bool | None = None,
) -> None:
    metadata: dict[str, object] = {"status": "ok"}
    if component == "discord-ingress-queue":
        metadata = {
            "status": "healthy" if consumer_alive else "capture_only",
            "utilization": 0.05,
            "consumer_alive": bool(consumer_alive),
        }
    with sqlite3.connect(path) as db:
        db.execute(
            """
            INSERT INTO heartbeats(component,last_seen_ts_utc,metadata_json)
            VALUES (?,?,?)
            ON CONFLICT(component) DO UPDATE SET
                last_seen_ts_utc=excluded.last_seen_ts_utc,
                metadata_json=excluded.metadata_json
            """,
            (component, NOW.isoformat(), json.dumps(metadata, sort_keys=True)),
        )


def _seed_liveness(path: Path, *, consumer_alive: bool = True) -> None:
    _seed_heartbeat(path, "resilience-watchdog")
    _seed_heartbeat(path, "discord-ingress-queue", consumer_alive=consumer_alive)


def _dossier(artifact: str, parent_release_id: str) -> ProductionPromotionDossier:
    return ProductionPromotionDossier(
        dossier_id="dossier-v23-resilience",
        component=COMPONENT,
        training_run_id="training-v23-resilience",
        shadow_release_id="shadow-v23-resilience",
        artifact_sha256=artifact,
        parent_release_id=parent_release_id,
        evidence_hash="e" * 64,
        policy_hash=POLICY_HASH,
        created_ts_utc=NOW,
        expires_ts_utc=NOW + timedelta(minutes=10),
        status=ProductionGateStatus.READY_FOR_APPROVAL,
        failures=(),
    )


def _authorization(dossier: ProductionPromotionDossier) -> ProductionAuthorization:
    return ProductionAuthorization(
        authorization_id="auth-v23-resilience",
        dossier_id=dossier.dossier_id,
        status=ProductionAuthorizationStatus.AUTHORIZED_FOR_OPERATOR_ACTIVATION,
        approvals=(),
        authorized_until_ts_utc=dossier.expires_ts_utc,
        failures=(),
    )


def _validation() -> PromotionEvidenceValidationReport:
    return PromotionEvidenceValidationReport(
        valid=True,
        records=11,
        required_kinds=11,
        failures=(),
    )


def test_external_no_trade_sentinel_survives_sqlite_trip_failure(tmp_path, monkeypatch):
    path = tmp_path / "safety.db"
    latch = NoTradeSafetyLatch(path)
    latch.clear_no_trade(
        COMPONENT,
        operator="operator-a",
        reason="initialize",
        source_release_id="release-a",
        now=NOW,
    )
    real_connect = fail_safe_module.sqlite3.connect

    def broken_connect(*args, **kwargs):
        del args, kwargs
        raise sqlite3.OperationalError("injected sqlite outage")

    monkeypatch.setattr(fail_safe_module.sqlite3, "connect", broken_connect)
    with pytest.raises(sqlite3.OperationalError, match="injected sqlite outage"):
        latch.trip_no_trade(
            COMPONENT,
            reason="risk monitor trip during database outage",
            source_release_id="release-a",
            now=NOW + timedelta(seconds=1),
        )
    monkeypatch.setattr(fail_safe_module.sqlite3, "connect", real_connect)

    assert latch.emergency_sentinel_active(COMPONENT) is True
    assert latch.state(COMPONENT).mode is SafetyMode.NO_TRADE
    with pytest.raises(RuntimeError, match="fail-closed"):
        latch.assert_execution_allowed(COMPONENT)


def test_normal_release_rebind_is_compare_and_swap(tmp_path):
    path = tmp_path / "rebind.db"
    latch = NoTradeSafetyLatch(path)
    latch.clear_no_trade(
        COMPONENT,
        operator="operator-a",
        reason="initialize",
        source_release_id="release-a",
        now=NOW,
    )
    with pytest.raises(ValueError, match="source release changed"):
        latch.rebind_normal_release(
            COMPONENT,
            expected_source_release_id="stale-release",
            new_source_release_id="release-b",
            operator="operator-b",
            reason="stale compare-and-swap attempt",
            now=NOW + timedelta(seconds=1),
        )
    assert latch.state(COMPONENT).source_release_id == "release-a"

    rebound = latch.rebind_normal_release(
        COMPONENT,
        expected_source_release_id="release-a",
        new_source_release_id="release-b",
        operator="operator-b",
        reason="authorized release provenance rebind",
        now=NOW + timedelta(seconds=2),
    )
    assert rebound.execution_allowed is True
    assert rebound.source_release_id == "release-b"
    assert latch.verify_integrity(COMPONENT).valid is True


def test_bottleneck_audit_blocks_fresh_capture_only_consumer(tmp_path):
    path = tmp_path / "capture-only.db"
    Store(path)
    _seed_liveness(path, consumer_alive=False)
    report = audit_production_bottlenecks(path, now=NOW)
    assert report.snapshot.ingress_consumer_alive is False
    assert any(item.code == "ingress_consumer_not_alive" for item in report.findings)
    assert report.ready_for_rollout is False


def test_bottleneck_audit_detects_normal_safety_bound_to_wrong_active_release(tmp_path):
    path = tmp_path / "wrong-safety-release.db"
    Store(path)
    _seed_liveness(path)
    registry = ReleaseRegistry(path)
    release = registry.register(
        component=COMPONENT,
        artifact_hash="a" * 64,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        now=NOW - timedelta(minutes=2),
    )
    registry.activate(
        release.release_id,
        operator="operator-a",
        promotion=_promotion(),
        now=NOW - timedelta(minutes=1),
    )
    NoTradeSafetyLatch(path).clear_no_trade(
        COMPONENT,
        operator="operator-a",
        reason="intentionally bind wrong release for audit test",
        source_release_id="wrong-release",
        now=NOW,
    )
    report = audit_production_bottlenecks(path, now=NOW)
    assert report.snapshot.safety_state_conflicts == 1
    assert any(item.code == "active_release_safety_conflict" for item in report.findings)
    assert report.ready_for_rollout is False


def test_activation_rebinds_normal_safety_to_exact_new_release(tmp_path):
    path = tmp_path / "activation-rebind.db"
    Store(path)
    registry = ReleaseRegistry(path)
    previous = registry.register(
        component=COMPONENT,
        artifact_hash="a" * 64,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        now=NOW - timedelta(minutes=3),
    )
    registry.activate(
        previous.release_id,
        operator="operator-original",
        promotion=_promotion(),
        now=NOW - timedelta(minutes=2),
    )
    latch = NoTradeSafetyLatch(path)
    latch.clear_no_trade(
        COMPONENT,
        operator="operator-original",
        reason="initialize active release binding",
        source_release_id=previous.release_id,
        now=NOW - timedelta(minutes=1),
    )
    candidate = registry.register(
        component=COMPONENT,
        artifact_hash="b" * 64,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        previous_release_id=previous.release_id,
        now=NOW,
    )
    _seed_liveness(path)
    dossier = _dossier("b" * 64, previous.release_id)
    audit = audit_production_bottlenecks(path, now=NOW)
    machine = DeploymentStateMachine(
        path,
        policy=DeploymentStateMachinePolicy(
            minimum_guarded_soak=timedelta(seconds=1),
            maximum_audit_age=timedelta(seconds=30),
            maximum_recovery_evidence_age=timedelta(seconds=30),
        ),
    )
    prepared = machine.prepare(
        candidate_release_id=candidate.release_id,
        dossier=dossier,
        authorization=_authorization(dossier),
        evidence_validation=_validation(),
        evidence_bundle_hash="bundle-v23-resilience",
        bottleneck_audit=audit,
        now=NOW,
    )
    _seed_liveness(path)
    activated = machine.activate(
        prepared.rollout_id,
        operator="operator-deploy",
        current_audit=audit_production_bottlenecks(path, now=NOW + timedelta(seconds=1)),
        now=NOW + timedelta(seconds=1),
    )
    assert activated.state is RolloutState.ACTIVE_GUARDED
    assert registry.get(candidate.release_id).state is ReleaseState.ACTIVE
    safety = latch.state(COMPONENT)
    assert safety.execution_allowed is True
    assert safety.source_release_id == candidate.release_id
    _seed_liveness(path)
    assert audit_production_bottlenecks(
        path,
        now=NOW + timedelta(seconds=1),
    ).snapshot.safety_state_conflicts == 0


def test_rollback_authorization_is_available_under_capture_only_without_allowing_resume():
    finding = BottleneckFinding(
        code="ingress_consumer_not_alive",
        surface="ingress",
        severity=BottleneckSeverity.CRITICAL,
        observed=0.0,
        threshold=1.0,
        blocks_rollout=True,
        detail="capture-only",
    )
    snapshot = ProductionBottleneckSnapshot(
        db_bytes=0,
        wal_bytes=0,
        page_count=1,
        freelist_ratio=0.0,
        pending_raw=0,
        oldest_pending_raw_age_seconds=0.0,
        pending_review_effects=0,
        pending_deliveries=0,
        expired_delivery_leases=0,
        queue_utilization=0.0,
        pipeline_p95_us=0.0,
        db_precommit_p95_us=0.0,
        stale_required_heartbeats=(),
        active_release_conflicts=0,
        active_shadow_conflicts=0,
        active_rollout_conflicts=0,
        safety_state_conflicts=0,
        expired_ready_dossiers=0,
        ingress_consumer_alive=False,
    )
    audit = ProductionBottleneckAuditReport(
        generated_ts_utc=NOW,
        snapshot=snapshot,
        findings=(finding,),
        dominant_bottleneck=finding.code,
        ready_for_rollout=False,
        report_hash="audit-hash",
    )
    health = RolloutHealthEvidence(
        observed_ts_utc=NOW,
        bottleneck_audit=audit,
        runtime_certified=False,
        state_replay_verified=True,
        safety_integrity_valid=True,
        quote_consensus_ok=False,
        canary_healthy=False,
        drift_active=True,
    )
    assert health.rollback_authorization_ready is True
    assert health.recovery_ready is False
    assert health.rollout_ready is False
