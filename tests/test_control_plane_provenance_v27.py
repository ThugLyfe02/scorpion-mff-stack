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
from scorpion.readiness_capability_journal import (
    ReadinessCapabilityEventKind,
    verify_readiness_capability_journal,
)
from scorpion.release_guard import ReleaseRegistry, ReleaseState
from scorpion.schema_contract import SCHEMA_CONTRACT_VERSION, inspect_schema
from scorpion.schema_migrations import (
    MIGRATION_ID,
    MIGRATION_SHA256,
    TARGET_SCHEMA_VERSION,
    apply_schema_migrations,
    verify_schema_migration_ledger,
)
from scorpion.store import Store

NOW = datetime(2026, 9, 10, 23, 0, tzinfo=UTC)
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


def _bottleneck_policy() -> ProductionBottleneckAuditPolicy:
    return ProductionBottleneckAuditPolicy(maximum_heartbeat_age_seconds=30.0)


def _dossier(artifact: str, predecessor: str) -> ProductionPromotionDossier:
    return ProductionPromotionDossier(
        dossier_id="dossier-v27",
        component=COMPONENT,
        training_run_id="training-v27",
        shadow_release_id="shadow-v27",
        artifact_sha256=artifact,
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
        authorization_id="auth-v27",
        dossier_id=dossier.dossier_id,
        status=ProductionAuthorizationStatus.AUTHORIZED_FOR_OPERATOR_ACTIVATION,
        approvals=(),
        authorized_until_ts_utc=dossier.expires_ts_utc,
        failures=(),
    )


def _validation() -> PromotionEvidenceValidationReport:
    return PromotionEvidenceValidationReport(valid=True, records=11, required_kinds=11, failures=())


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
    dossier = _dossier("b" * 64, predecessor.release_id)
    authorization = _authorization(dossier)
    validation = _validation()
    bundle = "bundle-v27"
    certificate = issue_rollout_readiness_certificate(
        path,
        workspace=path.parent / f"{path.stem}-ready",
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
        bottleneck_policy=_bottleneck_policy(),
    )
    return (
        registry,
        latch,
        predecessor,
        candidate,
        machine,
        dossier,
        authorization,
        validation,
        bundle,
        certificate,
    )


def _prepare(path: Path):
    items = _bootstrap(path)
    prepared = items[4].prepare(
        candidate_release_id=items[3].release_id,
        dossier=items[5],
        authorization=items[6],
        evidence_validation=items[7],
        evidence_bundle_hash=items[8],
        readiness_certificate=items[9],
        now=NOW,
    )
    return (*items, prepared)


def test_schema_migration_ledger_adopts_legacy_database_and_rejects_newer_version(tmp_path):
    path = tmp_path / "migration.db"
    Store(path)
    DeploymentStateMachine(path)
    report = verify_schema_migration_ledger(path)
    assert report.valid
    assert report.current_version == TARGET_SCHEMA_VERSION == SCHEMA_CONTRACT_VERSION
    assert report.events == 2
    assert inspect_schema(path).compatible
    with sqlite3.connect(str(path)) as db:
        state = db.execute(
            "SELECT schema_version,last_migration_id FROM schema_migration_state WHERE singleton_id=1"
        ).fetchone()
        attempt = db.execute(
            "SELECT status,migration_sha256 FROM schema_migration_attempts"
        ).fetchone()
        assert state == (TARGET_SCHEMA_VERSION, MIGRATION_ID)
        assert attempt == ("APPLIED", MIGRATION_SHA256)
        db.execute(
            "UPDATE schema_migration_state SET schema_version='2099.01.v1' WHERE singleton_id=1"
        )
    with pytest.raises(RuntimeError, match="unsupported"):
        DeploymentStateMachine(path)


def test_interrupted_migration_attempt_is_reconciled_without_reapplying_schema(tmp_path):
    path = tmp_path / "interrupted.db"
    Store(path)
    DeploymentStateMachine(path)
    with sqlite3.connect(str(path)) as db:
        db.execute(
            """
            INSERT INTO schema_migration_attempts
            (attempt_id,migration_id,from_version,to_version,migration_sha256,application_id,
             status,started_ts_utc)
            VALUES ('stale-attempt',?,?,?,?,?,'STARTED',?)
            """,
            (
                MIGRATION_ID,
                TARGET_SCHEMA_VERSION,
                TARGET_SCHEMA_VERSION,
                MIGRATION_SHA256,
                "crashed-process",
                (NOW - timedelta(seconds=1)).isoformat(),
            ),
        )
    report = apply_schema_migrations(path, application_id="recovery-process", now=NOW)
    assert report.valid
    with sqlite3.connect(str(path)) as db:
        assert db.execute(
            "SELECT status FROM schema_migration_attempts WHERE attempt_id='stale-attempt'"
        ).fetchone() == ("INTERRUPTED",)
        assert db.execute(
            "SELECT event_kind FROM schema_migration_events ORDER BY seq DESC LIMIT 1"
        ).fetchone() == ("INTERRUPTED",)


def test_migration_ledger_tampering_is_detected_and_blocks_machine_start(tmp_path):
    path = tmp_path / "migration-tamper.db"
    Store(path)
    DeploymentStateMachine(path)
    with sqlite3.connect(str(path)) as db:
        db.execute("DROP TRIGGER schema_migration_events_no_update")
        db.execute("UPDATE schema_migration_events SET application_id='tampered' WHERE seq=2")
    report = verify_schema_migration_ledger(path)
    assert not report.valid
    assert "schema_migration_event_hash_mismatch" in report.failures
    with pytest.raises(RuntimeError, match="migration ledger invalid"):
        DeploymentStateMachine(path)


