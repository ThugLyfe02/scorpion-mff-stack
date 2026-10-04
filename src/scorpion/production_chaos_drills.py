from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .delivery import DeliveryLedger
from .fail_safe_control import NoTradeSafetyLatch, SafetyMode
from .kill_switch_drills import run_isolated_kill_switch_recovery_drill
from .production_bottleneck_audit import (
    ProductionBottleneckAuditPolicy,
    audit_production_bottlenecks,
)
from .store import Store


@dataclass(frozen=True, slots=True)
class ChaosDrillCase:
    name: str
    passed: bool
    fail_closed_observed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PartialOutageChaosReport:
    drill_id: str
    cases: tuple[ChaosDrillCase, ...]
    passed: bool
    failures: tuple[str, ...]
    workspace: str


def _workspace(directory: str | Path, now: datetime) -> Path:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    suffix = hashlib.sha256(f"partial-outage-v2|{now.isoformat()}".encode()).hexdigest()[:16]
    workspace = root / f"scorpion-partial-outage-{suffix}"
    if workspace.exists():
        raise FileExistsError(workspace)
    workspace.mkdir()
    return workspace


def _seed_heartbeat(
    path: Path,
    *,
    component: str,
    timestamp: datetime,
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
            (
                component,
                timestamp.astimezone(UTC).isoformat(),
                json.dumps(metadata, sort_keys=True),
            ),
        )


def _healthy_liveness(path: Path, now: datetime) -> None:
    _seed_heartbeat(
        path,
        component="resilience-watchdog",
        timestamp=now,
        metadata={"status": "ok"},
    )
    _seed_heartbeat(
        path,
        component="discord-ingress-queue",
        timestamp=now,
        metadata={"status": "healthy", "utilization": 0.10, "consumer_alive": True},
    )


