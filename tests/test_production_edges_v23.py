import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scorpion.deployment_state_machine import (
    DeploymentStateMachine,
    DeploymentStateMachinePolicy,
    RollbackState,
    RolloutHealthEvidence,
    RolloutState,
)
from scorpion.fail_safe_control import NoTradeSafetyLatch, SafetyMode
from scorpion.production_bottleneck_audit import (
    ProductionBottleneckAuditPolicy,
    audit_production_bottlenecks,
)
from scorpion.production_chaos_drills import run_isolated_partial_outage_chaos_drills
from scorpion.production_gate import (
    ProductionAuthorization,
    ProductionAuthorizationStatus,
    ProductionGateStatus,
    ProductionPromotionDossier,
)
from scorpion.promotion_evidence_schema import PromotionEvidenceValidationReport
from scorpion.release_guard import ReleaseRegistry, ReleaseState
from scorpion.runtime import _HeartbeatPublisher
from scorpion.store import Store

NOW = datetime(2026, 9, 10, 17, 0, tzinfo=UTC)
COMPONENT = "entry-quality-model"
ARTIFACT_A = "a" * 64
ARTIFACT_B = "b" * 64
ARTIFACT_C = "c" * 64
POLICY_HASH = "p" * 64
MANIFEST_HASH = "m" * 64
EVIDENCE_HASH = "e" * 64


def _ready_promotion():
    from scorpion.governance import PromotionDecision, PromotionStatus

    return PromotionDecision(
        status=PromotionStatus.READY_FOR_OPERATOR_REVIEW,
        failures=(),
        reason="ready",
    )


def _seed_heartbeat(
    path: Path,
    component: str,
    when: datetime,
    metadata: dict[str, object],
) -> None:
    with sqlite3.connect(str(path)) as db:
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


def _seed_liveness(path: Path, when: datetime, *, utilization: float = 0.10) -> None:
    _seed_heartbeat(path, "resilience-watchdog", when, {"status": "ok"})
    _seed_heartbeat(
        path,
        "discord-ingress-queue",
        when,
        {
            "status": "healthy",
            "utilization": utilization,
            "consumer_alive": True,
        },
    )


def _audit(path: Path, when: datetime):
    _seed_liveness(path, when)
    return audit_production_bottlenecks(
        path,
        now=when,
        policy=ProductionBottleneckAuditPolicy(maximum_heartbeat_age_seconds=5.0),
    )


def _dossier(
    *,
    artifact: str,
    parent_release_id: str,
    suffix: str,
    created: datetime,
    ttl: timedelta = timedelta(minutes=30),
) -> ProductionPromotionDossier:
    return ProductionPromotionDossier(
        dossier_id=f"dossier-{suffix}",
        component=COMPONENT,
        training_run_id=f"training-{suffix}",
        shadow_release_id=f"shadow-{suffix}",
        artifact_sha256=artifact,
        parent_release_id=parent_release_id,
        evidence_hash=EVIDENCE_HASH,
        policy_hash=POLICY_HASH,
        created_ts_utc=created,
        expires_ts_utc=created + ttl,
        status=ProductionGateStatus.READY_FOR_APPROVAL,
        failures=(),
    )


def _authorization(
    dossier: ProductionPromotionDossier,
    *,
    expires: datetime | None = None,
) -> ProductionAuthorization:
    return ProductionAuthorization(
        authorization_id=f"auth-{dossier.dossier_id}",
        dossier_id=dossier.dossier_id,
        status=ProductionAuthorizationStatus.AUTHORIZED_FOR_OPERATOR_ACTIVATION,
        approvals=(),
        authorized_until_ts_utc=expires or dossier.expires_ts_utc,
        failures=(),
    )


def _validation() -> PromotionEvidenceValidationReport:
    return PromotionEvidenceValidationReport(
        valid=True,
        records=11,
        required_kinds=11,
        failures=(),
    )


