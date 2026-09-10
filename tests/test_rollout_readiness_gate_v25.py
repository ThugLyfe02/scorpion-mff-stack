import json
import sqlite3
from dataclasses import replace
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

NOW = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)
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


def _wide_benchmark_policy() -> HotPathBenchmarkPolicy:
    return HotPathBenchmarkPolicy(
        minimum_samples=4,
        maximum_core_p95_us=10_000_000,
        maximum_core_p99_us=10_000_000,
        maximum_receipt_p95_us=10_000_000,
        maximum_full_path_p95_us=10_000_000,
        maximum_full_path_p99_us=10_000_000,
        maximum_db_precommit_p95_us=10_000_000,
    )


def _dossier(
    artifact: str,
    predecessor: str,
    *,
    suffix: str = "v25",
    ttl: timedelta = timedelta(minutes=10),
) -> ProductionPromotionDossier:
    return ProductionPromotionDossier(
        dossier_id=f"dossier-{suffix}",
        component=COMPONENT,
        training_run_id=f"training-{suffix}",
        shadow_release_id=f"shadow-{suffix}",
        artifact_sha256=artifact,
        parent_release_id=predecessor,
        evidence_hash=(suffix[0] if suffix else "e") * 64,
        policy_hash=POLICY_HASH,
        created_ts_utc=NOW,
        expires_ts_utc=NOW + ttl,
        status=ProductionGateStatus.READY_FOR_APPROVAL,
        failures=(),
    )


