from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .fail_safe_control import NoTradeSafetyLatch, SafetyMode


@dataclass(frozen=True, slots=True)
class KillSwitchDrillPolicy:
    require_restart_persistence: bool = True
    require_integrity_verification: bool = True
    require_operator_recovery: bool = True


@dataclass(frozen=True, slots=True)
class KillSwitchDrillReport:
    drill_id: str
    component: str
    cold_start_fail_closed: bool
    trip_blocks_execution: bool
    restart_preserves_trip: bool
    integrity_verified_before_recovery: bool
    operator_recovery_restores_normal: bool
    restart_preserves_recovery: bool
    corruption_detected: bool
    passed: bool
    failures: tuple[str, ...]
    drill_db_path: str


def _drill_path(directory: str | Path, component: str, now: datetime) -> Path:
    material = f"{component}|{now.astimezone(UTC).isoformat()}".encode()
    suffix = hashlib.sha256(material).hexdigest()[:16]
    path = Path(directory) / f"scorpion-kill-switch-drill-{suffix}.db"
    if path.exists():
        raise FileExistsError(path)
    return path


def _execution_blocked(latch: NoTradeSafetyLatch, component: str) -> bool:
    try:
        latch.assert_execution_allowed(component)
    except RuntimeError:
        return True
    return False


def run_isolated_kill_switch_recovery_drill(
    directory: str | Path,
    *,
    component: str,
    operator: str,
    now: datetime | None = None,
    policy: KillSwitchDrillPolicy | None = None,
) -> KillSwitchDrillReport:
    """Exercise trip/restart/recovery/corruption semantics in an isolated drill database."""
    policy = policy or KillSwitchDrillPolicy()
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    if not component.strip() or not operator.strip():
        raise ValueError("component and operator are required")
    path = _drill_path(directory, component, timestamp)
    latch = NoTradeSafetyLatch(path)
    failures: list[str] = []

    cold_start = _execution_blocked(latch, component) and not latch.state(component).initialized
    if not cold_start:
        failures.append("cold_start_not_fail_closed")

    latch.clear_no_trade(
        component,
        operator=operator,
        reason="isolated_drill_initialize_normal",
        now=timestamp,
    )
    latch.trip_no_trade(
        component,
        reason="isolated_drill_trip",
        source_release_id="drill-release",
        actor="kill-switch-drill",
        now=timestamp + timedelta(seconds=1),
    )
    trip_blocks = _execution_blocked(latch, component)
    if not trip_blocks:
        failures.append("trip_did_not_block_execution")

    restarted = NoTradeSafetyLatch(path)
    restart_preserves_trip = (
        restarted.state(component).mode is SafetyMode.NO_TRADE
        and _execution_blocked(restarted, component)
    )
    if policy.require_restart_persistence and not restart_preserves_trip:
        failures.append("restart_lost_no_trade_state")

    integrity = restarted.verify_integrity(component)
    integrity_before = integrity.valid
    if policy.require_integrity_verification and not integrity_before:
        failures.extend(f"pre_recovery_integrity:{item}" for item in integrity.failures)

    restarted.clear_no_trade(
        component,
        operator=operator,
        reason="isolated_drill_recovery_after_review",
        now=timestamp + timedelta(seconds=2),
    )
    recovered = restarted.state(component)
    recovery_normal = recovered.execution_allowed and recovered.updated_by == operator
    if policy.require_operator_recovery and not recovery_normal:
        failures.append("operator_recovery_failed")

    after_recovery_restart = NoTradeSafetyLatch(path)
    restart_preserves_recovery = after_recovery_restart.state(component).execution_allowed
    if policy.require_restart_persistence and not restart_preserves_recovery:
        failures.append("restart_lost_recovered_state")

    with sqlite3.connect(str(path)) as db:
        row = db.execute(
            """
            SELECT event_id FROM component_safety_events
            WHERE component=? ORDER BY created_ts_utc LIMIT 1
            """,
            (component,),
        ).fetchone()
        if row is None:
            raise RuntimeError("drill safety event history unexpectedly empty")
        db.execute(
            "UPDATE component_safety_events SET reason=? WHERE event_id=?",
            ("tampered-drill-reason", str(row[0])),
        )
    corruption = NoTradeSafetyLatch(path).verify_integrity(component)
    corruption_detected = not corruption.valid and corruption.event_id_mismatches > 0
    if not corruption_detected:
        failures.append("safety_event_corruption_not_detected")

    drill_id = hashlib.sha256(
        f"kill-switch-drill-v1|{component}|{timestamp.isoformat()}|{path.name}".encode()
    ).hexdigest()
    return KillSwitchDrillReport(
        drill_id=drill_id,
        component=component,
        cold_start_fail_closed=cold_start,
        trip_blocks_execution=trip_blocks,
        restart_preserves_trip=restart_preserves_trip,
        integrity_verified_before_recovery=integrity_before,
        operator_recovery_restores_normal=recovery_normal,
        restart_preserves_recovery=restart_preserves_recovery,
        corruption_detected=corruption_detected,
        passed=not failures,
        failures=tuple(failures),
        drill_db_path=str(path),
    )