def _bootstrap_releases(path: Path):
    Store(path)
    registry = ReleaseRegistry(path)
    previous = registry.register(
        component=COMPONENT,
        artifact_hash=ARTIFACT_A,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        now=NOW - timedelta(minutes=10),
    )
    registry.activate(
        previous.release_id,
        operator="operator-original",
        promotion=_ready_promotion(),
        now=NOW - timedelta(minutes=9),
    )
    latch = NoTradeSafetyLatch(path)
    latch.clear_no_trade(
        COMPONENT,
        operator="operator-original",
        reason="initialize production safety state",
        source_release_id=previous.release_id,
        now=NOW - timedelta(minutes=8),
    )
    candidate = registry.register(
        component=COMPONENT,
        artifact_hash=ARTIFACT_B,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        previous_release_id=previous.release_id,
        now=NOW - timedelta(minutes=1),
    )
    return registry, latch, previous, candidate


def _machine(path: Path) -> DeploymentStateMachine:
    return DeploymentStateMachine(
        path,
        policy=DeploymentStateMachinePolicy(
            minimum_guarded_soak=timedelta(minutes=1),
            maximum_audit_age=timedelta(seconds=30),
            maximum_recovery_evidence_age=timedelta(seconds=30),
        ),
    )


def _health(path: Path, when: datetime) -> RolloutHealthEvidence:
    audit = _audit(path, when)
    return RolloutHealthEvidence(
        observed_ts_utc=when,
        bottleneck_audit=audit,
        runtime_certified=True,
        state_replay_verified=True,
        safety_integrity_valid=True,
        quote_consensus_ok=True,
        canary_healthy=True,
        drift_active=False,
    )


def _prepare_first(path: Path):
    registry, latch, previous, candidate = _bootstrap_releases(path)
    machine = _machine(path)
    dossier = _dossier(
        artifact=ARTIFACT_B,
        parent_release_id=previous.release_id,
        suffix="v23-b",
        created=NOW,
    )
    prepared = machine.prepare(
        candidate_release_id=candidate.release_id,
        dossier=dossier,
        authorization=_authorization(dossier),
        evidence_validation=_validation(),
        evidence_bundle_hash="bundle-b",
        bottleneck_audit=_audit(path, NOW),
        now=NOW,
    )
    return registry, latch, previous, candidate, machine, dossier, prepared


def test_bottleneck_audit_blocks_pressure_liveness_and_expired_delivery(tmp_path):
    path = tmp_path / "audit.db"
    Store(path)
    _seed_liveness(path, NOW, utilization=0.95)
    report = audit_production_bottlenecks(path, now=NOW)
    assert report.ready_for_rollout is False
    assert any(item.code == "ingress_queue_pressure" for item in report.findings)

    _seed_liveness(path, NOW)
    with sqlite3.connect(str(path)) as db:
        db.execute(
            "UPDATE heartbeats SET last_seen_ts_utc=? WHERE component='resilience-watchdog'",
            ((NOW - timedelta(minutes=1)).isoformat(),),
        )
    report = audit_production_bottlenecks(path, now=NOW)
    assert "resilience-watchdog" in report.snapshot.stale_required_heartbeats
    assert report.ready_for_rollout is False

    _seed_liveness(path, NOW)
    with sqlite3.connect(str(path)) as db:
        db.execute(
            """
            CREATE TABLE execution_delivery_ledger (
                delivery_id TEXT PRIMARY KEY,event_id TEXT,effect_kind TEXT,contract_key TEXT,
                generation INTEGER,quantity INTEGER,limit_price TEXT,state TEXT,
                attempt_count INTEGER,lease_owner TEXT,lease_until_utc TEXT,
                created_ts_utc TEXT,updated_ts_utc TEXT,terminal_reason TEXT DEFAULT ''
            )
            """
        )
        db.execute(
            """
            INSERT INTO execution_delivery_ledger
            VALUES (?,?,?,?,?,?,?,'LEASED',1,?,?,?,?,?)
            """,
            (
                "d1",
                "e1",
                "ENTRY",
                "TEST",
                1,
                1,
                "1.00",
                "worker",
                (NOW - timedelta(seconds=1)).isoformat(),
                (NOW - timedelta(seconds=10)).isoformat(),
                (NOW - timedelta(seconds=10)).isoformat(),
                "",
            ),
        )
    report = audit_production_bottlenecks(path, now=NOW)
    assert report.snapshot.expired_delivery_leases == 1
    assert any(item.code == "expired_delivery_leases" for item in report.findings)
    assert report.ready_for_rollout is False