def test_prepare_cross_binds_readiness_journal_and_single_snapshot(tmp_path):
    path = tmp_path / "journal.db"
    *_, certificate, prepared = _prepare(path)
    journal = verify_readiness_capability_journal(
        path,
        certificate_id=certificate.certificate_id,
        expected_latest_kind=ReadinessCapabilityEventKind.CONSUMED,
    )
    assert journal.valid
    assert journal.events == 2
    assert journal.deployment_cross_bindings_valid
    with sqlite3.connect(str(path)) as db:
        row = db.execute(
            "SELECT activation_snapshot_hash FROM production_readiness_consumptions "
            "WHERE certificate_id=?",
            (certificate.certificate_id,),
        ).fetchone()
        rollout = db.execute(
            "SELECT readiness_activation_snapshot_hash FROM deployment_rollouts WHERE rollout_id=?",
            (prepared.rollout_id,),
        ).fetchone()
    assert row is not None and row[0]
    assert rollout is not None and rollout[0] == row[0]


def test_readiness_journal_tamper_blocks_activation_and_preserves_incumbent(tmp_path):
    path = tmp_path / "journal-tamper.db"
    registry, _, predecessor, candidate, machine, *_, certificate, prepared = _prepare(path)
    with sqlite3.connect(str(path)) as db:
        db.execute("DROP TRIGGER readiness_capability_events_no_update")
        db.execute(
            "UPDATE readiness_capability_events SET deployment_event_hash='tampered' "
            "WHERE certificate_id=? AND event_kind='CONSUMED'",
            (certificate.certificate_id,),
        )
    _seed_liveness(path, NOW + timedelta(seconds=1))
    audit = audit_production_bottlenecks(
        path, now=NOW + timedelta(seconds=1), policy=_bottleneck_policy()
    )
    with pytest.raises(ValueError, match="fresh rollout readiness"):
        machine.activate(
            prepared.rollout_id,
            operator="operator-deploy",
            current_audit=audit,
            now=NOW + timedelta(seconds=1),
        )
    assert machine.get(prepared.rollout_id).state is RolloutState.CANCELLED
    assert registry.get(predecessor.release_id).state is ReleaseState.ACTIVE
    assert registry.get(candidate.release_id).state is ReleaseState.CANDIDATE


def test_single_snapshot_allows_clock_progress_but_rejects_semantic_drift(tmp_path):
    clean = tmp_path / "clock-progress.db"
    registry, _, _, candidate, machine, *_, prepared = _prepare(clean)
    _seed_liveness(clean, NOW + timedelta(seconds=1), utilization=0.10)
    audit = audit_production_bottlenecks(
        clean, now=NOW + timedelta(seconds=1), policy=_bottleneck_policy()
    )
    activated = machine.activate(
        prepared.rollout_id,
        operator="operator-deploy",
        current_audit=audit,
        now=NOW + timedelta(seconds=1),
    )
    assert activated.state is RolloutState.ACTIVE_GUARDED
    assert registry.get(candidate.release_id).state is ReleaseState.ACTIVE

    drift = tmp_path / "semantic-drift.db"
    registry2, _, predecessor2, candidate2, machine2, *_, prepared2 = _prepare(drift)
    _seed_liveness(drift, NOW + timedelta(seconds=1), utilization=0.20)
    audit2 = audit_production_bottlenecks(
        drift, now=NOW + timedelta(seconds=1), policy=_bottleneck_policy()
    )
    with pytest.raises(ValueError, match="fresh rollout readiness"):
        machine2.activate(
            prepared2.rollout_id,
            operator="operator-deploy",
            current_audit=audit2,
            now=NOW + timedelta(seconds=1),
        )
    assert machine2.get(prepared2.rollout_id).state is RolloutState.CANCELLED
    assert registry2.get(predecessor2.release_id).state is ReleaseState.ACTIVE
    assert registry2.get(candidate2.release_id).state is ReleaseState.CANDIDATE


def test_valid_migration_ledger_epoch_change_invalidates_prepared_activation(tmp_path):
    path = tmp_path / "migration-epoch.db"
    registry, _, predecessor, candidate, machine, *_, prepared = _prepare(path)
    with sqlite3.connect(str(path)) as db:
        db.execute(
            """
            INSERT INTO schema_migration_attempts
            (attempt_id,migration_id,from_version,to_version,migration_sha256,application_id,
             status,started_ts_utc)
            VALUES ('late-stale',?,?,?,?,?,'STARTED',?)
            """,
            (
                MIGRATION_ID,
                TARGET_SCHEMA_VERSION,
                TARGET_SCHEMA_VERSION,
                MIGRATION_SHA256,
                "stale-process",
                NOW.isoformat(),
            ),
        )
    assert apply_schema_migrations(
        path, application_id="reconciler", now=NOW + timedelta(milliseconds=500)
    ).valid
    _seed_liveness(path, NOW + timedelta(seconds=1))
    audit = audit_production_bottlenecks(
        path, now=NOW + timedelta(seconds=1), policy=_bottleneck_policy()
    )
    with pytest.raises(ValueError, match="fresh rollout readiness"):
        machine.activate(
            prepared.rollout_id,
            operator="operator-deploy",
            current_audit=audit,
            now=NOW + timedelta(seconds=1),
        )
    assert registry.get(predecessor.release_id).state is ReleaseState.ACTIVE
    assert registry.get(candidate.release_id).state is ReleaseState.CANDIDATE
