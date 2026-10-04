import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scorpion.deployment_state_machine import (
    DeploymentStateMachine,
    DeploymentStateMachinePolicy,
    RolloutState,
)
from scorpion.fail_safe_control import NoTradeSafetyLatch
from scorpion.governance import PromotionDecision, PromotionStatus
from scorpion.hot_path_benchmark import HotPathBenchmarkPolicy
from scorpion.production_bottleneck_audit import (
    ProductionBottleneckAuditPolicy,
    audit_production_bottlenecks,
)
from scorpion.production_gate import (
    ProductionAuthorization,
    ProductionAuthorizationStatus,
    ProductionGateStatus,
    ProductionPromotionDossier,
)
from scorpion.production_readiness import (
    ProductionReadinessPolicy,
    issue_rollout_readiness_certificate,
)
from scorpion.promotion_evidence_schema import PromotionEvidenceValidationReport
from scorpion.release_guard import ReleaseRegistry, ReleaseState
from scorpion.store import Store

NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)
COMPONENT = "entry-quality-model"
POLICY_HASH = "p" * 64
MANIFEST_HASH = "m" * 64


def _promotion() -> PromotionDecision:
    return PromotionDecision(
        status=PromotionStatus.READY_FOR_OPERATOR_REVIEW,
        failures=(),
        reason="ready",
    )


def _seed_liveness(
    path: Path,
    when: datetime,
    *,
    utilization: float = 0.10,
    consumer_alive: bool = True,
) -> None:
    with sqlite3.connect(str(path)) as db:
        for component, metadata in (
            ("resilience-watchdog", {"status": "ok"}),
            (
                "discord-ingress-queue",
                {
                    "status": "healthy" if consumer_alive else "capture_only",
                    "utilization": utilization,
                    "consumer_alive": consumer_alive,
                },
            ),
        ):
            db.execute(
                """
                INSERT INTO heartbeats(component,last_seen_ts_utc,metadata_json)
                VALUES (?,?,?)
                ON CONFLICT(component) DO UPDATE SET
                    last_seen_ts_utc=excluded.last_seen_ts_utc,
                    metadata_json=excluded.metadata_json
                """,
                (component, when.isoformat(), json.dumps(metadata, sort_keys=True)),
            )


def _benchmark_policy() -> HotPathBenchmarkPolicy:
    return HotPathBenchmarkPolicy(
        minimum_samples=4,
        maximum_core_p95_us=10_000_000,
        maximum_core_p99_us=10_000_000,
        maximum_receipt_p95_us=10_000_000,
        maximum_full_path_p95_us=10_000_000,
        maximum_full_path_p99_us=10_000_000,
        maximum_db_precommit_p95_us=10_000_000,
    )


def _dossier(predecessor: str) -> ProductionPromotionDossier:
    return ProductionPromotionDossier(
        dossier_id="dossier-v26",
        component=COMPONENT,
        training_run_id="training-v26",
        shadow_release_id="shadow-v26",
        artifact_sha256="b" * 64,
        parent_release_id=predecessor,
        evidence_hash="e" * 64,
        policy_hash=POLICY_HASH,
        created_ts_utc=NOW,
        expires_ts_utc=NOW + timedelta(minutes=10),
        status=ProductionGateStatus.READY_FOR_APPROVAL,
        failures=(),
    )