def test_rollout_stable_halt_rollback_verify_resume_is_fail_closed(tmp_path):
    path = tmp_path / "rollout.db"
    registry, latch, previous, candidate, machine, _, prepared = _prepare_first(path)
    assert prepared.state is RolloutState.PREPARED

    active = machine.activate(
        prepared.rollout_id,
        operator="operator-deploy",
        current_audit=_audit(path, NOW + timedelta(seconds=10)),
        now=NOW + timedelta(seconds=10),
    )
    assert active.state is RolloutState.ACTIVE_GUARDED
    assert registry.get(candidate.release_id).state is ReleaseState.ACTIVE

    stable_at = NOW + timedelta(minutes=2)
    stable = machine.mark_stable(
        active.rollout_id,
        operator="operator-risk",
        health=_health(path, stable_at),
        now=stable_at,
    )
    assert stable.state is RolloutState.STABLE

    halted_at = NOW + timedelta(minutes=3)
    halted = machine.halt(
        stable.rollout_id,
        reason="paired canary degradation",
        now=halted_at,
    )
    assert halted.state is RolloutState.HALTED
    assert halted.rollback_state is RollbackState.PROPOSED
    assert registry.get(candidate.release_id).state is ReleaseState.QUARANTINED
    assert latch.state(COMPONENT).mode is SafetyMode.NO_TRADE

    authorize_at = NOW + timedelta(minutes=4)
    rollback = machine.authorize_rollback(
        halted.rollout_id,
        operator="operator-model",
        health=_health(path, authorize_at),
        now=authorize_at,
    )
    assert rollback.state is RollbackState.AUTHORIZED
    assert rollback.target_release_id == previous.release_id

    with pytest.raises(ValueError, match="distinct operator"):
        machine.apply_rollback(
            halted.rollout_id,
            operator="operator-model",
            now=authorize_at + timedelta(seconds=1),
        )

    verifying = machine.apply_rollback(
        halted.rollout_id,
        operator="operator-risk",
        now=authorize_at + timedelta(seconds=2),
    )
    assert verifying.state is RolloutState.ROLLBACK_VERIFYING
    assert registry.get(previous.release_id).state is ReleaseState.ACTIVE
    assert latch.state(COMPONENT).mode is SafetyMode.NO_TRADE

    verified_at = authorize_at + timedelta(seconds=10)
    verified = machine.verify_rollback(
        halted.rollout_id,
        verifier="operator-verify",
        health=_health(path, verified_at),
        now=verified_at,
    )
    assert verified.state is RolloutState.RECOVERED_GUARDED
    assert latch.state(COMPONENT).mode is SafetyMode.NO_TRADE

    resumed_at = verified_at + timedelta(seconds=10)
    resumed = machine.resume_recovered(
        halted.rollout_id,
        operator="operator-resume",
        reason="rollback verified; resume guarded operation",
        health=_health(path, resumed_at),
        now=resumed_at,
    )
    assert resumed.state is RolloutState.ACTIVE_GUARDED
    recovered_safety = latch.state(COMPONENT)
    assert recovered_safety.mode is SafetyMode.NORMAL
    assert recovered_safety.source_release_id == previous.release_id
    assert machine.verify_integrity(halted.rollout_id).valid is True