def _authorization(dossier: ProductionPromotionDossier) -> ProductionAuthorization:
    return ProductionAuthorization(
        authorization_id=f"auth-{dossier.dossier_id}",
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


def _issue(
    path: Path,
    *,
    candidate_id: str,
    dossier: ProductionPromotionDossier,
    authorization: ProductionAuthorization,
    validation: PromotionEvidenceValidationReport,
    bundle: str,
    ttl: timedelta = timedelta(minutes=2),
):
    _seed_liveness(path, NOW)
    return issue_rollout_readiness_certificate(
        path,
        workspace=path.parent / f"{path.stem}-{candidate_id[:8]}-ready",
        component=COMPONENT,
        candidate_release_id=candidate_id,
        dossier=dossier,
        authorization=authorization,
        evidence_validation=validation,
        evidence_bundle_hash=bundle,
        operator="operator-readiness",
        now=NOW,
        policy=ProductionReadinessPolicy(
            certificate_ttl=ttl,
            benchmark_samples=4,
            require_chaos_drills=False,
        ),
        benchmark_policy=_wide_benchmark_policy(),
        bottleneck_policy=ProductionBottleneckAuditPolicy(
            maximum_heartbeat_age_seconds=30.0
        ),
    )


def _prepare_inputs(path: Path):
    registry, latch, predecessor, candidate, machine = _bootstrap(path)
    dossier = _dossier("b" * 64, predecessor.release_id)
    authorization = _authorization(dossier)
    validation = _validation()
    bundle = "bundle-v25"
    certificate = _issue(
        path,
        candidate_id=candidate.release_id,
        dossier=dossier,
        authorization=authorization,
        validation=validation,
        bundle=bundle,
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


def test_readiness_capability_is_consumed_atomically_and_cannot_be_replayed(tmp_path):
    path = tmp_path / "single-use.db"
    (
        _,
        _,
        _,
        candidate,
        machine,
        dossier,
        authorization,
        validation,
        bundle,
        certificate,
    ) = _prepare_inputs(path)
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
    with sqlite3.connect(str(path)) as db:
        consumption = db.execute(
            "SELECT consumed_rollout_id FROM production_readiness_consumptions "
            "WHERE certificate_id=?",
            (certificate.certificate_id,),
        ).fetchone()
        bound = db.execute(
            "SELECT readiness_certificate_id FROM deployment_rollouts WHERE rollout_id=?",
            (prepared.rollout_id,),
        ).fetchone()
    assert consumption is not None and consumption[0] == prepared.rollout_id
    assert bound is not None and bound[0] == certificate.certificate_id

    machine.cancel(
        prepared.rollout_id,
        operator="operator-deploy",
        reason="cancel to prove capability is still spent",
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="consumed"):
        machine.prepare(
            candidate_release_id=candidate.release_id,
            dossier=dossier,
            authorization=authorization,
            evidence_validation=validation,
            evidence_bundle_hash=bundle,
            readiness_certificate=certificate,
            now=NOW + timedelta(seconds=1),
        )


def test_legacy_core_prepare_is_blocked_by_sqlite_trigger(tmp_path):
    path = tmp_path / "legacy-bypass.db"
    (
        _,
        _,
        _,
        candidate,
        machine,
        dossier,
        authorization,
        validation,
        bundle,
        _,
    ) = _prepare_inputs(path)
    legacy_class = DeploymentStateMachine.__mro__[1]
    legacy = legacy_class(path, policy=machine.policy)
    audit = audit_production_bottlenecks(path, now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="rollout_readiness_required"):
        legacy.prepare(
            candidate_release_id=candidate.release_id,
            dossier=dossier,
            authorization=authorization,
            evidence_validation=validation,
            evidence_bundle_hash=bundle,
            bottleneck_audit=audit,
            now=NOW,
        )


def test_tampered_or_swapped_readiness_bindings_fail_before_prepare(tmp_path):
    path = tmp_path / "binding-swap.db"
    (
        registry,
        _,
        predecessor,
        candidate,
        machine,
        dossier,
        authorization,
        validation,
        bundle,
        certificate,
    ) = _prepare_inputs(path)
    tampered = replace(certificate, control_state_hash="f" * 64)
    with pytest.raises(ValueError, match="integrity_mismatch"):
        machine.prepare(
            candidate_release_id=candidate.release_id,
            dossier=dossier,
            authorization=authorization,
            evidence_validation=validation,
            evidence_bundle_hash=bundle,
            readiness_certificate=tampered,
            now=NOW,
        )

    candidate_c = registry.register(
        component=COMPONENT,
        artifact_hash="c" * 64,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        previous_release_id=predecessor.release_id,
        now=NOW,
    )
    with pytest.raises(ValueError, match="readiness_candidate_mismatch"):
        machine.prepare(
            candidate_release_id=candidate_c.release_id,
            dossier=dossier,
            authorization=authorization,
            evidence_validation=validation,
            evidence_bundle_hash=bundle,
            readiness_certificate=certificate,
            now=NOW,
        )
    with pytest.raises(ValueError, match="readiness_evidence_bundle_mismatch"):
        machine.prepare(
            candidate_release_id=candidate.release_id,
            dossier=dossier,
            authorization=authorization,
            evidence_validation=validation,
            evidence_bundle_hash="different-bundle",
            readiness_certificate=certificate,
            now=NOW,
        )


def test_safety_generation_churn_invalidates_even_when_visible_binding_returns_normal(tmp_path):
    path = tmp_path / "safety-generation.db"
    (
        _,
        latch,
        predecessor,
        candidate,
        machine,
        dossier,
        authorization,
        validation,
        bundle,
        certificate,
    ) = _prepare_inputs(path)
    latch.rebind_normal_release(
        COMPONENT,
        expected_source_release_id=predecessor.release_id,
        new_source_release_id="temporary-safety-generation",
        operator="operator-risk",
        reason="inject safety generation churn",
        now=NOW + timedelta(milliseconds=100),
    )
    latch.rebind_normal_release(
        COMPONENT,
        expected_source_release_id="temporary-safety-generation",
        new_source_release_id=predecessor.release_id,
        operator="operator-risk",
        reason="restore visible predecessor binding",
        now=NOW + timedelta(milliseconds=200),
    )
    assert latch.state(COMPONENT).source_release_id == predecessor.release_id
    with pytest.raises(ValueError, match="control-state epoch changed"):
        machine.prepare(
            candidate_release_id=candidate.release_id,
            dossier=dossier,
            authorization=authorization,
            evidence_validation=validation,
            evidence_bundle_hash=bundle,
            readiness_certificate=certificate,
            now=NOW + timedelta(seconds=1),
        )


def test_healthy_but_changed_bottleneck_state_requires_fresh_readiness(tmp_path):
    path = tmp_path / "bottleneck-change.db"
    (
        _,
        _,
        _,
        candidate,
        machine,
        dossier,
        authorization,
        validation,
        bundle,
        certificate,
    ) = _prepare_inputs(path)
    _seed_liveness(path, NOW + timedelta(seconds=1), utilization=0.20)
    with pytest.raises(ValueError, match="operator production state changed"):
        machine.prepare(
            candidate_release_id=candidate.release_id,
            dossier=dossier,
            authorization=authorization,
            evidence_validation=validation,
            evidence_bundle_hash=bundle,
            readiness_certificate=certificate,
            now=NOW + timedelta(seconds=1),
        )


def test_active_predecessor_change_invalidates_readiness(tmp_path):
    path = tmp_path / "predecessor-change.db"
    (
        registry,
        _,
        predecessor,
        candidate,
        machine,
        dossier,
        authorization,
        validation,
        bundle,
        certificate,
    ) = _prepare_inputs(path)
    unexpected = registry.register(
        component=COMPONENT,
        artifact_hash="d" * 64,
        policy_fingerprint=POLICY_HASH,
        research_manifest_hash=MANIFEST_HASH,
        previous_release_id=predecessor.release_id,
        now=NOW + timedelta(seconds=1),
    )
    with sqlite3.connect(str(path)) as db:
        db.execute(
            "UPDATE component_releases SET state='SUPERSEDED' WHERE release_id=?",
            (predecessor.release_id,),
        )
        db.execute(
            "UPDATE component_releases SET state='ACTIVE' WHERE release_id=?",
            (unexpected.release_id,),
        )
    with pytest.raises(ValueError, match="operator observability is no longer rollout-ready"):
        machine.prepare(
            candidate_release_id=candidate.release_id,
            dossier=dossier,
            authorization=authorization,
            evidence_validation=validation,
            evidence_bundle_hash=bundle,
            readiness_certificate=certificate,
            now=NOW + timedelta(seconds=1),
        )


def test_readiness_expiry_blocks_prepare_and_activation(tmp_path):
    path = tmp_path / "expiry.db"
    registry, _, predecessor, candidate, machine = _bootstrap(path)
    dossier = _dossier("b" * 64, predecessor.release_id, suffix="expiry")
    authorization = _authorization(dossier)
    validation = _validation()
    bundle = "bundle-expiry"
    expired_before_prepare = _issue(
        path,
        candidate_id=candidate.release_id,
        dossier=dossier,
        authorization=authorization,
        validation=validation,
        bundle=bundle,
        ttl=timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="readiness_certificate_expired"):
        machine.prepare(
            candidate_release_id=candidate.release_id,
            dossier=dossier,
            authorization=authorization,
            evidence_validation=validation,
            evidence_bundle_hash=bundle,
            readiness_certificate=expired_before_prepare,
            now=NOW + timedelta(seconds=2),
        )

    fresh = _issue(
        path,
        candidate_id=candidate.release_id,
        dossier=dossier,
        authorization=authorization,
        validation=validation,
        bundle=bundle,
        ttl=timedelta(seconds=2),
    )
    prepared = machine.prepare(
        candidate_release_id=candidate.release_id,
        dossier=dossier,
        authorization=authorization,
        evidence_validation=validation,
        evidence_bundle_hash=bundle,
        readiness_certificate=fresh,
        now=NOW,
    )
    _seed_liveness(path, NOW + timedelta(seconds=3))
    with pytest.raises(ValueError, match="readiness capability expired before activation"):
        machine.activate(
            prepared.rollout_id,
            operator="operator-deploy",
            current_audit=audit_production_bottlenecks(
                path,
                now=NOW + timedelta(seconds=3),
                policy=ProductionBottleneckAuditPolicy(
                    maximum_heartbeat_age_seconds=30.0
                ),
            ),
            now=NOW + timedelta(seconds=3),
        )
    assert machine.get(prepared.rollout_id).state is RolloutState.EXPIRED
    assert registry.get(candidate.release_id).state is ReleaseState.CANDIDATE