def _authorization(dossier: ProductionPromotionDossier) -> ProductionAuthorization:
    return ProductionAuthorization(
        authorization_id="authorization-v26",
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


def _bootstrap(path: Path):
    Store(path)
    registry = ReleaseRegistry(path)
    predecessor = registry.register(
        component=COMPONENT,
        artifact_hash="a" * 64,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        now=NOW - timedelta(minutes=3),
    )
    registry.activate(
        predecessor.release_id,
        operator="operator-original",
        promotion=_promotion(),
        now=NOW - timedelta(minutes=2),
    )
    latch = NoTradeSafetyLatch(path)
    latch.clear_no_trade(
        COMPONENT,
        operator="operator-original",
        reason="initialize production binding",
        source_release_id=predecessor.release_id,
        now=NOW - timedelta(minutes=1),
    )
    candidate = registry.register(
        component=COMPONENT,
        artifact_hash="b" * 64,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        previous_release_id=predecessor.release_id,
        now=NOW,
    )
    machine = DeploymentStateMachine(
        path,
        policy=DeploymentStateMachinePolicy(
            minimum_guarded_soak=timedelta(seconds=1),
            maximum_audit_age=timedelta(seconds=30),
            maximum_recovery_evidence_age=timedelta(seconds=30),
        ),
    )
    _seed_liveness(path, NOW)
    return registry, latch, predecessor, candidate, machine


def _prepared(
    path: Path,
    *,
    bottleneck_policy: ProductionBottleneckAuditPolicy | None = None,
):
    registry, latch, predecessor, candidate, machine = _bootstrap(path)
    dossier = _dossier(predecessor.release_id)
    authorization = _authorization(dossier)
    validation = _validation()
    bundle = "bundle-v26"
    policy = bottleneck_policy or ProductionBottleneckAuditPolicy(
        maximum_heartbeat_age_seconds=30.0
    )
    certificate = issue_rollout_readiness_certificate(
        path,
        workspace=path.parent / f"{path.stem}-readiness",
        component=COMPONENT,
        candidate_release_id=candidate.release_id,
        dossier=dossier,
        authorization=authorization,
        evidence_validation=validation,
        evidence_bundle_hash=bundle,
        operator="operator-readiness",
        now=NOW,
        policy=ProductionReadinessPolicy(
            certificate_ttl=timedelta(minutes=2),
            benchmark_samples=4,
            require_chaos_drills=False,
        ),
        benchmark_policy=_benchmark_policy(),
        bottleneck_policy=policy,
    )
    assert certificate.ready is True
    prepared = machine.prepare(
        candidate_release_id=candidate.release_id,
        dossier=dossier,
        authorization=authorization,
        evidence_validation=validation,
        evidence_bundle_hash=bundle,
        readiness_certificate=certificate,
        now=NOW,
    )
    assert prepared.state is RolloutState.PREPARED
    return registry, latch, predecessor, candidate, machine, prepared, certificate, policy


def _audit(path: Path, when: datetime, policy: ProductionBottleneckAuditPolicy):
    return audit_production_bottlenecks(path, now=when, policy=policy)


def test_activation_allows_heartbeat_clock_progress_when_semantics_are_unchanged(tmp_path):
    path = tmp_path / "heartbeat-clock.db"
    registry, latch, _, candidate, machine, prepared, _, policy = _prepared(path)
    activation_time = NOW + timedelta(seconds=1)
    _seed_liveness(path, activation_time)
    active = machine.activate(
        prepared.rollout_id,
        operator="operator-deploy",
        current_audit=_audit(path, activation_time, policy),
        now=activation_time,
    )
    assert active.state is RolloutState.ACTIVE_GUARDED
    assert registry.get(candidate.release_id).state is ReleaseState.ACTIVE
    assert latch.state(COMPONENT).source_release_id == candidate.release_id


def test_safety_generation_churn_after_prepare_invalidates_activation_atomically(tmp_path):
    path = tmp_path / "safety-churn.db"
    registry, latch, predecessor, candidate, machine, prepared, _, policy = _prepared(path)
    latch.rebind_normal_release(
        COMPONENT,
        expected_source_release_id=predecessor.release_id,
        new_source_release_id="temporary-generation",
        operator="operator-risk",
        reason="test safety churn",
        now=NOW + timedelta(milliseconds=100),
    )
    latch.rebind_normal_release(
        COMPONENT,
        expected_source_release_id="temporary-generation",
        new_source_release_id=predecessor.release_id,
        operator="operator-risk",
        reason="restore predecessor binding",
        now=NOW + timedelta(milliseconds=200),
    )
    activation_time = NOW + timedelta(seconds=1)
    _seed_liveness(path, activation_time)
    with pytest.raises(ValueError, match="fresh rollout readiness is required"):
        machine.activate(
            prepared.rollout_id,
            operator="operator-deploy",
            current_audit=_audit(path, activation_time, policy),
            now=activation_time,
        )
    assert machine.get(prepared.rollout_id).state is RolloutState.CANCELLED
    assert registry.get(predecessor.release_id).state is ReleaseState.ACTIVE
    assert registry.get(candidate.release_id).state is ReleaseState.CANDIDATE


def test_runtime_halt_generation_churn_cannot_hide_behind_final_clear_state(tmp_path):
    path = tmp_path / "halt-churn.db"
    registry, _, predecessor, candidate, machine, prepared, _, policy = _prepared(path)
    with sqlite3.connect(str(path)) as db:
        db.execute(
            "INSERT OR REPLACE INTO runtime_flags(key,value,updated_ts_utc) VALUES ('halt',?,?)",
            (
                json.dumps({"halted": True, "reason": "test"}, sort_keys=True),
                (NOW + timedelta(milliseconds=100)).isoformat(),
            ),
        )
        db.execute(
            "UPDATE runtime_flags SET value=?,updated_ts_utc=? WHERE key='halt'",
            (
                json.dumps({"halted": False, "reason": ""}, sort_keys=True),
                (NOW + timedelta(milliseconds=200)).isoformat(),
            ),
        )
    activation_time = NOW + timedelta(seconds=1)
    _seed_liveness(path, activation_time)
    with pytest.raises(ValueError, match="fresh rollout readiness is required"):
        machine.activate(
            prepared.rollout_id,
            operator="operator-deploy",
            current_audit=_audit(path, activation_time, policy),
            now=activation_time,
        )
    assert machine.get(prepared.rollout_id).state is RolloutState.CANCELLED
    assert registry.get(predecessor.release_id).state is ReleaseState.ACTIVE
    assert registry.get(candidate.release_id).state is ReleaseState.CANDIDATE


def test_exact_readiness_policy_is_reapplied_inside_activation_transaction(tmp_path):
    path = tmp_path / "policy-replay.db"
    strict = ProductionBottleneckAuditPolicy(
        maximum_pending_review_effects=0,
        maximum_heartbeat_age_seconds=30.0,
    )
    registry, _, predecessor, candidate, machine, prepared, _, _ = _prepared(
        path,
        bottleneck_policy=strict,
    )
    with sqlite3.connect(str(path)) as db:
        db.execute(
            """
            INSERT INTO proposed_effects
            (source_event_id,kind,contract_key,generation,reason,quantity_hint,
             metadata_json,created_ts_utc,status)
            VALUES ('v26-effect','ENTRY','TEST',1,'test',1,'{}',?,'PENDING_REVIEW')
            """,
            ((NOW + timedelta(milliseconds=100)).isoformat(),),
        )
    activation_time = NOW + timedelta(seconds=1)
    _seed_liveness(path, activation_time)
    looser = ProductionBottleneckAuditPolicy(
        maximum_pending_review_effects=10,
        maximum_heartbeat_age_seconds=30.0,
    )
    caller_audit = _audit(path, activation_time, looser)
    assert caller_audit.ready_for_rollout is True
    with pytest.raises(ValueError, match="fresh rollout readiness is required"):
        machine.activate(
            prepared.rollout_id,
            operator="operator-deploy",
            current_audit=caller_audit,
            now=activation_time,
        )
    assert machine.get(prepared.rollout_id).state is RolloutState.CANCELLED
    assert registry.get(predecessor.release_id).state is ReleaseState.ACTIVE
    assert registry.get(candidate.release_id).state is ReleaseState.CANDIDATE


def test_tampered_persisted_bottleneck_policy_invalidates_activation(tmp_path):
    path = tmp_path / "policy-tamper.db"
    registry, _, predecessor, candidate, machine, prepared, _, policy = _prepared(path)
    with sqlite3.connect(str(path)) as db:
        db.execute(
            "UPDATE production_readiness_consumptions SET bottleneck_policy_json=? "
            "WHERE consumed_rollout_id=?",
            (json.dumps({"required_heartbeats": []}), prepared.rollout_id),
        )
    activation_time = NOW + timedelta(seconds=1)
    _seed_liveness(path, activation_time)
    with pytest.raises(ValueError, match="fresh rollout readiness is required"):
        machine.activate(
            prepared.rollout_id,
            operator="operator-deploy",
            current_audit=_audit(path, activation_time, policy),
            now=activation_time,
        )
    assert machine.get(prepared.rollout_id).state is RolloutState.CANCELLED
    assert registry.get(predecessor.release_id).state is ReleaseState.ACTIVE
    assert registry.get(candidate.release_id).state is ReleaseState.CANDIDATE


def test_healthy_heartbeat_metadata_drift_requires_fresh_activation_readiness(tmp_path):
    path = tmp_path / "heartbeat-metadata.db"
    registry, _, predecessor, candidate, machine, prepared, _, policy = _prepared(path)
    activation_time = NOW + timedelta(seconds=1)
    _seed_liveness(path, activation_time, utilization=0.20)
    audit = _audit(path, activation_time, policy)
    assert audit.ready_for_rollout is True
    with pytest.raises(ValueError, match="fresh rollout readiness is required"):
        machine.activate(
            prepared.rollout_id,
            operator="operator-deploy",
            current_audit=audit,
            now=activation_time,
        )
    assert machine.get(prepared.rollout_id).state is RolloutState.CANCELLED
    assert registry.get(predecessor.release_id).state is ReleaseState.ACTIVE
    assert registry.get(candidate.release_id).state is ReleaseState.CANDIDATE
