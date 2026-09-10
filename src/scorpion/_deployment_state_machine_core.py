from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .fail_safe_control import NoTradeSafetyLatch, SafetyMode, quarantine_and_trip_no_trade
from .production_bottleneck_audit import ProductionBottleneckAuditReport
from .production_gate import (
    ProductionAuthorization,
    ProductionAuthorizationStatus,
    ProductionPromotionDossier,
)
from .promotion_evidence_schema import PromotionEvidenceValidationReport
from .release_guard import ReleaseRegistry, ReleaseState


class RolloutState(StrEnum):
    PREPARED = "PREPARED"
    ACTIVE_GUARDED = "ACTIVE_GUARDED"
    STABLE = "STABLE"
    HALTED = "HALTED"
    ROLLBACK_PENDING = "ROLLBACK_PENDING"
    ROLLBACK_VERIFYING = "ROLLBACK_VERIFYING"
    RECOVERED_GUARDED = "RECOVERED_GUARDED"
    RESUME_PENDING = "RESUME_PENDING"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED_SAFE = "FAILED_SAFE"


class RollbackState(StrEnum):
    NONE = "NONE"
    PROPOSED = "PROPOSED"
    AUTHORIZED = "AUTHORIZED"
    VERIFYING = "VERIFYING"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class DeploymentStateMachinePolicy:
    minimum_guarded_soak: timedelta = timedelta(minutes=15)
    maximum_audit_age: timedelta = timedelta(seconds=30)
    maximum_recovery_evidence_age: timedelta = timedelta(seconds=30)
    require_distinct_rollback_operators: bool = True

    def __post_init__(self) -> None:
        if self.minimum_guarded_soak < timedelta(0):
            raise ValueError("minimum_guarded_soak cannot be negative")
        if self.maximum_audit_age <= timedelta(0):
            raise ValueError("maximum_audit_age must be positive")
        if self.maximum_recovery_evidence_age <= timedelta(0):
            raise ValueError("maximum_recovery_evidence_age must be positive")


@dataclass(frozen=True, slots=True)
class RolloutHealthEvidence:
    observed_ts_utc: datetime
    bottleneck_audit: ProductionBottleneckAuditReport
    runtime_certified: bool
    state_replay_verified: bool
    safety_integrity_valid: bool
    quote_consensus_ok: bool
    canary_healthy: bool
    drift_active: bool

    @property
    def rollout_ready(self) -> bool:
        return self._base_healthy() and self.bottleneck_audit.ready_for_rollout

    @property
    def rollback_authorization_ready(self) -> bool:
        tolerated = {
            "active_release_safety_conflict",
            "ingress_consumer_not_alive",
            "pending_raw_backlog",
            "pending_raw_age",
        }
        blocking = tuple(
            item
            for item in self.bottleneck_audit.findings
            if item.blocks_rollout and item.code not in tolerated
        )
        return self.state_replay_verified and self.safety_integrity_valid and not blocking

    @property
    def recovery_ready(self) -> bool:
        blocking = tuple(
            item
            for item in self.bottleneck_audit.findings
            if item.blocks_rollout and item.code != "active_release_safety_conflict"
        )
        return self._base_healthy() and not blocking

    def _base_healthy(self) -> bool:
        return (
            self.runtime_certified
            and self.state_replay_verified
            and self.safety_integrity_valid
            and self.quote_consensus_ok
            and self.canary_healthy
            and not self.drift_active
        )


@dataclass(frozen=True, slots=True)
class RolloutRecord:
    rollout_id: str
    component: str
    candidate_release_id: str
    previous_release_id: str
    active_release_id: str
    dossier_id: str
    authorization_id: str
    authorization_expires_ts_utc: datetime
    evidence_bundle_hash: str
    preparation_audit_hash: str
    state: RolloutState
    rollback_state: RollbackState
    generation: int
    created_ts_utc: datetime
    updated_ts_utc: datetime
    activated_ts_utc: datetime | None
    stable_ts_utc: datetime | None


@dataclass(frozen=True, slots=True)
class RollbackRecord:
    rollback_id: str
    rollout_id: str
    target_release_id: str
    state: RollbackState
    proposed_ts_utc: datetime
    authorized_ts_utc: datetime | None
    authorized_by: str
    applied_ts_utc: datetime | None
    applied_by: str
    verified_ts_utc: datetime | None
    verified_by: str
    failure_reason: str


