from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from . import _deployment_state_machine_core as _core
from ._deployment_core_alias import CoreDeploymentStateMachine
from . import _deployment_state_machine_v26 as _legacy
from .activation_snapshot import certify_activation_snapshot
from .production_bottleneck_audit import ProductionBottleneckAuditReport
from .production_gate import ProductionAuthorization, ProductionPromotionDossier
from .production_readiness import (
    RolloutReadinessCertificate,
    activation_bottleneck_fingerprint,
    bottleneck_policy_fingerprint,
    bottleneck_policy_json,
    parse_bottleneck_policy_json,
)
from .promotion_evidence_schema import PromotionEvidenceValidationReport
from .readiness_capability_journal import (
    ReadinessCapabilityEventKind,
    append_readiness_capability_event,
    capability_payload_sha256,
    ensure_readiness_capability_issued,
    verify_readiness_capability_journal_connection,
)
from .schema_migrations import apply_schema_migrations

DeploymentStateMachinePolicy = _legacy.DeploymentStateMachinePolicy
RollbackRecord = _legacy.RollbackRecord
RollbackState = _legacy.RollbackState
RolloutHealthEvidence = _legacy.RolloutHealthEvidence
RolloutIntegrityReport = _legacy.RolloutIntegrityReport
RolloutRecord = _legacy.RolloutRecord
RolloutState = _legacy.RolloutState

__all__ = [
    "DeploymentStateMachine",
    "DeploymentStateMachinePolicy",
    "RollbackRecord",
    "RollbackState",
    "RolloutHealthEvidence",
    "RolloutIntegrityReport",
    "RolloutRecord",
    "RolloutState",
]


def _deployment_event_hash(db: sqlite3.Connection, rollout_id: str, to_state: str) -> str:
    row = db.execute(
        "SELECT event_hash FROM deployment_rollout_events "
        "WHERE rollout_id=? AND to_state=? ORDER BY seq DESC LIMIT 1",
        (rollout_id, to_state),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"deployment event missing:{rollout_id}:{to_state}")
    return str(row[0])