def test_successor_rollout_supersedes_stable_rollout_only_on_activation(tmp_path):
    path = tmp_path / "successor.db"
    registry, latch, _, candidate, machine, _, first = _prepare_first(path)
    first = machine.activate(
        first.rollout_id,
        operator="operator-deploy",
        current_audit=_audit(path, NOW + timedelta(seconds=5)),
        now=NOW + timedelta(seconds=5),
    )
    first = machine.mark_stable(
        first.rollout_id,
        operator="operator-risk",
        health=_health(path, NOW + timedelta(minutes=2)),
        now=NOW + timedelta(minutes=2),
    )
    assert first.state is RolloutState.STABLE

    next_release = registry.register(
        component=COMPONENT,
        artifact_hash=ARTIFACT_C,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        previous_release_id=candidate.release_id,
        now=NOW + timedelta(minutes=3),
    )
    dossier = _dossier(
        artifact=ARTIFACT_C,
        parent_release_id=candidate.release_id,
        suffix="v23-c",
        created=NOW + timedelta(minutes=3),
    )
    prepared = machine.prepare(
        candidate_release_id=next_release.release_id,
        dossier=dossier,
        authorization=_authorization(dossier),
        evidence_validation=_validation(),
        evidence_bundle_hash="bundle-c",
        bottleneck_audit=_audit(path, NOW + timedelta(minutes=3)),
        now=NOW + timedelta(minutes=3),
    )
    assert prepared.state is RolloutState.PREPARED
    assert machine.get(first.rollout_id).state is RolloutState.STABLE
    assert latch.state(COMPONENT).mode is SafetyMode.NORMAL

    activated = machine.activate(
        prepared.rollout_id,
        operator="operator-deploy-2",
        current_audit=_audit(path, NOW + timedelta(minutes=3, seconds=5)),
        now=NOW + timedelta(minutes=3, seconds=5),
    )
    assert activated.state is RolloutState.ACTIVE_GUARDED
    assert machine.get(first.rollout_id).state is RolloutState.SUPERSEDED
    assert registry.get(next_release.release_id).state is ReleaseState.ACTIVE
    assert registry.get(candidate.release_id).state is ReleaseState.SUPERSEDED


def test_expired_or_cancelled_prepared_rollout_releases_deployment_lane(tmp_path):
    path = tmp_path / "expiry.db"
    registry, _, previous, candidate = _bootstrap_releases(path)
    machine = _machine(path)
    short = _dossier(
        artifact=ARTIFACT_B,
        parent_release_id=previous.release_id,
        suffix="short",
        created=NOW,
        ttl=timedelta(seconds=5),
    )
    prepared = machine.prepare(
        candidate_release_id=candidate.release_id,
        dossier=short,
        authorization=_authorization(short),
        evidence_validation=_validation(),
        evidence_bundle_hash="bundle-short",
        bottleneck_audit=_audit(path, NOW),
        now=NOW,
    )
    with pytest.raises(ValueError, match="expired before activation"):
        machine.activate(
            prepared.rollout_id,
            operator="operator-deploy",
            current_audit=_audit(path, NOW + timedelta(seconds=6)),
            now=NOW + timedelta(seconds=6),
        )
    assert machine.get(prepared.rollout_id).state is RolloutState.EXPIRED

    replacement = registry.register(
        component=COMPONENT,
        artifact_hash=ARTIFACT_C,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        previous_release_id=previous.release_id,
        now=NOW + timedelta(seconds=7),
    )
    dossier = _dossier(
        artifact=ARTIFACT_C,
        parent_release_id=previous.release_id,
        suffix="replacement",
        created=NOW + timedelta(seconds=7),
    )
    replacement_rollout = machine.prepare(
        candidate_release_id=replacement.release_id,
        dossier=dossier,
        authorization=_authorization(dossier),
        evidence_validation=_validation(),
        evidence_bundle_hash="bundle-replacement",
        bottleneck_audit=_audit(path, NOW + timedelta(seconds=7)),
        now=NOW + timedelta(seconds=7),
    )
    cancelled = machine.cancel(
        replacement_rollout.rollout_id,
        operator="operator-deploy",
        reason="deployment window closed",
        now=NOW + timedelta(seconds=8),
    )
    assert cancelled.state is RolloutState.CANCELLED


