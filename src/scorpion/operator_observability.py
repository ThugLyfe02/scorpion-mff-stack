from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .production_bottleneck_audit import (
    ProductionBottleneckAuditPolicy,
    ProductionBottleneckAuditReport,
    audit_production_bottlenecks,
)
from .schema_contract import inspect_schema
from .stage_trace import StageLatencyReport, load_stage_latency_report


class OperatorSystemState(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAIL_CLOSED = "FAIL_CLOSED"


@dataclass(frozen=True, slots=True)
class OperatorComponentStatus:
    component: str
    active_release_id: str
    safety_mode: str
    safety_source_release_id: str
    safety_initialized: bool
    emergency_sentinel_active: bool
    rollout_id: str
    rollout_state: str
    rollback_state: str
    active_shadow_release_id: str
    consistent: bool
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperatorObservabilitySnapshot:
    snapshot_id: str
    generated_ts_utc: datetime
    system_state: OperatorSystemState
    schema_compatible: bool
    runtime_halted: bool
    halt_reason: str
    bottleneck_report: ProductionBottleneckAuditReport
    latency_report: StageLatencyReport
    components: tuple[OperatorComponentStatus, ...]
    pending_raw_revisions: int
    pending_review_effects: int
    pending_deliveries: int
    expired_delivery_leases: int
    stale_required_heartbeats: tuple[str, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    next_actions: tuple[str, ...]


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _sentinel_path(path: Path, component: str) -> Path:
    suffix = hashlib.sha256(component.encode()).hexdigest()[:20]
    return Path(f"{path}.{suffix}.NO_TRADE")


def _runtime_halt(db: sqlite3.Connection) -> tuple[bool, str]:
    if not _table_exists(db, "runtime_flags"):
        return True, "runtime_flags_missing"
    row = db.execute("SELECT value FROM runtime_flags WHERE key='halt'").fetchone()
    if row is None:
        return False, ""
    try:
        payload = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return True, "runtime_halt_payload_invalid"
    return bool(payload.get("halted", False)), str(payload.get("reason", ""))


def _component_names(db: sqlite3.Connection) -> tuple[str, ...]:
    names: set[str] = set()
    for table in ("component_releases", "component_safety_state", "deployment_rollouts"):
        if not _table_exists(db, table):
            continue
        rows = db.execute(f"SELECT DISTINCT component FROM {table}").fetchall()
        names.update(str(row[0]) for row in rows if str(row[0]).strip())
    return tuple(sorted(names))


def _active_release(db: sqlite3.Connection, component: str) -> str:
    if not _table_exists(db, "component_releases"):
        return ""
    rows = db.execute(
        "SELECT release_id FROM component_releases WHERE component=? AND state='ACTIVE' "
        "ORDER BY activated_ts_utc DESC,registered_ts_utc DESC",
        (component,),
    ).fetchall()
    return str(rows[0][0]) if len(rows) == 1 else ""


def _safety(db: sqlite3.Connection, component: str) -> tuple[str, str, bool]:
    if not _table_exists(db, "component_safety_state"):
        return "", "", False
    row = db.execute(
        "SELECT mode,source_release_id FROM component_safety_state WHERE component=?",
        (component,),
    ).fetchone()
    if row is None:
        return "", "", False
    return str(row[0]), str(row[1]), True


def _rollout(db: sqlite3.Connection, component: str) -> tuple[str, str, str]:
    if not _table_exists(db, "deployment_rollouts"):
        return "", "", ""
    row = db.execute(
        """
        SELECT rollout_id,state,rollback_state
        FROM deployment_rollouts
        WHERE component=? AND state NOT IN ('SUPERSEDED','CANCELLED','EXPIRED')
        ORDER BY updated_ts_utc DESC LIMIT 1
        """,
        (component,),
    ).fetchone()
    if row is None:
        return "", "", ""
    return str(row[0]), str(row[1]), str(row[2])


def _shadow(db: sqlite3.Connection, component: str) -> str:
    if not _table_exists(db, "shadow_model_releases"):
        return ""
    row = db.execute(
        """
        SELECT shadow_release_id FROM shadow_model_releases
        WHERE component=? AND state='ACTIVE_SHADOW'
        ORDER BY activated_ts_utc DESC,created_ts_utc DESC LIMIT 1
        """,
        (component,),
    ).fetchone()
    return str(row[0]) if row is not None else ""


def _count(db: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> int:
    row = db.execute(query, params).fetchone()
    return int(row[0]) if row is not None else 0


def _component_status(
    db: sqlite3.Connection,
    db_path: Path,
    component: str,
) -> OperatorComponentStatus:
    active_release = _active_release(db, component)
    safety_mode, safety_release, initialized = _safety(db, component)
    rollout_id, rollout_state, rollback_state = _rollout(db, component)
    shadow = _shadow(db, component)
    sentinel = _sentinel_path(db_path, component).exists()
    failures: list[str] = []

    if active_release:
        if not initialized:
            failures.append("active_release_has_no_initialized_safety_state")
        if safety_mode != "NORMAL":
            failures.append("active_release_not_in_normal_safety_mode")
        if safety_release != active_release:
            failures.append("active_release_safety_binding_mismatch")
        if sentinel:
            failures.append("active_release_has_emergency_no_trade_sentinel")
    elif safety_mode == "NORMAL":
        failures.append("normal_safety_state_without_active_release")

    if rollout_state in {"FAILED_SAFE", "HALTED", "ROLLBACK_PENDING", "ROLLBACK_VERIFYING"}:
        failures.append(f"deployment_state:{rollout_state}")

    return OperatorComponentStatus(
        component=component,
        active_release_id=active_release,
        safety_mode=safety_mode,
        safety_source_release_id=safety_release,
        safety_initialized=initialized,
        emergency_sentinel_active=sentinel,
        rollout_id=rollout_id,
        rollout_state=rollout_state,
        rollback_state=rollback_state,
        active_shadow_release_id=shadow,
        consistent=not failures,
        failures=tuple(failures),
    )


def build_operator_observability_snapshot(
    path: str | Path,
    *,
    now: datetime | None = None,
    bottleneck_policy: ProductionBottleneckAuditPolicy | None = None,
    stage_limit: int = 500,
) -> OperatorObservabilitySnapshot:
    """Build one read-only, hashed operator view across production control-plane seams."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    db_path = Path(path)
    if not db_path.is_file():
        raise FileNotFoundError(db_path)

    schema = inspect_schema(db_path)
    bottleneck = audit_production_bottlenecks(
        db_path,
        now=timestamp,
        policy=bottleneck_policy,
    )
    latency = load_stage_latency_report(db_path, limit=stage_limit)
    blockers: list[str] = []
    warnings: list[str] = []

    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        runtime_halted, halt_reason = _runtime_halt(db)
        components = tuple(
            _component_status(db, db_path, component) for component in _component_names(db)
        )
        pending_deliveries = (
            _count(
                db,
                "SELECT COUNT(*) FROM execution_delivery_ledger "
                "WHERE state IN ('PREPARED','LEASED')",
            )
            if _table_exists(db, "execution_delivery_ledger")
            else 0
        )
        expired_leases = (
            _count(
                db,
                "SELECT COUNT(*) FROM execution_delivery_ledger "
                "WHERE state='LEASED' AND lease_until_utc IS NOT NULL AND lease_until_utc <= ?",
                (timestamp.isoformat(),),
            )
            if _table_exists(db, "execution_delivery_ledger")
            else 0
        )

    if not schema.compatible:
        blockers.extend(f"schema:{failure}" for failure in schema.failures)
    if runtime_halted:
        blockers.append(f"runtime_halted:{halt_reason or 'unspecified'}")
    blockers.extend(
        f"bottleneck:{finding.code}"
        for finding in bottleneck.findings
        if finding.blocks_rollout
    )
    warnings.extend(
        f"bottleneck:{finding.code}"
        for finding in bottleneck.findings
        if not finding.blocks_rollout
    )
    for component in components:
        blockers.extend(
            f"component:{component.component}:{failure}" for failure in component.failures
        )

    if blockers:
        state = OperatorSystemState.FAIL_CLOSED
    elif warnings:
        state = OperatorSystemState.DEGRADED
    else:
        state = OperatorSystemState.READY

    actions: list[str] = []
    if any("normalized_ingress_consumer_unhealthy" in item for item in blockers):
        actions.append("RECOVER_NORMALIZED_CONSUMER")
    if bottleneck.snapshot.pending_raw:
        actions.append("DRAIN_DURABLE_RAW_BACKLOG")
    if expired_leases:
        actions.append("RECONCILE_EXPIRED_DELIVERY_LEASES")
    if any("safety_binding_mismatch" in item for item in blockers):
        actions.append("RECONCILE_ACTIVE_RELEASE_SAFETY_BINDING")
    if any("runtime_halted" in item for item in blockers):
        actions.append("INVESTIGATE_RUNTIME_HALT_BEFORE_RESUME")
    if not schema.compatible:
        actions.append("COMPLETE_SCHEMA_MIGRATION_AND_RECERTIFY")
    if warnings:
        actions.append("CLEAR_NONBLOCKING_OPERATIONAL_DEBT")

    material = {
        "version": "operator-observability-v1",
        "generated_ts_utc": timestamp.isoformat(),
        "system_state": state.value,
        "schema_compatible": schema.compatible,
        "runtime_halted": runtime_halted,
        "halt_reason": halt_reason,
        "bottleneck_report_hash": bottleneck.report_hash,
        "components": [asdict(item) for item in components],
        "pending_raw_revisions": bottleneck.snapshot.pending_raw,
        "pending_review_effects": bottleneck.snapshot.pending_review_effects,
        "pending_deliveries": pending_deliveries,
        "expired_delivery_leases": expired_leases,
        "blockers": blockers,
        "warnings": warnings,
        "next_actions": actions,
    }
    snapshot_id = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return OperatorObservabilitySnapshot(
        snapshot_id=snapshot_id,
        generated_ts_utc=timestamp,
        system_state=state,
        schema_compatible=schema.compatible,
        runtime_halted=runtime_halted,
        halt_reason=halt_reason,
        bottleneck_report=bottleneck,
        latency_report=latency,
        components=components,
        pending_raw_revisions=bottleneck.snapshot.pending_raw,
        pending_review_effects=bottleneck.snapshot.pending_review_effects,
        pending_deliveries=pending_deliveries,
        expired_delivery_leases=expired_leases,
        stale_required_heartbeats=bottleneck.snapshot.stale_required_heartbeats,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        next_actions=tuple(dict.fromkeys(actions)),
    )