class DeploymentStateMachine(_legacy.DeploymentStateMachine):
    """Migration-managed, journaled, single-snapshot production deployment control."""

    def __init__(
        self,
        path: str | Path,
        *,
        policy: DeploymentStateMachinePolicy | None = None,
    ) -> None:
        CoreDeploymentStateMachine.__init__(self, path, policy=policy)
        report = apply_schema_migrations(
            path,
            application_id="deployment-state-machine/0.27.0",
        )
        if not report.valid:
            raise RuntimeError("production schema migration provenance is invalid")
        with self._connect() as db:
            _legacy._ensure_readiness_enforcement(db)

    def prepare(
        self,
        *,
        candidate_release_id: str,
        dossier: ProductionPromotionDossier,
        authorization: ProductionAuthorization,
        evidence_validation: PromotionEvidenceValidationReport,
        evidence_bundle_hash: str,
        bottleneck_audit: ProductionBottleneckAuditReport | None = None,
        now: datetime | None = None,
        readiness_certificate: RolloutReadinessCertificate | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        if readiness_certificate is None:
            raise ValueError("rollout readiness certificate is required")
        prepared = super().prepare(
            candidate_release_id=candidate_release_id,
            dossier=dossier,
            authorization=authorization,
            evidence_validation=evidence_validation,
            evidence_bundle_hash=evidence_bundle_hash,
            bottleneck_audit=bottleneck_audit,
            now=timestamp,
            readiness_certificate=readiness_certificate,
        )
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                payload_hash = capability_payload_sha256(
                    certificate_id=readiness_certificate.certificate_id,
                    component=readiness_certificate.component,
                    candidate_release_id=candidate_release_id,
                    expires_ts_utc=readiness_certificate.expires_ts_utc,
                )
                ensure_readiness_capability_issued(
                    db,
                    certificate_id=readiness_certificate.certificate_id,
                    component=readiness_certificate.component,
                    candidate_release_id=candidate_release_id,
                    expires_ts_utc=readiness_certificate.expires_ts_utc,
                    actor="production-readiness-gate",
                    issued_ts_utc=readiness_certificate.generated_ts_utc,
                )
                append_readiness_capability_event(
                    db,
                    certificate_id=readiness_certificate.certificate_id,
                    event_kind=ReadinessCapabilityEventKind.CONSUMED,
                    component=readiness_certificate.component,
                    candidate_release_id=candidate_release_id,
                    rollout_id=prepared.rollout_id,
                    actor="production-readiness-gate",
                    reason="readiness capability atomically consumed by PREPARED rollout",
                    payload_sha256=payload_hash,
                    deployment_event_hash=_deployment_event_hash(
                        db, prepared.rollout_id, RolloutState.PREPARED.value
                    ),
                    now=timestamp,
                )
                journal = verify_readiness_capability_journal_connection(
                    db,
                    certificate_id=readiness_certificate.certificate_id,
                    expected_latest_kind=ReadinessCapabilityEventKind.CONSUMED,
                )
                if not journal.valid:
                    raise RuntimeError(
                        "readiness capability journal invalid: " + ",".join(journal.failures)
                    )
                snapshot = certify_activation_snapshot(
                    db,
                    self.path,
                    component=prepared.component,
                    rollout_id=prepared.rollout_id,
                    certificate_id=readiness_certificate.certificate_id,
                    candidate_release_id=prepared.candidate_release_id,
                    previous_release_id=prepared.previous_release_id,
                    policy=readiness_certificate.bottleneck_policy,
                    now=timestamp,
                )
                if not snapshot.passed:
                    raise RuntimeError(
                        "activation snapshot certification failed: " + ",".join(snapshot.failures)
                    )
                db.execute(
                    "UPDATE production_readiness_consumptions SET activation_snapshot_hash=? "
                    "WHERE certificate_id=? AND consumed_rollout_id=?",
                    (
                        snapshot.snapshot_hash,
                        readiness_certificate.certificate_id,
                        prepared.rollout_id,
                    ),
                )
                db.execute(
                    "UPDATE deployment_rollouts SET readiness_activation_snapshot_hash=? "
                    "WHERE rollout_id=? AND state='PREPARED'",
                    (snapshot.snapshot_hash, prepared.rollout_id),
                )
                db.execute("COMMIT")
        except Exception:
            with contextlib.suppress(Exception):
                _legacy.DeploymentStateMachine.cancel(
                    self,
                    prepared.rollout_id,
                    operator="production-readiness-gate",
                    reason="post-PREPARED provenance certification failed",
                    now=timestamp,
                )
            raise
        return self.get(prepared.rollout_id)

    def _validate_activation_preconditions_in_transaction(
        self,
        db: sqlite3.Connection,
        rollout: sqlite3.Row,
        *,
        current_audit: ProductionBottleneckAuditReport,
        now: datetime,
    ) -> None:
        try:
            rollout_id = str(rollout["rollout_id"])
            certificate_id = str(rollout["readiness_certificate_id"])
            capability = db.execute(
                "SELECT * FROM production_readiness_consumptions WHERE certificate_id=?",
                (certificate_id,),
            ).fetchone()
            if capability is None or str(capability["consumed_rollout_id"]) != rollout_id:
                raise ValueError("consumed readiness capability is missing")
            encoded = str(capability["bottleneck_policy_json"])
            policy = parse_bottleneck_policy_json(encoded)
            policy_hash = bottleneck_policy_fingerprint(policy)
            if policy_hash != str(capability["bottleneck_policy_sha256"]):
                raise ValueError("readiness bottleneck policy integrity mismatch")
            if policy_hash != str(rollout["readiness_bottleneck_policy_hash"]):
                raise ValueError("rollout bottleneck policy binding changed")
            if bottleneck_policy_json(policy) != encoded:
                raise ValueError("readiness bottleneck policy is noncanonical")
            snapshot = certify_activation_snapshot(
                db,
                self.path,
                component=str(rollout["component"]),
                rollout_id=rollout_id,
                certificate_id=certificate_id,
                candidate_release_id=str(rollout["candidate_release_id"]),
                previous_release_id=str(rollout["previous_release_id"]),
                policy=policy,
                now=now,
            )
            if not snapshot.passed:
                raise ValueError(";".join(snapshot.failures))
            stored = str(capability["activation_snapshot_hash"])
            if not stored or stored != str(rollout["readiness_activation_snapshot_hash"]):
                raise ValueError("activation snapshot binding changed")
            if snapshot.snapshot_hash != stored:
                raise ValueError("activation snapshot changed after PREPARED")
            if snapshot.control_hash != str(capability["activation_control_hash"]):
                raise ValueError("activation control epoch changed after PREPARED")
            if snapshot.bottleneck_hash != str(capability["activation_bottleneck_hash"]):
                raise ValueError("bottleneck epoch changed after PREPARED")
            if activation_bottleneck_fingerprint(current_audit) != snapshot.bottleneck_hash:
                raise ValueError("caller audit does not match single-snapshot activation epoch")
        except _legacy._ActivationReadinessInvalid:
            raise
        except Exception as exc:
            raise _legacy._ActivationReadinessInvalid(str(exc)) from exc

    def cancel(
        self,
        rollout_id: str,
        *,
        operator: str,
        reason: str,
        now: datetime | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        record = super().cancel(rollout_id, operator=operator, reason=reason, now=timestamp)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT readiness_certificate_id FROM deployment_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            certificate_id = str(row[0]) if row is not None else ""
            journal_row = db.execute(
                "SELECT payload_sha256 FROM readiness_capability_events "
                "WHERE certificate_id=? ORDER BY seq DESC LIMIT 1",
                (certificate_id,),
            ).fetchone()
            if certificate_id and journal_row is not None:
                append_readiness_capability_event(
                    db,
                    certificate_id=certificate_id,
                    event_kind=ReadinessCapabilityEventKind.INVALIDATED,
                    component=record.component,
                    candidate_release_id=record.candidate_release_id,
                    rollout_id=rollout_id,
                    actor=operator,
                    reason=reason,
                    payload_sha256=str(journal_row[0]),
                    deployment_event_hash=_deployment_event_hash(
                        db, rollout_id, RolloutState.CANCELLED.value
                    ),
                    now=timestamp,
                )
            db.execute("COMMIT")
        return record

    def activate(
        self,
        rollout_id: str,
        *,
        operator: str,
        current_audit: ProductionBottleneckAuditReport,
        now: datetime | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            activated = super().activate(
                rollout_id,
                operator=operator,
                current_audit=current_audit,
                now=timestamp,
            )
        except ValueError as exc:
            if "readiness capability expired before activation" in str(exc):
                self._journal_terminal(
                    rollout_id,
                    ReadinessCapabilityEventKind.EXPIRED,
                    actor="production-readiness-gate",
                    reason="readiness capability expired before guarded activation",
                    now=timestamp,
                )
            raise
        try:
            self._journal_terminal(
                rollout_id,
                ReadinessCapabilityEventKind.ACTIVATED,
                actor=operator,
                reason="operator activated single-snapshot-certified guarded rollout",
                now=timestamp,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.halt(
                    rollout_id,
                    reason="readiness_capability_activation_journal_failed",
                    actor="deployment-state-machine",
                    now=timestamp,
                )
            raise RuntimeError("activation provenance journal failed; rollout halted") from exc
        return activated

    def _journal_terminal(
        self,
        rollout_id: str,
        kind: ReadinessCapabilityEventKind,
        *,
        actor: str,
        reason: str,
        now: datetime,
    ) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT readiness_certificate_id,component,candidate_release_id "
                "FROM deployment_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise KeyError(rollout_id)
            certificate_id = str(row["readiness_certificate_id"])
            latest = db.execute(
                "SELECT payload_sha256 FROM readiness_capability_events "
                "WHERE certificate_id=? ORDER BY seq DESC LIMIT 1",
                (certificate_id,),
            ).fetchone()
            if latest is None:
                db.execute("ROLLBACK")
                raise RuntimeError("readiness capability journal is missing")
            state = {
                ReadinessCapabilityEventKind.ACTIVATED: RolloutState.ACTIVE_GUARDED.value,
                ReadinessCapabilityEventKind.EXPIRED: RolloutState.EXPIRED.value,
            }[kind]
            append_readiness_capability_event(
                db,
                certificate_id=certificate_id,
                event_kind=kind,
                component=str(row["component"]),
                candidate_release_id=str(row["candidate_release_id"]),
                rollout_id=rollout_id,
                actor=actor,
                reason=reason,
                payload_sha256=str(latest[0]),
                deployment_event_hash=_deployment_event_hash(db, rollout_id, state),
                now=now,
            )
            report = verify_readiness_capability_journal_connection(
                db,
                certificate_id=certificate_id,
                expected_latest_kind=kind,
            )
            if not report.valid:
                db.execute("ROLLBACK")
                raise RuntimeError("readiness capability journal verification failed")
            db.execute("COMMIT")


_CORE_DEPLOYMENT_CLASS = "DeploymentStateMachine"
setattr(_core, _CORE_DEPLOYMENT_CLASS, DeploymentStateMachine)