@dataclass(frozen=True, slots=True)
class RolloutIntegrityReport:
    rollout_id: str
    events: int
    contiguous_sequence: bool
    event_hashes_valid: bool
    event_ids_valid: bool
    chain_valid: bool
    checkpoint_matches_history: bool
    materialized_state_matches: bool
    valid: bool
    failures: tuple[str, ...]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployment_rollouts (
    rollout_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    candidate_release_id TEXT NOT NULL,
    previous_release_id TEXT NOT NULL DEFAULT '',
    active_release_id TEXT NOT NULL DEFAULT '',
    dossier_id TEXT NOT NULL,
    authorization_id TEXT NOT NULL,
    authorization_expires_ts_utc TEXT NOT NULL,
    evidence_bundle_hash TEXT NOT NULL,
    preparation_audit_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    rollback_state TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    created_ts_utc TEXT NOT NULL,
    updated_ts_utc TEXT NOT NULL,
    activated_ts_utc TEXT,
    stable_ts_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_deployment_rollout_component_state
ON deployment_rollouts(component,state,updated_ts_utc);

CREATE TABLE IF NOT EXISTS deployment_rollbacks (
    rollback_id TEXT PRIMARY KEY,
    rollout_id TEXT NOT NULL UNIQUE,
    target_release_id TEXT NOT NULL,
    state TEXT NOT NULL,
    proposed_ts_utc TEXT NOT NULL,
    authorized_ts_utc TEXT,
    authorized_by TEXT NOT NULL DEFAULT '',
    applied_ts_utc TEXT,
    applied_by TEXT NOT NULL DEFAULT '',
    verified_ts_utc TEXT,
    verified_by TEXT NOT NULL DEFAULT '',
    failure_reason TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(rollout_id) REFERENCES deployment_rollouts(rollout_id)
);

CREATE TABLE IF NOT EXISTS deployment_rollout_events (
    event_id TEXT PRIMARY KEY,
    rollout_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    rollback_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    previous_event_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    UNIQUE(rollout_id,seq),
    FOREIGN KEY(rollout_id) REFERENCES deployment_rollouts(rollout_id)
);
CREATE INDEX IF NOT EXISTS idx_deployment_events_rollout_seq
ON deployment_rollout_events(rollout_id,seq);

CREATE TABLE IF NOT EXISTS deployment_rollout_integrity_state (
    rollout_id TEXT PRIMARY KEY,
    event_count INTEGER NOT NULL,
    head_event_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL,
    FOREIGN KEY(rollout_id) REFERENCES deployment_rollouts(rollout_id)
);
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
    return hashlib.sha256(encoded.encode()).hexdigest()


def _event_hash(
    *,
    rollout_id: str,
    seq: int,
    from_state: str,
    to_state: str,
    rollback_state: str,
    actor: str,
    reason: str,
    created_ts_utc: datetime,
    previous_event_hash: str,
) -> str:
    return _hash_payload(
        {
            "version": "deployment-rollout-event-v1",
            "rollout_id": rollout_id,
            "seq": seq,
            "from_state": from_state,
            "to_state": to_state,
            "rollback_state": rollback_state,
            "actor": actor,
            "reason": reason,
            "created_ts_utc": created_ts_utc.astimezone(UTC).isoformat(),
            "previous_event_hash": previous_event_hash,
        }
    )


def _event_id(rollout_id: str, seq: int, event_hash: str) -> str:
    return _hash_payload(
        {
            "version": "deployment-rollout-event-id-v1",
            "rollout_id": rollout_id,
            "seq": seq,
            "event_hash": event_hash,
        }
    )


def _audit_fresh(
    report: ProductionBottleneckAuditReport,
    *,
    now: datetime,
    maximum_age: timedelta,
) -> bool:
    generated = report.generated_ts_utc.astimezone(UTC)
    return generated <= now and now - generated <= maximum_age


def _health_fresh(
    evidence: RolloutHealthEvidence,
    *,
    now: datetime,
    maximum_age: timedelta,
) -> bool:
    observed = evidence.observed_ts_utc.astimezone(UTC)
    if observed > now or now - observed > maximum_age:
        return False
    return _audit_fresh(
        evidence.bottleneck_audit,
        now=now,
        maximum_age=maximum_age,
    )


class DeploymentStateMachine:
    """Durable rollout/rollback control with risk-increasing transitions operator-gated."""

    def __init__(
        self,
        path: str | Path,
        *,
        policy: DeploymentStateMachinePolicy | None = None,
    ) -> None:
        self.path = str(path)
        self.policy = policy or DeploymentStateMachinePolicy()
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _append_event(
        self,
        db: sqlite3.Connection,
        *,
        rollout_id: str,
        from_state: str,
        to_state: RolloutState,
        rollback_state: RollbackState,
        actor: str,
        reason: str,
        now: datetime,
    ) -> None:
        last = db.execute(
            """
            SELECT seq,event_hash FROM deployment_rollout_events
            WHERE rollout_id=? ORDER BY seq DESC LIMIT 1
            """,
            (rollout_id,),
        ).fetchone()
        seq = int(last["seq"]) + 1 if last is not None else 1
        previous_hash = str(last["event_hash"]) if last is not None else ""
        normalized_reason = reason[:1000]
        digest = _event_hash(
            rollout_id=rollout_id,
            seq=seq,
            from_state=from_state,
            to_state=to_state.value,
            rollback_state=rollback_state.value,
            actor=actor,
            reason=normalized_reason,
            created_ts_utc=now,
            previous_event_hash=previous_hash,
        )
        identifier = _event_id(rollout_id, seq, digest)
        db.execute(
            """
            INSERT INTO deployment_rollout_events
            (event_id,rollout_id,seq,from_state,to_state,rollback_state,actor,reason,
             created_ts_utc,previous_event_hash,event_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                identifier,
                rollout_id,
                seq,
                from_state,
                to_state.value,
                rollback_state.value,
                actor,
                normalized_reason,
                now.isoformat(),
                previous_hash,
                digest,
            ),
        )
        checkpoint = db.execute(
            "SELECT event_count,chain_hash FROM deployment_rollout_integrity_state "
            "WHERE rollout_id=?",
            (rollout_id,),
        ).fetchone()
        previous_chain = str(checkpoint["chain_hash"]) if checkpoint is not None else ""
        previous_count = int(checkpoint["event_count"]) if checkpoint is not None else 0
        chain_hash = hashlib.sha256(f"{previous_chain}|{identifier}".encode()).hexdigest()
        db.execute(
            """
            INSERT INTO deployment_rollout_integrity_state
            (rollout_id,event_count,head_event_hash,chain_hash)
            VALUES (?,?,?,?)
            ON CONFLICT(rollout_id) DO UPDATE SET
                event_count=excluded.event_count,
                head_event_hash=excluded.head_event_hash,
                chain_hash=excluded.chain_hash
            """,
            (rollout_id, previous_count + 1, digest, chain_hash),
        )

    def _expire_prepared(self, db: sqlite3.Connection, component: str, now: datetime) -> None:
        rows = db.execute(
            """
            SELECT rollout_id,rollback_state FROM deployment_rollouts
            WHERE component=? AND state='PREPARED' AND authorization_expires_ts_utc < ?
            """,
            (component, now.isoformat()),
        ).fetchall()
        for row in rows:
            rollout_id = str(row["rollout_id"])
            rollback_state = RollbackState(str(row["rollback_state"]))
            db.execute(
                "UPDATE deployment_rollouts SET state='EXPIRED',updated_ts_utc=? "
                "WHERE rollout_id=?",
                (now.isoformat(), rollout_id),
            )
            self._append_event(
                db,
                rollout_id=rollout_id,
                from_state=RolloutState.PREPARED.value,
                to_state=RolloutState.EXPIRED,
                rollback_state=rollback_state,
                actor="deployment-state-machine",
                reason="production authorization expired before activation",
                now=now,
            )

    def prepare(
        self,
        *,
        candidate_release_id: str,
        dossier: ProductionPromotionDossier,
        authorization: ProductionAuthorization,
        evidence_validation: PromotionEvidenceValidationReport,
        evidence_bundle_hash: str,
        bottleneck_audit: ProductionBottleneckAuditReport,
        now: datetime | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
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
        expires = authorization.authorized_until_ts_utc.astimezone(UTC)
        if timestamp > expires:
            raise ValueError("production authorization expired before rollout preparation")
        if not evidence_validation.valid:
            raise ValueError("formal promotion evidence schema is invalid")
        if not bottleneck_audit.ready_for_rollout:
            raise ValueError("production bottleneck audit blocks rollout")
        if not _audit_fresh(
            bottleneck_audit,
            now=timestamp,
            maximum_age=self.policy.maximum_audit_age,
        ):
            raise ValueError("production bottleneck audit is stale or future-dated")

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute(
                "SELECT * FROM component_releases WHERE release_id=?",
                (candidate_release_id,),
            ).fetchone()
            if target is None:
                db.execute("ROLLBACK")
                raise KeyError(candidate_release_id)
            component = str(target["component"])
            if component != dossier.component:
                db.execute("ROLLBACK")
                raise ValueError("candidate release component does not match dossier")
            if str(target["artifact_hash"]) != dossier.artifact_sha256:
                db.execute("ROLLBACK")
                raise ValueError("candidate release artifact does not match dossier")
            previous_release_id = str(target["previous_release_id"] or "")
            if previous_release_id != dossier.parent_release_id:
                db.execute("ROLLBACK")
                raise ValueError("candidate release predecessor does not match dossier lineage")
            target_state = ReleaseState(str(target["state"]))
            if target_state not in {ReleaseState.CANDIDATE, ReleaseState.SUPERSEDED}:
                db.execute("ROLLBACK")
                raise ValueError("candidate release is not rollout-eligible")
            safety = db.execute(
                "SELECT mode,source_release_id FROM component_safety_state WHERE component=?",
                (component,),
            ).fetchone()
            if safety is None or SafetyMode(str(safety["mode"])) is not SafetyMode.NORMAL:
                db.execute("ROLLBACK")
                raise ValueError("component safety latch is not initialized NORMAL")
            if previous_release_id and str(safety["source_release_id"]) != previous_release_id:
                db.execute("ROLLBACK")
                raise ValueError("component safety latch is not bound to predecessor release")
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
                db.execute("ROLLBACK")
                raise ValueError("component already has a non-terminal rollout")

            rollout_id = _hash_payload(
                {
                    "version": "deployment-rollout-v3",
                    "component": component,
                    "candidate_release_id": candidate_release_id,
                    "dossier_id": dossier.dossier_id,
                    "authorization_id": authorization.authorization_id,
                    "evidence_bundle_hash": evidence_bundle_hash,
                    "preparation_audit_hash": bottleneck_audit.report_hash,
                }
            )
            db.execute(
                """
                INSERT INTO deployment_rollouts
                (rollout_id,component,candidate_release_id,previous_release_id,
                 active_release_id,dossier_id,authorization_id,authorization_expires_ts_utc,
                 evidence_bundle_hash,preparation_audit_hash,state,rollback_state,generation,
                 created_ts_utc,updated_ts_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,'PREPARED','NONE',0,?,?)
                """,
                (
                    rollout_id,
                    component,
                    candidate_release_id,
                    previous_release_id,
                    "",
                    dossier.dossier_id,
                    authorization.authorization_id,
                    expires.isoformat(),
                    evidence_bundle_hash,
                    bottleneck_audit.report_hash,
                    timestamp.isoformat(),
                    timestamp.isoformat(),
                ),
            )
            self._append_event(
                db,
                rollout_id=rollout_id,
                from_state="",
                to_state=RolloutState.PREPARED,
                rollback_state=RollbackState.NONE,
                actor="production-gate",
                reason="formal evidence and bottleneck audit accepted",
                now=timestamp,
            )
            db.execute("COMMIT")
        integrity = NoTradeSafetyLatch(self.path).verify_integrity(component)
        if not integrity.valid:
            self.cancel(
                rollout_id,
                operator="deployment-state-machine",
                reason="safety ledger integrity failed after preparation",
                now=timestamp,
            )
            raise ValueError("component safety ledger integrity is invalid")
        return self.get(rollout_id)

    def cancel(
        self,
        rollout_id: str,
        *,
        operator: str,
        reason: str,
        now: datetime | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        if not operator.strip() or not reason.strip():
            raise ValueError("operator identity and cancel reason are required")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,rollback_state FROM deployment_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise KeyError(rollout_id)
            state = RolloutState(str(row["state"]))
            if state is not RolloutState.PREPARED:
                db.execute("ROLLBACK")
                raise ValueError("only a prepared rollout can be cancelled")
            rollback_state = RollbackState(str(row["rollback_state"]))
            db.execute(
                "UPDATE deployment_rollouts SET state='CANCELLED',updated_ts_utc=? "
                "WHERE rollout_id=?",
                (timestamp.isoformat(), rollout_id),
            )
            self._append_event(
                db,
                rollout_id=rollout_id,
                from_state=state.value,
                to_state=RolloutState.CANCELLED,
                rollback_state=rollback_state,
                actor=operator,
                reason=reason,
                now=timestamp,
            )
            db.execute("COMMIT")
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
        if not operator.strip():
            raise ValueError("operator identity is required")
        if not current_audit.ready_for_rollout:
            raise ValueError("current bottleneck audit blocks activation")
        if not _audit_fresh(
            current_audit,
            now=timestamp,
            maximum_age=self.policy.maximum_audit_age,
        ):
            raise ValueError("current bottleneck audit is stale or future-dated")

        current = self.get(rollout_id)
        latch = NoTradeSafetyLatch(self.path)
        safety_integrity = latch.verify_integrity(current.component)
        if not safety_integrity.valid:
            raise ValueError("component safety ledger integrity is invalid")
        component = current.component
        candidate = current.candidate_release_id
        previous = current.previous_release_id
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM deployment_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise KeyError(rollout_id)
            if RolloutState(str(row["state"])) is not RolloutState.PREPARED:
                db.execute("ROLLBACK")
                raise ValueError("rollout is not prepared for activation")
            expires = datetime.fromisoformat(
                str(row["authorization_expires_ts_utc"])
            ).astimezone(UTC)
            if timestamp > expires:
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
                    rollback_state=RollbackState.NONE,
                    actor="deployment-state-machine",
                    reason="production authorization expired before activation",
                    now=timestamp,
                )
                db.execute("COMMIT")
                raise ValueError("production authorization expired before activation")
            safety = db.execute(
                "SELECT mode,source_release_id FROM component_safety_state WHERE component=?",
                (component,),
            ).fetchone()
            if safety is None or SafetyMode(str(safety["mode"])) is not SafetyMode.NORMAL:
                db.execute("ROLLBACK")
                raise ValueError("component safety latch is not NORMAL")
            if str(safety["source_release_id"]) != previous:
                db.execute("ROLLBACK")
                raise ValueError("safety latch predecessor changed after rollout preparation")
            active = db.execute(
                "SELECT release_id FROM component_releases WHERE component=? AND state='ACTIVE'",
                (component,),
            ).fetchall()
            active_ids = {str(item["release_id"]) for item in active}
            expected = {previous} if previous else set()
            if active_ids != expected:
                db.execute("ROLLBACK")
                raise ValueError("active release lineage changed after rollout preparation")
            target = db.execute(
                "SELECT state FROM component_releases WHERE release_id=? AND component=?",
                (candidate, component),
            ).fetchone()
            if target is None or ReleaseState(str(target["state"])) not in {
                ReleaseState.CANDIDATE,
                ReleaseState.SUPERSEDED,
            }:
                db.execute("ROLLBACK")
                raise ValueError("candidate release is no longer activatable")

            prior_rollouts = db.execute(
                """
                SELECT rollout_id,rollback_state FROM deployment_rollouts
                WHERE component=? AND state='STABLE' AND rollout_id<>?
                """,
                (component, rollout_id),
            ).fetchall()
            for prior in prior_rollouts:
                prior_id = str(prior["rollout_id"])
                prior_rollback = RollbackState(str(prior["rollback_state"]))
                db.execute(
                    "UPDATE deployment_rollouts SET state='SUPERSEDED',updated_ts_utc=? "
                    "WHERE rollout_id=?",
                    (timestamp.isoformat(), prior_id),
                )
                self._append_event(
                    db,
                    rollout_id=prior_id,
                    from_state=RolloutState.STABLE.value,
                    to_state=RolloutState.SUPERSEDED,
                    rollback_state=prior_rollback,
                    actor=operator,
                    reason=f"superseded by rollout:{rollout_id}",
                    now=timestamp,
                )

            if previous:
                db.execute(
                    "UPDATE component_releases SET state='SUPERSEDED' WHERE release_id=?",
                    (previous,),
                )
            db.execute(
                """
                UPDATE component_releases
                SET state='ACTIVE',activated_ts_utc=?,activated_by=? WHERE release_id=?
                """,
                (timestamp.isoformat(), operator, candidate),
            )
            db.execute(
                """
                UPDATE deployment_rollouts
                SET state='ACTIVE_GUARDED',active_release_id=?,generation=generation+1,
                    activated_ts_utc=?,stable_ts_utc=NULL,updated_ts_utc=?
                WHERE rollout_id=?
                """,
                (candidate, timestamp.isoformat(), timestamp.isoformat(), rollout_id),
            )
            self._append_event(
                db,
                rollout_id=rollout_id,
                from_state=RolloutState.PREPARED.value,
                to_state=RolloutState.ACTIVE_GUARDED,
                rollback_state=RollbackState.NONE,
                actor=operator,
                reason="operator activated evidence-bound guarded rollout",
                now=timestamp,
            )
            db.execute("COMMIT")

        try:
            rebound = latch.rebind_normal_release(
                component,
                expected_source_release_id=previous,
                new_source_release_id=candidate,
                operator=operator,
                reason="bind safety state to newly active guarded rollout release",
                now=timestamp,
            )
            if not rebound.execution_allowed or rebound.source_release_id != candidate:
                raise RuntimeError("safety latch did not bind activated release")
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.halt(
                    rollout_id,
                    reason=f"activation_safety_rebind_failed:{type(exc).__name__}",
                    actor="deployment-state-machine",
                    now=timestamp,
                )
            with contextlib.suppress(Exception):
                latch.trip_no_trade(
                    component,
                    reason="activation_safety_rebind_failed",
                    source_release_id=candidate,
                    actor="deployment-state-machine",
                    now=timestamp,
                )
            raise RuntimeError("failed to bind safety latch to activated release") from exc
        return self.get(rollout_id)

    def mark_stable(
        self,
        rollout_id: str,
        *,
        operator: str,
        health: RolloutHealthEvidence,
        now: datetime | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        if not operator.strip():
            raise ValueError("operator identity is required")
        if not health.rollout_ready:
            raise ValueError("rollout health evidence is not healthy")
        if not _health_fresh(
            health,
            now=timestamp,
            maximum_age=self.policy.maximum_recovery_evidence_age,
        ):
            raise ValueError("rollout health evidence is stale or future-dated")
        current = self.get(rollout_id)
        latch = NoTradeSafetyLatch(self.path)
        safety_integrity = latch.verify_integrity(current.component)
        safety_state = latch.state(current.component)
        if (
            not safety_integrity.valid
            or not safety_state.execution_allowed
            or safety_state.source_release_id != current.active_release_id
        ):
            raise ValueError("component safety state is not bound to active release")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM deployment_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise KeyError(rollout_id)
            state = RolloutState(str(row["state"]))
            if state is not RolloutState.ACTIVE_GUARDED:
                db.execute("ROLLBACK")
                raise ValueError("rollout is not active and guarded")
            activated_raw = row["activated_ts_utc"]
            if activated_raw is None:
                db.execute("ROLLBACK")
                raise ValueError("guarded rollout has no activation timestamp")
            activated = datetime.fromisoformat(str(activated_raw)).astimezone(UTC)
            if timestamp - activated < self.policy.minimum_guarded_soak:
                db.execute("ROLLBACK")
                raise ValueError("guarded rollout soak period is incomplete")
            active_release_id = str(row["active_release_id"])
            active = db.execute(
                "SELECT release_id FROM component_releases WHERE component=? AND state='ACTIVE'",
                (current.component,),
            ).fetchall()
            if {str(item["release_id"]) for item in active} != {active_release_id}:
                db.execute("ROLLBACK")
                raise ValueError("materialized active release conflicts with rollout state")
            db.execute(
                "UPDATE deployment_rollouts SET state='STABLE',stable_ts_utc=?,updated_ts_utc=? "
                "WHERE rollout_id=?",
                (timestamp.isoformat(), timestamp.isoformat(), rollout_id),
            )
            self._append_event(
                db,
                rollout_id=rollout_id,
                from_state=state.value,
                to_state=RolloutState.STABLE,
                rollback_state=RollbackState(str(row["rollback_state"])),
                actor=operator,
                reason="operator accepted healthy guarded rollout after soak",
                now=timestamp,
            )
            db.execute("COMMIT")
        return self.get(rollout_id)

    def halt(
        self,
        rollout_id: str,
        *,
        reason: str,
        actor: str = "automatic-rollout-safety",
        now: datetime | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        if not reason.strip() or not actor.strip():
            raise ValueError("halt reason and actor are required")
        current = self.get(rollout_id)
        if current.state not in {
            RolloutState.ACTIVE_GUARDED,
            RolloutState.STABLE,
        }:
            raise ValueError("rollout is not in an active state")

        with self._connect() as db:
            active = db.execute(
                "SELECT release_id FROM component_releases WHERE component=? AND state='ACTIVE'",
                (current.component,),
            ).fetchall()
        active_ids = {str(item["release_id"]) for item in active}
        if active_ids != {current.active_release_id}:
            NoTradeSafetyLatch(self.path).trip_no_trade(
                current.component,
                reason=f"{reason};rollout_active_release_identity_conflict",
                source_release_id=",".join(sorted(active_ids)),
                actor=actor,
                now=timestamp,
            )
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """
                    UPDATE deployment_rollouts
                    SET state='FAILED_SAFE',rollback_state='FAILED',updated_ts_utc=?
                    WHERE rollout_id=?
                    """,
                    (timestamp.isoformat(), rollout_id),
                )
                self._append_event(
                    db,
                    rollout_id=rollout_id,
                    from_state=current.state.value,
                    to_state=RolloutState.FAILED_SAFE,
                    rollback_state=RollbackState.FAILED,
                    actor=actor,
                    reason="active release identity conflict; failed closed without quarantine",
                    now=timestamp,
                )
                db.execute("COMMIT")
            return self.get(rollout_id)

        protected = quarantine_and_trip_no_trade(
            ReleaseRegistry(self.path),
            NoTradeSafetyLatch(self.path),
            component=current.component,
            reason=reason,
            now=timestamp,
        )
        rollback_plan = protected.rollback_plan
        target = rollback_plan.rollback_release_id if rollback_plan is not None else None
        next_state = RolloutState.HALTED if target else RolloutState.FAILED_SAFE
        next_rollback = RollbackState.PROPOSED if target else RollbackState.FAILED

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                "SELECT state FROM deployment_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            if latest is None:
                db.execute("ROLLBACK")
                raise KeyError(rollout_id)
            from_state = RolloutState(str(latest["state"]))
            db.execute(
                """
                UPDATE deployment_rollouts
                SET state=?,rollback_state=?,active_release_id='',updated_ts_utc=?
                WHERE rollout_id=?
                """,
                (next_state.value, next_rollback.value, timestamp.isoformat(), rollout_id),
            )
            if target:
                rollback_id = _hash_payload(
                    {
                        "version": "deployment-rollback-v1",
                        "rollout_id": rollout_id,
                        "target_release_id": target,
                        "quarantined_release_id": (
                            rollback_plan.quarantined_release_id
                            if rollback_plan is not None
                            else ""
                        ),
                    }
                )
                db.execute(
                    """
                    INSERT OR IGNORE INTO deployment_rollbacks
                    (rollback_id,rollout_id,target_release_id,state,proposed_ts_utc)
                    VALUES (?,?,?,'PROPOSED',?)
                    """,
                    (rollback_id, rollout_id, target, timestamp.isoformat()),
                )
            self._append_event(
                db,
                rollout_id=rollout_id,
                from_state=from_state.value,
                to_state=next_state,
                rollback_state=next_rollback,
                actor=actor,
                reason=reason,
                now=timestamp,
            )
            db.execute("COMMIT")
        return self.get(rollout_id)

    def authorize_rollback(
        self,
        rollout_id: str,
        *,
        operator: str,
        health: RolloutHealthEvidence,
        now: datetime | None = None,
    ) -> RollbackRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        if not operator.strip():
            raise ValueError("operator identity is required")
        if not health.rollback_authorization_ready:
            raise ValueError("rollback authorization evidence is not safe")
        if not _health_fresh(
            health,
            now=timestamp,
            maximum_age=self.policy.maximum_recovery_evidence_age,
        ):
            raise ValueError("rollback readiness evidence is stale or future-dated")
        current = self.get(rollout_id)
        latch = NoTradeSafetyLatch(self.path)
        if not latch.verify_integrity(current.component).valid:
            raise ValueError("component safety ledger integrity is invalid")
        if latch.state(current.component).mode is not SafetyMode.NO_TRADE:
            raise ValueError("rollback authorization requires NO_TRADE")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rollout = db.execute(
                "SELECT state FROM deployment_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            rollback = db.execute(
                "SELECT * FROM deployment_rollbacks WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            if rollout is None or rollback is None:
                db.execute("ROLLBACK")
                raise KeyError(rollout_id)
            if RolloutState(str(rollout["state"])) is not RolloutState.HALTED:
                db.execute("ROLLBACK")
                raise ValueError("rollout is not halted")
            if RollbackState(str(rollback["state"])) is not RollbackState.PROPOSED:
                db.execute("ROLLBACK")
                raise ValueError("rollback is not proposed")
            target = db.execute(
                "SELECT state FROM component_releases WHERE release_id=?",
                (str(rollback["target_release_id"]),),
            ).fetchone()
            if target is None or ReleaseState(str(target["state"])) is not ReleaseState.SUPERSEDED:
                db.execute("ROLLBACK")
                raise ValueError("rollback target is not a known superseded release")
            db.execute(
                "UPDATE deployment_rollbacks SET state='AUTHORIZED',authorized_ts_utc=?,"
                "authorized_by=? WHERE rollout_id=?",
                (timestamp.isoformat(), operator, rollout_id),
            )
            db.execute(
                """
                UPDATE deployment_rollouts
                SET state='ROLLBACK_PENDING',rollback_state='AUTHORIZED',updated_ts_utc=?
                WHERE rollout_id=?
                """,
                (timestamp.isoformat(), rollout_id),
            )
            self._append_event(
                db,
                rollout_id=rollout_id,
                from_state=RolloutState.HALTED.value,
                to_state=RolloutState.ROLLBACK_PENDING,
                rollback_state=RollbackState.AUTHORIZED,
                actor=operator,
                reason="operator authorized known-good rollback candidate",
                now=timestamp,
            )
            db.execute("COMMIT")
        return self.get_rollback(rollout_id)

    def apply_rollback(
        self,
        rollout_id: str,
        *,
        operator: str,
        now: datetime | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        if not operator.strip():
            raise ValueError("operator identity is required")
        current = self.get(rollout_id)
        latch = NoTradeSafetyLatch(self.path)
        if not latch.verify_integrity(current.component).valid:
            raise ValueError("component safety ledger integrity is invalid")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rollout = db.execute(
                "SELECT * FROM deployment_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            rollback = db.execute(
                "SELECT * FROM deployment_rollbacks WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            if rollout is None or rollback is None:
                db.execute("ROLLBACK")
                raise KeyError(rollout_id)
            if RolloutState(str(rollout["state"])) is not RolloutState.ROLLBACK_PENDING:
                db.execute("ROLLBACK")
                raise ValueError("rollout has no authorized rollback pending")
            if RollbackState(str(rollback["state"])) is not RollbackState.AUTHORIZED:
                db.execute("ROLLBACK")
                raise ValueError("rollback has not been authorized")
            authorized_by = str(rollback["authorized_by"])
            if self.policy.require_distinct_rollback_operators and operator == authorized_by:
                db.execute("ROLLBACK")
                raise ValueError("rollback application requires a distinct operator")
            if latch.state(current.component).mode is not SafetyMode.NO_TRADE:
                db.execute("ROLLBACK")
                raise ValueError("rollback application requires NO_TRADE to remain latched")
            target_id = str(rollback["target_release_id"])
            target = db.execute(
                "SELECT state FROM component_releases WHERE release_id=? AND component=?",
                (target_id, current.component),
            ).fetchone()
            if target is None or ReleaseState(str(target["state"])) is not ReleaseState.SUPERSEDED:
                db.execute("ROLLBACK")
                raise ValueError("rollback target is no longer safe to reactivate")
            db.execute(
                "UPDATE component_releases SET state='SUPERSEDED' "
                "WHERE component=? AND state='ACTIVE'",
                (current.component,),
            )
            db.execute(
                """
                UPDATE component_releases
                SET state='ACTIVE',activated_ts_utc=?,activated_by=? WHERE release_id=?
                """,
                (timestamp.isoformat(), operator, target_id),
            )
            db.execute(
                "UPDATE deployment_rollbacks SET state='VERIFYING',applied_ts_utc=?,"
                "applied_by=? WHERE rollout_id=?",
                (timestamp.isoformat(), operator, rollout_id),
            )
            db.execute(
                """
                UPDATE deployment_rollouts
                SET state='ROLLBACK_VERIFYING',rollback_state='VERIFYING',
                    active_release_id=?,updated_ts_utc=? WHERE rollout_id=?
                """,
                (target_id, timestamp.isoformat(), rollout_id),
            )
            self._append_event(
                db,
                rollout_id=rollout_id,
                from_state=RolloutState.ROLLBACK_PENDING.value,
                to_state=RolloutState.ROLLBACK_VERIFYING,
                rollback_state=RollbackState.VERIFYING,
                actor=operator,
                reason="authorized rollback applied while NO_TRADE remains latched",
                now=timestamp,
            )
            db.execute("COMMIT")
        return self.get(rollout_id)

    def verify_rollback(
        self,
        rollout_id: str,
        *,
        verifier: str,
        health: RolloutHealthEvidence,
        now: datetime | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        if not verifier.strip():
            raise ValueError("verifier identity is required")
        current = self.get(rollout_id)
        latch = NoTradeSafetyLatch(self.path)
        integrity_valid = latch.verify_integrity(current.component).valid
        fresh = _health_fresh(
            health,
            now=timestamp,
            maximum_age=self.policy.maximum_recovery_evidence_age,
        )
        healthy = health.recovery_ready and fresh and integrity_valid
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rollout = db.execute(
                "SELECT * FROM deployment_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            rollback = db.execute(
                "SELECT * FROM deployment_rollbacks WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
            if rollout is None or rollback is None:
                db.execute("ROLLBACK")
                raise KeyError(rollout_id)
            if RolloutState(str(rollout["state"])) is not RolloutState.ROLLBACK_VERIFYING:
                db.execute("ROLLBACK")
                raise ValueError("rollback is not awaiting verification")
            target_id = str(rollback["target_release_id"])
            active = db.execute(
                "SELECT release_id FROM component_releases WHERE component=? AND state='ACTIVE'",
                (current.component,),
            ).fetchall()
            if {str(item["release_id"]) for item in active} != {target_id}:
                healthy = False
            if latch.state(current.component).mode is not SafetyMode.NO_TRADE:
                healthy = False
            next_state = (
                RolloutState.RECOVERED_GUARDED if healthy else RolloutState.FAILED_SAFE
            )
            next_rollback = RollbackState.RECOVERED if healthy else RollbackState.FAILED
            failure_reason = "" if healthy else "rollback_verification_failed"
            db.execute(
                """
                UPDATE deployment_rollbacks
                SET state=?,verified_ts_utc=?,verified_by=?,failure_reason=?
                WHERE rollout_id=?
                """,
                (
                    next_rollback.value,
                    timestamp.isoformat(),
                    verifier,
                    failure_reason,
                    rollout_id,
                ),
            )
            db.execute(
                """
                UPDATE deployment_rollouts
                SET state=?,rollback_state=?,updated_ts_utc=? WHERE rollout_id=?
                """,
                (next_state.value, next_rollback.value, timestamp.isoformat(), rollout_id),
            )
            self._append_event(
                db,
                rollout_id=rollout_id,
                from_state=RolloutState.ROLLBACK_VERIFYING.value,
                to_state=next_state,
                rollback_state=next_rollback,
                actor=verifier,
                reason=(
                    "rollback verification passed; NO_TRADE remains latched"
                    if healthy
                    else "rollback verification failed; remain fail-closed"
                ),
                now=timestamp,
            )
            db.execute("COMMIT")
        return self.get(rollout_id)

    def resume_recovered(
        self,
        rollout_id: str,
        *,
        operator: str,
        reason: str,
        health: RolloutHealthEvidence,
        now: datetime | None = None,
    ) -> RolloutRecord:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        if not operator.strip() or not reason.strip():
            raise ValueError("operator identity and resume reason are required")
        if not health.recovery_ready:
            raise ValueError("recovery health evidence is not healthy")
        if not _health_fresh(
            health,
            now=timestamp,
            maximum_age=self.policy.maximum_recovery_evidence_age,
        ):
            raise ValueError("recovery health evidence is stale or future-dated")
        current = self.get(rollout_id)
        if current.state not in {RolloutState.RECOVERED_GUARDED, RolloutState.RESUME_PENDING}:
            raise ValueError("rollout is not recovered or awaiting resume finalization")
        latch = NoTradeSafetyLatch(self.path)
        if not latch.verify_integrity(current.component).valid:
            raise ValueError("component safety ledger integrity is invalid")
        rollback = self.get_rollback(rollout_id)

        if current.state is RolloutState.RECOVERED_GUARDED:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE deployment_rollouts SET state='RESUME_PENDING',updated_ts_utc=? "
                    "WHERE rollout_id=? AND state='RECOVERED_GUARDED'",
                    (timestamp.isoformat(), rollout_id),
                )
                self._append_event(
                    db,
                    rollout_id=rollout_id,
                    from_state=RolloutState.RECOVERED_GUARDED.value,
                    to_state=RolloutState.RESUME_PENDING,
                    rollback_state=RollbackState.RECOVERED,
                    actor=operator,
                    reason="operator requested recovery resume; NO_TRADE still latched",
                    now=timestamp,
                )
                db.execute("COMMIT")

        try:
            cleared = latch.clear_no_trade(
                current.component,
                operator=operator,
                reason=reason,
                source_release_id=rollback.target_release_id,
                now=timestamp,
            )
            binding_ok = (
                cleared.execution_allowed
                and cleared.source_release_id == rollback.target_release_id
            )
            if not binding_ok:
                raise RuntimeError("safety latch did not bind recovered release")
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT state FROM deployment_rollouts WHERE rollout_id=?",
                    (rollout_id,),
                ).fetchone()
                if row is None or RolloutState(str(row["state"])) is not RolloutState.RESUME_PENDING:
                    db.execute("ROLLBACK")
                    raise ValueError("rollout state changed during recovery resume")
                active = db.execute(
                    "SELECT release_id FROM component_releases "
                    "WHERE component=? AND state='ACTIVE'",
                    (current.component,),
                ).fetchall()
                if {str(item["release_id"]) for item in active} != {rollback.target_release_id}:
                    db.execute("ROLLBACK")
                    raise ValueError("recovered active release changed during resume")
                db.execute(
                    """
                    UPDATE deployment_rollouts
                    SET state='ACTIVE_GUARDED',generation=generation+1,activated_ts_utc=?,
                        stable_ts_utc=NULL,updated_ts_utc=? WHERE rollout_id=?
                    """,
                    (timestamp.isoformat(), timestamp.isoformat(), rollout_id),
                )
                self._append_event(
                    db,
                    rollout_id=rollout_id,
                    from_state=RolloutState.RESUME_PENDING.value,
                    to_state=RolloutState.ACTIVE_GUARDED,
                    rollback_state=RollbackState.RECOVERED,
                    actor=operator,
                    reason="verified rollback resumed into a fresh guarded soak",
                    now=timestamp,
                )
                db.execute("COMMIT")
        except Exception:
            latch.trip_no_trade(
                current.component,
                reason="recovery_resume_finalization_failed",
                source_release_id=rollback.target_release_id,
                actor="deployment-state-machine",
                now=timestamp,
            )
            raise
        return self.get(rollout_id)

    def get(self, rollout_id: str) -> RolloutRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM deployment_rollouts WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
        if row is None:
            raise KeyError(rollout_id)
        return _rollout_from_row(row)

    def get_rollback(self, rollout_id: str) -> RollbackRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM deployment_rollbacks WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
        if row is None:
            raise KeyError(rollout_id)
        return _rollback_from_row(row)

    def verify_integrity(self, rollout_id: str) -> RolloutIntegrityReport:
        rollout = self.get(rollout_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM deployment_rollout_events WHERE rollout_id=? ORDER BY seq",
                (rollout_id,),
            ).fetchall()
            checkpoint = db.execute(
                "SELECT * FROM deployment_rollout_integrity_state WHERE rollout_id=?",
                (rollout_id,),
            ).fetchone()
        failures: list[str] = []
        contiguous = [int(row["seq"]) for row in rows] == list(range(1, len(rows) + 1))
        if not contiguous:
            failures.append("deployment_event_sequence_gap")
        previous_hash = ""
        chain_checkpoint = ""
        event_hashes_valid = True
        event_ids_valid = True
        chain_valid = True
        for row in rows:
            if str(row["previous_event_hash"]) != previous_hash:
                chain_valid = False
            expected_hash = _event_hash(
                rollout_id=rollout_id,
                seq=int(row["seq"]),
                from_state=str(row["from_state"]),
                to_state=str(row["to_state"]),
                rollback_state=str(row["rollback_state"]),
                actor=str(row["actor"]),
                reason=str(row["reason"]),
                created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC),
                previous_event_hash=str(row["previous_event_hash"]),
            )
            identifier = str(row["event_id"])
            if expected_hash != str(row["event_hash"]):
                event_hashes_valid = False
            if _event_id(rollout_id, int(row["seq"]), str(row["event_hash"])) != identifier:
                event_ids_valid = False
            chain_checkpoint = hashlib.sha256(
                f"{chain_checkpoint}|{identifier}".encode()
            ).hexdigest()
            previous_hash = str(row["event_hash"])
        if not event_hashes_valid:
            failures.append("deployment_event_hash_mismatch")
        if not event_ids_valid:
            failures.append("deployment_event_id_mismatch")
        if not chain_valid:
            failures.append("deployment_event_chain_mismatch")
        checkpoint_matches = checkpoint is not None and (
            int(checkpoint["event_count"]) == len(rows)
            and str(checkpoint["head_event_hash"]) == (previous_hash if rows else "")
            and str(checkpoint["chain_hash"]) == chain_checkpoint
        )
        if not checkpoint_matches:
            failures.append("deployment_integrity_checkpoint_mismatch")
        materialized = bool(rows) and (
            str(rows[-1]["to_state"]) == rollout.state.value
            and str(rows[-1]["rollback_state"]) == rollout.rollback_state.value
        )
        if not materialized:
            failures.append("deployment_materialized_state_mismatch")
        return RolloutIntegrityReport(
            rollout_id=rollout_id,
            events=len(rows),
            contiguous_sequence=contiguous,
            event_hashes_valid=event_hashes_valid,
            event_ids_valid=event_ids_valid,
            chain_valid=chain_valid,
            checkpoint_matches_history=checkpoint_matches,
            materialized_state_matches=materialized,
            valid=not failures,
            failures=tuple(failures),
        )


def _parse_optional(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value)).astimezone(UTC)


def _rollout_from_row(row: sqlite3.Row) -> RolloutRecord:
    return RolloutRecord(
        rollout_id=str(row["rollout_id"]),
        component=str(row["component"]),
        candidate_release_id=str(row["candidate_release_id"]),
        previous_release_id=str(row["previous_release_id"]),
        active_release_id=str(row["active_release_id"]),
        dossier_id=str(row["dossier_id"]),
        authorization_id=str(row["authorization_id"]),
        authorization_expires_ts_utc=datetime.fromisoformat(
            str(row["authorization_expires_ts_utc"])
        ).astimezone(UTC),
        evidence_bundle_hash=str(row["evidence_bundle_hash"]),
        preparation_audit_hash=str(row["preparation_audit_hash"]),
        state=RolloutState(str(row["state"])),
        rollback_state=RollbackState(str(row["rollback_state"])),
        generation=int(row["generation"]),
        created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC),
        updated_ts_utc=datetime.fromisoformat(str(row["updated_ts_utc"])).astimezone(UTC),
        activated_ts_utc=_parse_optional(row["activated_ts_utc"]),
        stable_ts_utc=_parse_optional(row["stable_ts_utc"]),
    )


def _rollback_from_row(row: sqlite3.Row) -> RollbackRecord:
    return RollbackRecord(
        rollback_id=str(row["rollback_id"]),
        rollout_id=str(row["rollout_id"]),
        target_release_id=str(row["target_release_id"]),
        state=RollbackState(str(row["state"])),
        proposed_ts_utc=datetime.fromisoformat(str(row["proposed_ts_utc"])).astimezone(UTC),
        authorized_ts_utc=_parse_optional(row["authorized_ts_utc"]),
        authorized_by=str(row["authorized_by"]),
        applied_ts_utc=_parse_optional(row["applied_ts_utc"]),
        applied_by=str(row["applied_by"]),
        verified_ts_utc=_parse_optional(row["verified_ts_utc"]),
        verified_by=str(row["verified_by"]),
        failure_reason=str(row["failure_reason"]),
    )