def test_rollout_integrity_detects_event_tampering_and_deletion(tmp_path):
    path = tmp_path / "integrity.db"
    _, _, _, _, machine, _, prepared = _prepare_first(path)
    baseline = machine.verify_integrity(prepared.rollout_id)
    assert baseline.valid is True

    with sqlite3.connect(str(path)) as db:
        db.execute(
            "UPDATE deployment_rollout_events SET reason='tampered' "
            "WHERE rollout_id=? AND seq=1",
            (prepared.rollout_id,),
        )
    assert machine.verify_integrity(prepared.rollout_id).valid is False

    path2 = tmp_path / "integrity-delete.db"
    _, _, _, _, machine2, _, prepared2 = _prepare_first(path2)
    with sqlite3.connect(str(path2)) as db:
        db.execute(
            "DELETE FROM deployment_rollout_events WHERE rollout_id=? AND seq=1",
            (prepared2.rollout_id,),
        )
    report = machine2.verify_integrity(prepared2.rollout_id)
    assert report.valid is False
    assert "deployment_integrity_checkpoint_mismatch" in report.failures


def test_automated_halt_never_quarantines_unexpected_active_release(tmp_path):
    path = tmp_path / "identity-conflict.db"
    registry, latch, previous, candidate, machine, _, prepared = _prepare_first(path)
    active = machine.activate(
        prepared.rollout_id,
        operator="operator-deploy",
        current_audit=_audit(path, NOW + timedelta(seconds=5)),
        now=NOW + timedelta(seconds=5),
    )
    assert active.state is RolloutState.ACTIVE_GUARDED

    unexpected = registry.register(
        component=COMPONENT,
        artifact_hash=ARTIFACT_C,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        previous_release_id=candidate.release_id,
        now=NOW + timedelta(seconds=6),
    )
    with sqlite3.connect(str(path)) as db:
        db.execute(
            "UPDATE component_releases SET state='SUPERSEDED' WHERE release_id=?",
            (candidate.release_id,),
        )
        db.execute(
            "UPDATE component_releases SET state='ACTIVE' WHERE release_id=?",
            (unexpected.release_id,),
        )
    failed_safe = machine.halt(
        active.rollout_id,
        reason="degradation alarm",
        now=NOW + timedelta(seconds=7),
    )
    assert failed_safe.state is RolloutState.FAILED_SAFE
    assert registry.get(unexpected.release_id).state is ReleaseState.ACTIVE
    assert registry.get(candidate.release_id).state is ReleaseState.SUPERSEDED
    assert registry.get(previous.release_id).state is ReleaseState.SUPERSEDED
    assert latch.state(COMPONENT).mode is SafetyMode.NO_TRADE


def test_partial_outage_chaos_drills_fail_closed(tmp_path):
    report = run_isolated_partial_outage_chaos_drills(
        tmp_path,
        component=COMPONENT,
        operator="operator-chaos",
        now=NOW,
    )
    assert report.passed is True
    assert len(report.cases) == 4
    assert all(case.fail_closed_observed for case in report.cases)


class _FakeHeartbeatStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def heartbeat(self, component: str, **metadata: object) -> None:
        self.calls.append((component, metadata))


def test_runtime_heartbeat_publisher_coalesces_write_churn():
    store = _FakeHeartbeatStore()
    publisher = _HeartbeatPublisher(store, minimum_interval_seconds=60.0)  # type: ignore[arg-type]

    async def exercise() -> tuple[bool, bool, bool]:
        first = await publisher.publish("ingress", status="healthy", depth=1)
        duplicate = await publisher.publish("ingress", status="healthy", depth=2)
        changed = await publisher.publish("ingress", status="capture_only", depth=2)
        return first, duplicate, changed

    first, duplicate, changed = asyncio.run(exercise())
    assert first is True
    assert duplicate is False
    assert changed is True
    assert len(store.calls) == 2
