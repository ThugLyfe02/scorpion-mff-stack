from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from . import _deployment_state_machine_core as _core
from ._deployment_state_machine_core import (
    DeploymentStateMachinePolicy,
    RollbackState,
    RolloutRecord,
    RolloutState,
)
from .fail_safe_control import NoTradeSafetyLatch, SafetyMode
from .operator_observability import OperatorSystemState, build_operator_observability_snapshot
from .production_bottleneck_audit import ProductionBottleneckAuditReport
from .production_gate import (
    ProductionAuthorization,
    ProductionAuthorizationStatus,
    ProductionPromotionDossier,
)
from .production_readiness import (
    RolloutReadinessCertificate,
    bottleneck_state_fingerprint,
    capture_control_state_binding,
    operator_state_fingerprint,
    verify_rollout_readiness_certificate,
)
from .promotion_evidence_schema import PromotionEvidenceValidationReport
from .release_guard import ReleaseState
from .schema_contract import SCHEMA_CONTRACT_VERSION

_READINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS production_readiness_consumptions (
    certificate_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    candidate_release_id TEXT NOT NULL,
    dossier_id TEXT NOT NULL,
    authorization_id TEXT NOT NULL,
    evidence_bundle_hash TEXT NOT NULL,
    control_state_hash TEXT NOT NULL,
    certificate_expires_ts_utc TEXT NOT NULL,
    consumed_rollout_id TEXT NOT NULL DEFAULT '',
    consumed_ts_utc TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_readiness_consumed_rollout
ON production_readiness_consumptions(consumed_rollout_id)
WHERE consumed_rollout_id<>'';
"""

_NONTERMINAL = (
    RolloutState.PREPARED.value,
    RolloutState.ACTIVE_GUARDED.value,
    RolloutState.HALTED.value,
    RolloutState.ROLLBACK_PENDING.value,
    RolloutState.ROLLBACK_VERIFYING.value,
    RolloutState.RECOVERED_GUARDED.value,
    RolloutState.RESUME_PENDING.value,
)


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _ensure_column(db: sqlite3.Connection, column: str, ddl: str) -> None:
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(deployment_rollouts)")}
    if column not in columns:
        db.execute(f"ALTER TABLE deployment_rollouts ADD COLUMN {ddl}")


def _ensure_readiness_enforcement(db: sqlite3.Connection) -> None:
    db.executescript(_READINESS_SCHEMA)
    for column, ddl in (
        ("readiness_certificate_id", "readiness_certificate_id TEXT NOT NULL DEFAULT ''"),
        (
            "readiness_expires_ts_utc",
            "readiness_expires_ts_utc TEXT NOT NULL DEFAULT ''",
        ),
        (
            "readiness_operator_state_hash",
            "readiness_operator_state_hash TEXT NOT NULL DEFAULT ''",
        ),
        (
            "readiness_bottleneck_state_hash",
            "readiness_bottleneck_state_hash TEXT NOT NULL DEFAULT ''",
        ),
        (
            "readiness_control_state_hash",
            "readiness_control_state_hash TEXT NOT NULL DEFAULT ''",
        ),
    ):
        _ensure_column(db, column, ddl)
    db.executescript(
        """
        DROP TRIGGER IF EXISTS require_rollout_readiness_capability;
        DROP TRIGGER IF EXISTS consume_rollout_readiness_capability;

        CREATE TRIGGER require_rollout_readiness_capability
        BEFORE INSERT ON deployment_rollouts
        WHEN NEW.state='PREPARED'
        BEGIN
            SELECT CASE
                WHEN NEW.readiness_certificate_id=''
                THEN RAISE(ABORT, 'rollout_readiness_required')
            END;
            SELECT CASE
                WHEN NOT EXISTS (
                    SELECT 1 FROM production_readiness_consumptions r
                    WHERE r.certificate_id=NEW.readiness_certificate_id
                      AND r.component=NEW.component
                      AND r.candidate_release_id=NEW.candidate_release_id
                      AND r.dossier_id=NEW.dossier_id
                      AND r.authorization_id=NEW.authorization_id
                      AND r.evidence_bundle_hash=NEW.evidence_bundle_hash
                      AND r.control_state_hash=NEW.readiness_control_state_hash
                      AND r.consumed_rollout_id=''
                      AND r.certificate_expires_ts_utc>=NEW.created_ts_utc
                )
                THEN RAISE(ABORT, 'rollout_readiness_invalid_or_consumed')
            END;
        END;

        CREATE TRIGGER consume_rollout_readiness_capability
        AFTER INSERT ON deployment_rollouts
        WHEN NEW.state='PREPARED'
        BEGIN
            UPDATE production_readiness_consumptions
            SET consumed_rollout_id=NEW.rollout_id,
                consumed_ts_utc=NEW.created_ts_utc
            WHERE certificate_id=NEW.readiness_certificate_id
              AND consumed_rollout_id='';
        END;
        """
    )


class DeploymentStateMachine(_core.DeploymentStateMachine):
    """Production deployment machine with non-bypassable readiness consumption at PREPARED."""

    def __init__(
        self,
        path: str | Path,
        *,
        policy: DeploymentStateMachinePolicy | None = None,
    ) -> None:
        super().__init__(path, policy=policy)
        with self._connect() as db:
            _ensure_readiness_enforcement(db)

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
        if (
            bottleneck_audit is not None
            and bottleneck_audit.report_hash != readiness_certificate.bottleneck_report_hash
        ):
            raise ValueError("legacy bottleneck audit does not match readiness certificate")
        verification = verify_rollout_readiness_certificate(
            readiness_certificate,
            candidate_release_id=candidate_release_id,
            dossier=dossier,
            authorization=authorization,
            evidence_validation=evidence_validation,
            evidence_bundle_hash=evidence_bundle_hash,
            now=timestamp,
        )
        if not verification.valid:
            raise ValueError(
                "rollout readiness certificate invalid: " + ",".join(verification.failures)
            )
        if readiness_certificate.schema_contract_version != SCHEMA_CONTRACT_VERSION:
            raise ValueError("rollout readiness schema contract changed after certification")
        if not candidate_release_id.strip() or not evidence_bundle_hash.strip():
            raise ValueError("candidate_release_id and evidence_bundle_hash are required")
        if not dossier.ready_for_approval:
            raise ValueError("production dossier is not ready")
        if (
            authorization.status
            is not ProductionAuthorizationStatus.AUTHORIZED_FOR_OPERATOR_ACTIVATION
        ):
            raise ValueError("production authorization is not active")
        if authorization.dossier_id != dossier.dossier_id:
            raise ValueError("authorization is bound to a different dossier")
        if authorization.authorized_until_ts_utc is None:
            raise ValueError("production authorization has no expiry")
        authorization_expires = authorization.authorized_until_ts_utc.astimezone(UTC)
        if timestamp > authorization_expires:
            raise ValueError("production authorization expired before rollout preparation")
        if not evidence_validation.valid:
            raise ValueError("formal promotion evidence schema is invalid")

        # Fast, precise diagnostics before the broader operator snapshot. These checks are
        # repeated inside BEGIN IMMEDIATE below; this preflight does not carry authority.
        with self._connect() as preflight_db:
            consumed = preflight_db.execute(
                "SELECT consumed_rollout_id FROM production_readiness_consumptions "
                "WHERE certificate_id=?",
                (readiness_certificate.certificate_id,),
            ).fetchone()
            if consumed is not None and str(consumed["consumed_rollout_id"]):
                raise ValueError("rollout readiness certificate has already been consumed")
            preflight_control = capture_control_state_binding(
                preflight_db,
                component=readiness_certificate.component,
                required_heartbeats=readiness_certificate.bottleneck_policy.required_heartbeats,
            )
        if preflight_control.state_hash != readiness_certificate.control_state_hash:
            raise ValueError("control-state epoch changed after readiness certification")

        current_snapshot = build_operator_observability_snapshot(
            self.path,
            now=timestamp,
            bottleneck_policy=readiness_certificate.bottleneck_policy,
        )
        if current_snapshot.system_state is not OperatorSystemState.READY:
            raise ValueError("operator observability is no longer rollout-ready")
        current_operator_state = operator_state_fingerprint(current_snapshot)
        if current_operator_state != readiness_certificate.operator_state_hash:
            raise ValueError("operator production state changed after readiness certification")
        current_bottleneck_state = bottleneck_state_fingerprint(
            current_snapshot.bottleneck_report
        )
        if current_bottleneck_state != readiness_certificate.bottleneck_state_hash:
            raise ValueError("production bottleneck state changed after readiness certification")
        if not current_snapshot.bottleneck_report.ready_for_rollout:
            raise ValueError("current production bottleneck state blocks rollout")

        latch = NoTradeSafetyLatch(self.path)
        if not latch.verify_integrity(readiness_certificate.component).valid:
            raise ValueError("component safety ledger integrity is invalid")

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                target = db.execute(
                    "SELECT * FROM component_releases WHERE release_id=?",
                    (candidate_release_id,),
                ).fetchone()
                if target is None:
                    raise KeyError(candidate_release_id)
                component = str(target["component"])
                if component != dossier.component or component != readiness_certificate.component:
                    raise ValueError("candidate release component changed after readiness")
                if str(target["artifact_hash"]) != dossier.artifact_sha256:
                    raise ValueError("candidate release artifact does not match dossier")
                if str(target["artifact_hash"]) != readiness_certificate.candidate_artifact_hash:
                    raise ValueError("candidate release artifact changed after readiness")
                if (
                    str(target["policy_fingerprint"])
                    != readiness_certificate.candidate_policy_fingerprint
                ):
                    raise ValueError("candidate policy fingerprint changed after readiness")
                if (
                    str(target["research_manifest_hash"])
                    != readiness_certificate.candidate_research_manifest_hash
                ):
                    raise ValueError("candidate research manifest changed after readiness")
                previous_release_id = str(target["previous_release_id"] or "")
                if previous_release_id != dossier.parent_release_id:
                    raise ValueError("candidate release predecessor does not match dossier lineage")
                if previous_release_id != readiness_certificate.expected_predecessor_release_id:
                    raise ValueError("candidate predecessor changed after readiness certification")
                target_state = ReleaseState(str(target["state"]))
                if target_state not in {ReleaseState.CANDIDATE, ReleaseState.SUPERSEDED}:
                    raise ValueError("candidate release is not rollout-eligible")

                active = db.execute(
                    "SELECT release_id FROM component_releases "
                    "WHERE component=? AND state='ACTIVE'",
                    (component,),
                ).fetchall()
                active_ids = tuple(sorted(str(row["release_id"]) for row in active))
                expected_active = (previous_release_id,) if previous_release_id else ()
                if active_ids != expected_active:
                    raise ValueError("active predecessor changed after readiness certification")

                control = capture_control_state_binding(
                    db,
                    component=component,
                    required_heartbeats=readiness_certificate.bottleneck_policy.required_heartbeats,
                )
                if control.state_hash != readiness_certificate.control_state_hash:
                    raise ValueError("control-state epoch changed after readiness certification")
                if control.safety_mode != SafetyMode.NORMAL.value:
                    raise ValueError("component safety latch is not initialized NORMAL")
                if previous_release_id and control.safety_source_release_id != previous_release_id:
                    raise ValueError("component safety latch is not bound to predecessor release")
                if control.safety_event_count != readiness_certificate.safety_event_count:
                    raise ValueError("safety generation changed after readiness certification")
                if control.safety_head_event_id != readiness_certificate.safety_head_event_id:
                    raise ValueError("safety event head changed after readiness certification")
                if control.safety_chain_hash != readiness_certificate.safety_chain_hash:
                    raise ValueError("safety event chain changed after readiness certification")

                consumed = db.execute(
                    "SELECT consumed_rollout_id FROM production_readiness_consumptions "
                    "WHERE certificate_id=?",
                    (readiness_certificate.certificate_id,),
                ).fetchone()
                if consumed is not None and str(consumed["consumed_rollout_id"]):
                    raise ValueError("rollout readiness certificate has already been consumed")

                self._expire_prepared(db, component, timestamp)
                placeholders = ",".join("?" for _ in _NONTERMINAL)
                existing = db.execute(
                    f"""
                    SELECT rollout_id FROM deployment_rollouts
                    WHERE component=? AND state IN ({placeholders})
                    LIMIT 1
                    """,
                    (component, *_NONTERMINAL),
                ).fetchone()
                if existing is not None:
                    raise ValueError("component already has a non-terminal rollout")

                rollout_id = _hash_payload(
                    {
                        "version": "deployment-rollout-v4",
                        "component": component,
                        "candidate_release_id": candidate_release_id,
                        "dossier_id": dossier.dossier_id,
                        "authorization_id": authorization.authorization_id,
                        "evidence_bundle_hash": evidence_bundle_hash,
                        "readiness_certificate_id": readiness_certificate.certificate_id,
                        "operator_state_hash": readiness_certificate.operator_state_hash,
                        "bottleneck_state_hash": readiness_certificate.bottleneck_state_hash,
                        "control_state_hash": readiness_certificate.control_state_hash,
                    }
                )
                db.execute(
                    """
                    INSERT INTO production_readiness_consumptions
                    (certificate_id,component,candidate_release_id,dossier_id,authorization_id,
                     evidence_bundle_hash,control_state_hash,certificate_expires_ts_utc)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(certificate_id) DO NOTHING
                    """,
                    (
                        readiness_certificate.certificate_id,
                        component,
                        candidate_release_id,
                        dossier.dossier_id,
                        authorization.authorization_id,
                        evidence_bundle_hash,
                        readiness_certificate.control_state_hash,
                        readiness_certificate.expires_ts_utc.astimezone(UTC).isoformat(),
                    ),
                )
                capability = db.execute(
                    "SELECT consumed_rollout_id FROM production_readiness_consumptions "
                    "WHERE certificate_id=?",
                    (readiness_certificate.certificate_id,),
                ).fetchone()
                if capability is None or str(capability["consumed_rollout_id"]):
                    raise ValueError("rollout readiness capability is unavailable or consumed")

                db.execute(
                    """
                    INSERT INTO deployment_rollouts
                    (rollout_id,component,candidate_release_id,previous_release_id,
                     active_release_id,dossier_id,authorization_id,authorization_expires_ts_utc,
                     evidence_bundle_hash,preparation_audit_hash,state,rollback_state,generation,
                     created_ts_utc,updated_ts_utc,readiness_certificate_id,
                     readiness_expires_ts_utc,readiness_operator_state_hash,
                     readiness_bottleneck_state_hash,readiness_control_state_hash)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'PREPARED','NONE',0,?,?,?,?,?,?,?)
                    """,
                    (
                        rollout_id,
                        component,
                        candidate_release_id,
                        previous_release_id,
                        "",
                        dossier.dossier_id,
                        authorization.authorization_id,
                        authorization_expires.isoformat(),
                        evidence_bundle_hash,
                        readiness_certificate.bottleneck_report_hash,
                        timestamp.isoformat(),
                        timestamp.isoformat(),
                        readiness_certificate.certificate_id,
                        readiness_certificate.expires_ts_utc.astimezone(UTC).isoformat(),
                        readiness_certificate.operator_state_hash,
                        readiness_certificate.bottleneck_state_hash,
                        readiness_certificate.control_state_hash,
                    ),
                )
                self._append_event(
                    db,
                    rollout_id=rollout_id,
                    from_state="",
                    to_state=RolloutState.PREPARED,
                    rollback_state=RollbackState.NONE,
                    actor="production-readiness-gate",
                    reason=(
                        "single-use readiness capability consumed; formal evidence, "
                        "operator state and control epoch accepted"
                    ),
                    now=timestamp,
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

        integrity = latch.verify_integrity(component)
        if not integrity.valid:
            self.cancel(
                rollout_id,
                operator="deployment-state-machine",
                reason="safety ledger integrity failed after readiness-bound preparation",
                now=timestamp,
            )
            raise ValueError("component safety ledger integrity is invalid")
        return self.get(rollout_id)

    def activate(
        self,
        rollout_id: str,
        *,
        operator: str,
        current_audit: ProductionBottleneckAuditReport,
        now: datetime | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self._connect() as db:
            row = db.execute(
                "SELECT certificate_id,certificate_expires_ts_utc "
                "FROM production_readiness_consumptions WHERE consumed_rollout_id=?",
                (rollout_id,),
            ).fetchone()
        if row is None:
            raise ValueError("prepared rollout has no consumed readiness capability")
        readiness_expires = datetime.fromisoformat(
            str(row["certificate_expires_ts_utc"])
        ).astimezone(UTC)
        if timestamp > readiness_expires:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                rollout = db.execute(
                    "SELECT state,rollback_state FROM deployment_rollouts WHERE rollout_id=?",
                    (rollout_id,),
                ).fetchone()
                if (
                    rollout is not None
                    and RolloutState(str(rollout["state"])) is RolloutState.PREPARED
                ):
                    db.execute(
                        "UPDATE deployment_rollouts SET state='EXPIRED',updated_ts_utc=? "
                        "WHERE rollout_id=?",
                        (timestamp.isoformat(), rollout_id),
                    )
                    self._append_event(
                        db,
                        rollout_id=rollout_id,
                        from_state=RolloutState.PREPARED.value,
                        to_state=RolloutState.EXPIRED,
                        rollback_state=RollbackState(str(rollout["rollback_state"])),
                        actor="production-readiness-gate",
                        reason="readiness capability expired before guarded activation",
                        now=timestamp,
                    )
                    db.execute("COMMIT")
                else:
                    db.execute("ROLLBACK")
            raise ValueError("rollout readiness capability expired before activation")
        return super().activate(
            rollout_id,
            operator=operator,
            current_audit=current_audit,
            now=timestamp,
        )


def __getattr__(name: str) -> object:
    """Forward private legacy helpers for compatibility while keeping the guarded class public."""
    return getattr(_core, name)


_CORE_DEPLOYMENT_CLASS = "DeploymentStateMachine"
setattr(_core, _CORE_DEPLOYMENT_CLASS, DeploymentStateMachine)
