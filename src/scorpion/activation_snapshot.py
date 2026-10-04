from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .production_bottleneck_audit import ProductionBottleneckAuditPolicy
from .production_bottleneck_snapshot import audit_production_bottlenecks_connection
from .production_readiness import (
    activation_bottleneck_fingerprint,
    activation_control_fingerprint,
    capture_control_state_binding,
)
from .readiness_capability_journal import (
    ReadinessCapabilityEventKind,
    verify_readiness_capability_journal_connection,
)
from .schema_contract import inspect_schema_connection
from .schema_migrations import TARGET_SCHEMA_VERSION, verify_schema_migration_ledger_connection


@dataclass(frozen=True, slots=True)
class ActivationSnapshotCertification:
    snapshot_hash: str
    schema_version: str
    migration_head_hash: str
    migration_chain_hash: str
    readiness_journal_head_hash: str
    readiness_journal_chain_hash: str
    control_hash: str
    bottleneck_hash: str
    active_release_ids: tuple[str, ...]
    candidate_state: str
    passed: bool
    failures: tuple[str, ...]


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def certify_activation_snapshot(
    db: sqlite3.Connection,
    db_path: str | Path,
    *,
    component: str,
    rollout_id: str,
    certificate_id: str,
    candidate_release_id: str,
    previous_release_id: str,
    policy: ProductionBottleneckAuditPolicy,
    now: datetime,
) -> ActivationSnapshotCertification:
    """Certify activation-relevant production truth from one SQLite transaction snapshot."""
    timestamp = now.astimezone(UTC)
    db.row_factory = sqlite3.Row
    failures: list[str] = []

    schema = inspect_schema_connection(db)
    if not schema.compatible:
        failures.extend(f"schema:{item}" for item in schema.failures)

    migration = verify_schema_migration_ledger_connection(db)
    if not migration.valid:
        failures.extend(f"migration:{item}" for item in migration.failures)
    if migration.current_version != TARGET_SCHEMA_VERSION:
        failures.append("migration:target_schema_version_mismatch")

    journal = verify_readiness_capability_journal_connection(
        db,
        certificate_id=certificate_id,
        expected_latest_kind=ReadinessCapabilityEventKind.CONSUMED,
    )
    if not journal.valid:
        failures.extend(f"readiness_journal:{item}" for item in journal.failures)
    if journal.latest_rollout_id != rollout_id:
        failures.append("readiness_journal:rollout_binding_mismatch")

    audit = audit_production_bottlenecks_connection(
        db,
        db_path,
        now=timestamp,
        policy=policy,
    )
    if not audit.ready_for_rollout:
        failures.append("bottleneck:activation_snapshot_not_rollout_safe")

    control = capture_control_state_binding(
        db,
        component=component,
        required_heartbeats=policy.required_heartbeats,
    )
    control_hash = activation_control_fingerprint(
        control,
        prepared_rollout_id=rollout_id,
    )
    bottleneck_hash = activation_bottleneck_fingerprint(audit)
    expected_active = (previous_release_id,) if previous_release_id else ()
    if control.active_release_ids != expected_active:
        failures.append("control:active_predecessor_mismatch")
    if control.safety_mode != "NORMAL":
        failures.append("control:safety_not_normal")
    if previous_release_id and control.safety_source_release_id != previous_release_id:
        failures.append("control:safety_predecessor_binding_mismatch")

    candidate = db.execute(
        "SELECT component,state,previous_release_id FROM component_releases WHERE release_id=?",
        (candidate_release_id,),
    ).fetchone()
    candidate_state = str(candidate["state"]) if candidate is not None else ""
    if candidate is None:
        failures.append("candidate:missing")
    else:
        if str(candidate["component"]) != component:
            failures.append("candidate:component_mismatch")
        if str(candidate["previous_release_id"] or "") != previous_release_id:
            failures.append("candidate:predecessor_mismatch")
        if candidate_state not in {"CANDIDATE", "SUPERSEDED"}:
            failures.append("candidate:not_activatable")

    capability = db.execute(
        """
        SELECT component,candidate_release_id,consumed_rollout_id,certificate_expires_ts_utc,
               bottleneck_policy_sha256,safety_event_count,safety_head_event_id,safety_chain_hash
        FROM production_readiness_consumptions WHERE certificate_id=?
        """,
        (certificate_id,),
    ).fetchone()
    if capability is None:
        failures.append("capability:materialized_row_missing")
        capability_material: dict[str, object] = {}
    else:
        if str(capability["component"]) != component:
            failures.append("capability:component_mismatch")
        if str(capability["candidate_release_id"]) != candidate_release_id:
            failures.append("capability:candidate_mismatch")
        if str(capability["consumed_rollout_id"]) != rollout_id:
            failures.append("capability:consumed_rollout_mismatch")
        if timestamp > datetime.fromisoformat(
            str(capability["certificate_expires_ts_utc"])
        ).astimezone(UTC):
            failures.append("capability:expired")
        capability_material = {
            "bottleneck_policy_sha256": str(capability["bottleneck_policy_sha256"]),
            "safety_event_count": int(capability["safety_event_count"]),
            "safety_head_event_id": str(capability["safety_head_event_id"]),
            "safety_chain_hash": str(capability["safety_chain_hash"]),
        }
        if int(capability["safety_event_count"]) != control.safety_event_count:
            failures.append("capability:safety_generation_mismatch")
        if str(capability["safety_head_event_id"]) != control.safety_head_event_id:
            failures.append("capability:safety_head_mismatch")
        if str(capability["safety_chain_hash"]) != control.safety_chain_hash:
            failures.append("capability:safety_chain_mismatch")

    material = {
        "version": "activation-single-snapshot-v1",
        "schema_contract_version": schema.contract_version,
        "schema_compatible": schema.compatible,
        "migration_version": migration.current_version,
        "migration_events": migration.events,
        "migration_head_hash": migration.head_event_hash,
        "migration_chain_hash": migration.chain_hash,
        "readiness_journal_events": journal.events,
        "readiness_journal_head_hash": journal.head_event_hash,
        "readiness_journal_chain_hash": journal.chain_hash,
        "readiness_journal_latest_kind": journal.latest_kind,
        "component": component,
        "rollout_id": rollout_id,
        "certificate_id": certificate_id,
        "candidate_release_id": candidate_release_id,
        "previous_release_id": previous_release_id,
        "candidate_state": candidate_state,
        "active_release_ids": control.active_release_ids,
        "control_hash": control_hash,
        "bottleneck_hash": bottleneck_hash,
        "capability": capability_material,
    }
    return ActivationSnapshotCertification(
        snapshot_hash=_hash_payload(material),
        schema_version=migration.current_version,
        migration_head_hash=migration.head_event_hash,
        migration_chain_hash=migration.chain_hash,
        readiness_journal_head_hash=journal.head_event_hash,
        readiness_journal_chain_hash=journal.chain_hash,
        control_hash=control_hash,
        bottleneck_hash=bottleneck_hash,
        active_release_ids=control.active_release_ids,
        candidate_state=candidate_state,
        passed=not failures,
        failures=tuple(failures),
    )