def run_isolated_partial_outage_chaos_drills(
    directory: str | Path,
    *,
    component: str,
    operator: str,
    now: datetime | None = None,
) -> PartialOutageChaosReport:
    """Inject control-plane outages only into isolated SQLite drill stores."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    if not component.strip() or not operator.strip():
        raise ValueError("component and operator are required")
    workspace = _workspace(directory, timestamp)
    cases: list[ChaosDrillCase] = []
    failures: list[str] = []

    kill = run_isolated_kill_switch_recovery_drill(
        workspace,
        component=component,
        operator=operator,
        now=timestamp,
    )
    cases.append(
        ChaosDrillCase(
            name="kill_switch_restart_and_corruption",
            passed=kill.passed,
            fail_closed_observed=kill.trip_blocks_execution and kill.corruption_detected,
            detail="NO_TRADE survived restart and corrupted safety evidence was detected",
        )
    )

    audit_path = workspace / "bottleneck-audit.db"
    Store(audit_path)
    _healthy_liveness(audit_path, timestamp)
    with sqlite3.connect(str(audit_path)) as db:
        db.execute(
            "UPDATE heartbeats SET last_seen_ts_utc=? WHERE component='resilience-watchdog'",
            ((timestamp - timedelta(minutes=1)).isoformat(),),
        )
    stale = audit_production_bottlenecks(
        audit_path,
        now=timestamp,
        policy=ProductionBottleneckAuditPolicy(maximum_heartbeat_age_seconds=5.0),
    )
    stale_detected = any(
        item.code == "stale_heartbeat:resilience-watchdog" for item in stale.findings
    )
    cases.append(
        ChaosDrillCase(
            name="watchdog_partial_outage",
            passed=stale_detected and not stale.ready_for_rollout,
            fail_closed_observed=not stale.ready_for_rollout,
            detail="stale watchdog heartbeat blocked rollout readiness",
        )
    )

    _healthy_liveness(audit_path, timestamp)
    _seed_heartbeat(
        audit_path,
        component="discord-ingress-queue",
        timestamp=timestamp,
        metadata={"status": "capture_only", "utilization": 0.02, "consumer_alive": False},
    )
    capture_only = audit_production_bottlenecks(audit_path, now=timestamp)
    capture_detected = any(
        item.code == "ingress_consumer_not_alive" for item in capture_only.findings
    )
    cases.append(
        ChaosDrillCase(
            name="fresh_capture_only_ingress",
            passed=capture_detected and not capture_only.ready_for_rollout,
            fail_closed_observed=not capture_only.ready_for_rollout,
            detail="fresh heartbeat could not hide a dead normalized-ingress consumer",
        )
    )

    _healthy_liveness(audit_path, timestamp)
    DeliveryLedger(audit_path)
    with sqlite3.connect(str(audit_path)) as db:
        db.execute(
            """
            INSERT INTO execution_delivery_ledger
            (delivery_id,event_id,effect_kind,contract_key,generation,quantity,limit_price,
             state,attempt_count,lease_owner,lease_until_utc,created_ts_utc,updated_ts_utc)
            VALUES (?,?,?,?,?,?,?,'LEASED',1,?,?,?,?)
            """,
            (
                "chaos-delivery",
                "chaos-event",
                "ENTRY",
                "TEST|2099-01-01|C|1",
                1,
                1,
                "1.00",
                "chaos-worker",
                (timestamp - timedelta(seconds=1)).isoformat(),
                (timestamp - timedelta(seconds=5)).isoformat(),
                (timestamp - timedelta(seconds=5)).isoformat(),
            ),
        )
    expired = audit_production_bottlenecks(audit_path, now=timestamp)
    lease_detected = any(item.code == "expired_delivery_leases" for item in expired.findings)
    cases.append(
        ChaosDrillCase(
            name="delivery_worker_partial_outage",
            passed=lease_detected and not expired.ready_for_rollout,
            fail_closed_observed=not expired.ready_for_rollout,
            detail="expired delivery lease was surfaced as a rollout-blocking fault",
        )
    )

    safety_path = workspace / "safety-split-brain.db"
    Store(safety_path)
    latch = NoTradeSafetyLatch(safety_path)
    latch.clear_no_trade(
        component,
        operator=operator,
        reason="chaos initialize normal",
        source_release_id="chaos-release",
        now=timestamp,
    )
    latch.trip_no_trade(
        component,
        reason="chaos emergency trip",
        source_release_id="chaos-release",
        actor="chaos-safety-monitor",
        now=timestamp + timedelta(seconds=1),
    )
    with sqlite3.connect(str(safety_path)) as db:
        db.execute(
            "UPDATE component_safety_state SET mode='NORMAL',reason='tampered normal' "
            "WHERE component=?",
            (component,),
        )
    split_integrity = latch.verify_integrity(component)
    sentinel_still_blocks = latch.state(component).mode is SafetyMode.NO_TRADE
    split_detected = (
        not split_integrity.valid
        and "safety_state_does_not_match_latest_event" in split_integrity.failures
    )
    cases.append(
        ChaosDrillCase(
            name="safety_db_sentinel_split_brain",
            passed=split_detected and sentinel_still_blocks,
            fail_closed_observed=sentinel_still_blocks,
            detail="external sentinel blocked execution while SQLite safety state was tampered",
        )
    )

    lock_path = workspace / "writer-contention.db"
    Store(lock_path)
    holder = sqlite3.connect(str(lock_path), timeout=0.1, isolation_level=None)
    contender = sqlite3.connect(str(lock_path), timeout=0.01, isolation_level=None)
    writer_contention_detected = False
    try:
        holder.execute("BEGIN IMMEDIATE")
        try:
            contender.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            writer_contention_detected = True
        else:
            contender.execute("ROLLBACK")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        contender.close()
    cases.append(
        ChaosDrillCase(
            name="sqlite_writer_contention",
            passed=writer_contention_detected,
            fail_closed_observed=writer_contention_detected,
            detail="competing writer failed before it could mutate the evidence store",
        )
    )

    for case in cases:
        if not case.passed:
            failures.append(case.name)
    drill_id = hashlib.sha256(
        (
            "partial-outage-chaos-v2|"
            f"{component}|{timestamp.isoformat()}|"
            + "|".join(f"{case.name}:{case.passed}" for case in cases)
        ).encode()
    ).hexdigest()
    return PartialOutageChaosReport(
        drill_id=drill_id,
        cases=tuple(cases),
        passed=not failures,
        failures=tuple(failures),
        workspace=str(workspace),
    )
