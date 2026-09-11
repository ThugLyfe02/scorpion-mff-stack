from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from . import _deployment_state_machine_core as _core
from ._deployment_core_alias import CoreDeploymentStateMachine
from ._deployment_state_machine_v26 import (
    DeploymentStateMachine as LegacyDeploymentStateMachine,
    DeploymentStateMachinePolicy,
    RollbackRecord,
    RollbackState,
    RolloutHealthEvidence,
    RolloutIntegrityReport,
    RolloutRecord,
    RolloutState,
    _ActivationReadinessInvalid,
    _ensure_readiness_enforcement,
)
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

_APPLICATION_ID = "scorpion-mff/0.27.0"


class DeploymentStateMachine(LegacyDeploymentStateMachine):
    """v0.27 production deployment gate with migration and capability provenance."""

    def __init__(
        self,
        path: str | Path,
        *,
        policy: DeploymentStateMachinePolicy | None = None,
    ) -> None:
        # Bootstrap only the stable core schema, then migrate every production extension
        # through the checksummed migration ledger before installing runtime triggers.
        CoreDeploymentStateMachine.__init__(self, path, policy=policy)
        apply_schema_migrations(self.path, application_id=_APPLICATION_ID)
        with self._connect() as db:
            _ensure_readiness_enforcement(db)

    @staticmethod
    def _deployment_event_hash(
        db: sqlite3.Connection,
        *,
        rollout_id: str,
        to_state: str,
    ) -> str:
        row = db.execute(
            """
            SELECT event_hash FROM deployment_rollout_events
            WHERE rollout_id=? AND to_state=? ORDER BY seq DESC LIMIT 1
            """,
            (rollout_id, to_state),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"deployment event missing for {rollout_id}:{to_state}")
        return str(row[0])

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
        if readiness_certificate is None:
            # Super already rejects this. Keep type narrowing explicit for strict mypy.
            raise RuntimeError("rollout readiness certificate unexpectedly missing")

        payload_hash = capability_payload_sha256(
            certificate_id=readiness_certificate.certificate_id,
            component=prepared.component,
            candidate_release_id=prepared.candidate_release_id,
            expires_ts_utc=readiness_certificate.expires_ts_utc,
        )
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                ensure_readiness_capability_issued(
                    db,
                    certificate_id=readiness_certificate.certificate_id,
                    component=prepared.component,
                    candidate_release_id=prepared.candidate_release_id,
                    expires_ts_utc=readiness_certificate.expires_ts_utc,
                    actor="production-readiness-gate",
                    issued_ts_utc=readiness_certificate.generated_ts_utc,
                )
                prepared_event_hash = self._deployment_event_hash(
                    db,
                    rollout_id=prepared.rollout_id,
                    to_state=RolloutState.PREPARED.value,
                )
                append_readiness_capability_event(
                    db,
                    certificate_id=readiness_certificate.certificate_id,
                    event_kind=ReadinessCapabilityEventKind.CONSUMED,
                    component=prepared.component,
                    candidate_release_id=prepared.candidate_release_id,
                    rollout_id=prepared.rollout_id,
                    actor="production-readiness-gate",
                    reason="single-use readiness capability consumed by PREPARED rollout",
                    payload_sha256=payload_hash,
                    deployment_event_hash=prepared_event_hash,
                    now=timestamp,
                )
                journal = verify_readiness_capability_journal_connection(
                    db,
                    certificate_id=readiness_certificate.certificate_id,
                    expected_latest_kind=ReadinessCapabilityEventKind.CONSUMED,
                )
                if not journal.valid:
                    raise RuntimeError(
                        "readiness capability journal invalid after consumption: "
                        + ",".join(journal.failures)
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
                        "single-snapshot activation certification failed after PREPARED: "
                        + ",".join(snapshot.failures)
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
                super().cancel(
                    prepared.rollout_id,
                    operator="production-readiness-gate",
                    reason="v0.27 post-prepare provenance certification failed",
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
        rollout_id = str(rollout["rollout_id"])
        component = str(rollout["component"])
        capability = db.execute(
            "SELECT * FROM production_readiness_consumptions WHERE consumed_rollout_id=?",
            (rollout_id,),
        ).fetchone()
        if capability is None:
            raise _ActivationReadinessInvalid("consumed readiness capability is missing")
        certificate_id = str(capability["certificate_id"])
        if certificate_id != str(rollout["readiness_certificate_id"]):
            raise _ActivationReadinessInvalid(
                "rollout readiness certificate identity changed"
            )
        for column in (
            "component",
            "candidate_release_id",
            "dossier_id",
            "authorization_id",
            "evidence_bundle_hash",
        ):
            if str(capability[column]) != str(rollout[column]):
                raise _ActivationReadinessInvalid(
                    f"readiness capability binding changed:{column}"
                )
        expiry = datetime.fromisoformat(str(capability["certificate_expires_ts_utc"])).astimezone(
            UTC
        )
        if now > expiry:
            raise _ActivationReadinessInvalid("readiness capability expired")

        encoded_policy = str(capability["bottleneck_policy_json"])
        policy = parse_bottleneck_policy_json(encoded_policy)
        policy_hash = bottleneck_policy_fingerprint(policy)
        if policy_hash != str(capability["bottleneck_policy_sha256"]):
            raise _ActivationReadinessInvalid(
                "readiness bottleneck policy integrity mismatch"
            )
        if policy_hash != str(rollout["readiness_bottleneck_policy_hash"]):
            raise _ActivationReadinessInvalid("rollout bottleneck policy binding changed")
        if bottleneck_policy_json(policy) != encoded_policy:
            raise _ActivationReadinessInvalid("readiness bottleneck policy is noncanonical")

        snapshot = certify_activation_snapshot(
            db,
            self.path,
            component=component,
            rollout_id=rollout_id,
            certificate_id=certificate_id,
            candidate_release_id=str(rollout["candidate_release_id"]),
            previous_release_id=str(rollout["previous_release_id"]),
            policy=policy,
            now=now,
        )
        if not snapshot.passed:
            raise _ActivationReadinessInvalid(
                "single-snapshot activation certification failed:" + ",".join(snapshot.failures)
            )
        expected_snapshot = str(capability["activation_snapshot_hash"])
        if not expected_snapshot:
            raise _ActivationReadinessInvalid("activation snapshot binding is missing")
        if expected_snapshot != str(rollout["readiness_activation_snapshot_hash"]):
            raise _ActivationReadinessInvalid("rollout activation snapshot binding changed")
        if snapshot.snapshot_hash != expected_snapshot:
            raise _ActivationReadinessInvalid("activation snapshot changed after PREPARED")
        if snapshot.control_hash != str(capability["activation_control_hash"]):
            raise _ActivationReadinessInvalid("activation control epoch changed")
        if snapshot.bottleneck_hash != str(capability["activation_bottleneck_hash"]):
            raise _ActivationReadinessInvalid("activation bottleneck epoch changed")
        if activation_bottleneck_fingerprint(current_audit) != snapshot.bottleneck_hash:
            raise _ActivationReadinessInvalid(
                "caller audit does not match authoritative activation snapshot"
            )

    def _append_terminal_capability_event(
        self,
        rollout_id: str,
        *,
        kind: ReadinessCapabilityEventKind,
        deployment_state: str,
        actor: str,
        reason: str,
        now: datetime,
    ) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rollout = db.execute(
                "SELECT * FROM deployment_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            if rollout is None:
                db.execute("ROLLBACK")
                raise KeyError(rollout_id)
            certificate_id = str(rollout["readiness_certificate_id"])
            report = verify_readiness_capability_journal_connection(
                db,
                certificate_id=certificate_id,
            )
            if report.latest_kind in {
                ReadinessCapabilityEventKind.ACTIVATED.value,
                ReadinessCapabilityEventKind.INVALIDATED.value,
                ReadinessCapabilityEventKind.EXPIRED.value,
            }:
                db.execute("COMMIT")
                return
            first = db.execute(
                "SELECT payload_sha256 FROM readiness_capability_events "
                "WHERE certificate_id=? ORDER BY seq LIMIT 1",
                (certificate_id,),
            ).fetchone()
            if first is None:
                db.execute("ROLLBACK")
                raise RuntimeError("readiness capability ISSUED event is missing")
            deployment_hash = self._deployment_event_hash(
                db,
                rollout_id=rollout_id,
                to_state=deployment_state,
            )
            append_readiness_capability_event(
                db,
                certificate_id=certificate_id,
                event_kind=kind,
                component=str(rollout["component"]),
                candidate_release_id=str(rollout["candidate_release_id"]),
                rollout_id=rollout_id,
                actor=actor,
                reason=reason,
                payload_sha256=str(first["payload_sha256"]),
                deployment_event_hash=deployment_hash,
                now=now,
            )
            db.execute("COMMIT")

    def cancel(
        self,
        rollout_id: str,
        *,
        operator: str,
        reason: str,
        now: datetime | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        result = super().cancel(
            rollout_id,
            operator=operator,
            reason=reason,
            now=timestamp,
        )
        self._append_terminal_capability_event(
            rollout_id,
            kind=ReadinessCapabilityEventKind.INVALIDATED,
            deployment_state=RolloutState.CANCELLED.value,
            actor=operator,
            reason=reason,
            now=timestamp,
        )
        return result

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
            result = super().activate(
                rollout_id,
                operator=operator,
                current_audit=current_audit,
                now=timestamp,
            )
        except Exception:
            with contextlib.suppress(Exception):
                state = self.get(rollout_id).state
                if state is RolloutState.EXPIRED:
                    self._append_terminal_capability_event(
                        rollout_id,
                        kind=ReadinessCapabilityEventKind.EXPIRED,
                        deployment_state=RolloutState.EXPIRED.value,
                        actor="production-readiness-gate",
                        reason="readiness capability expired before activation",
                        now=timestamp,
                    )
            raise
        try:
            self._append_terminal_capability_event(
                rollout_id,
                kind=ReadinessCapabilityEventKind.ACTIVATED,
                deployment_state=RolloutState.ACTIVE_GUARDED.value,
                actor=operator,
                reason="operator activated single-snapshot-certified rollout",
                now=timestamp,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.halt(
                    rollout_id,
                    reason="readiness_capability_activation_journal_failed",
                    actor="production-readiness-gate",
                    now=timestamp,
                )
            raise RuntimeError(
                "activation succeeded but readiness journal failed; rollout failed closed"
            ) from exc
        return result


_CORE_DEPLOYMENT_CLASS = "DeploymentStateMachine"
setattr(_core, _CORE_DEPLOYMENT_CLASS, DeploymentStateMachine)
