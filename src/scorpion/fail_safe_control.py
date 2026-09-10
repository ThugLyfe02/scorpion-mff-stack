from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .release_guard import ReleaseRegistry, RollbackPlan


class SafetyMode(StrEnum):
    NORMAL = "NORMAL"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True, slots=True)
class SafetyLatchState:
    component: str
    mode: SafetyMode
    source_release_id: str
    reason: str
    updated_ts_utc: datetime
    updated_by: str
    initialized: bool = True

    @property
    def execution_allowed(self) -> bool:
        return self.initialized and self.mode is SafetyMode.NORMAL


@dataclass(frozen=True, slots=True)
class SafetyLedgerIntegrityReport:
    component: str
    initialized: bool
    events: int
    event_id_mismatches: int
    state_matches_latest_event: bool
    valid: bool
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProtectedRollback:
    rollback_plan: RollbackPlan | None
    safety_state: SafetyLatchState
    replacement_requires_operator: bool


_SCHEMA = """
CREATE TABLE IF NOT EXISTS component_safety_state (
    component TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    source_release_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_ts_utc TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS component_safety_events (
    event_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    mode TEXT NOT NULL,
    source_release_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    actor TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_component_safety_events_component_created
ON component_safety_events(component,created_ts_utc,event_id);
"""


class NoTradeSafetyLatch:
    """Durable fail-closed latch. Automation may trip it; only an operator may clear it."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.executescript(_SCHEMA)

    def state(self, component: str) -> SafetyLatchState:
        if not component.strip():
            raise ValueError("component is required")
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM component_safety_state WHERE component=?",
                (component,),
            ).fetchone()
        if row is None:
            return SafetyLatchState(
                component=component,
                mode=SafetyMode.NO_TRADE,
                source_release_id="",
                reason="uninitialized_component_fail_closed",
                updated_ts_utc=datetime.fromtimestamp(0, tz=UTC),
                updated_by="system-default",
                initialized=False,
            )
        return _row_to_state(row)

    def trip_no_trade(
        self,
        component: str,
        *,
        reason: str,
        source_release_id: str = "",
        actor: str = "automatic-safety-monitor",
        now: datetime | None = None,
    ) -> SafetyLatchState:
        if not component.strip() or not reason.strip() or not actor.strip():
            raise ValueError("component, reason and actor are required")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        event_id = _event_id(
            component=component,
            mode=SafetyMode.NO_TRADE,
            source_release_id=source_release_id,
            reason=reason,
            timestamp=timestamp,
            actor=actor,
        )
        with sqlite3.connect(self.path) as db:
            db.executescript(_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT OR IGNORE INTO component_safety_events
                (event_id,component,mode,source_release_id,reason,created_ts_utc,actor)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    component,
                    SafetyMode.NO_TRADE.value,
                    source_release_id,
                    reason[:1000],
                    timestamp.isoformat(),
                    actor,
                ),
            )
            db.execute(
                """
                INSERT INTO component_safety_state
                (component,mode,source_release_id,reason,updated_ts_utc,updated_by)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(component) DO UPDATE SET
                    mode=excluded.mode,
                    source_release_id=excluded.source_release_id,
                    reason=excluded.reason,
                    updated_ts_utc=excluded.updated_ts_utc,
                    updated_by=excluded.updated_by
                """,
                (
                    component,
                    SafetyMode.NO_TRADE.value,
                    source_release_id,
                    reason[:1000],
                    timestamp.isoformat(),
                    actor,
                ),
            )
            db.commit()
        return self.state(component)

    def clear_no_trade(
        self,
        component: str,
        *,
        operator: str,
        reason: str,
        now: datetime | None = None,
    ) -> SafetyLatchState:
        if not operator.strip() or not reason.strip():
            raise ValueError("operator identity and clear reason are required")
        current = self.state(component)
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        event_id = _event_id(
            component=component,
            mode=SafetyMode.NORMAL,
            source_release_id=current.source_release_id,
            reason=reason,
            timestamp=timestamp,
            actor=operator,
        )
        with sqlite3.connect(self.path) as db:
            db.executescript(_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO component_safety_events
                (event_id,component,mode,source_release_id,reason,created_ts_utc,actor)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    component,
                    SafetyMode.NORMAL.value,
                    current.source_release_id,
                    reason[:1000],
                    timestamp.isoformat(),
                    operator,
                ),
            )
            db.execute(
                """
                INSERT INTO component_safety_state
                (component,mode,source_release_id,reason,updated_ts_utc,updated_by)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(component) DO UPDATE SET
                    mode=excluded.mode,
                    source_release_id=excluded.source_release_id,
                    reason=excluded.reason,
                    updated_ts_utc=excluded.updated_ts_utc,
                    updated_by=excluded.updated_by
                """,
                (
                    component,
                    SafetyMode.NORMAL.value,
                    current.source_release_id,
                    reason[:1000],
                    timestamp.isoformat(),
                    operator,
                ),
            )
            db.commit()
        return self.state(component)

    def verify_integrity(self, component: str) -> SafetyLedgerIntegrityReport:
        current = self.state(component)
        failures: list[str] = []
        if not current.initialized:
            failures.append("safety_component_uninitialized")
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT event_id,component,mode,source_release_id,reason,created_ts_utc,actor
                FROM component_safety_events
                WHERE component=? ORDER BY created_ts_utc,event_id
                """,
                (component,),
            ).fetchall()
        mismatches = 0
        for row in rows:
            timestamp = datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC)
            expected = _event_id(
                component=str(row["component"]),
                mode=SafetyMode(str(row["mode"])),
                source_release_id=str(row["source_release_id"]),
                reason=str(row["reason"]),
                timestamp=timestamp,
                actor=str(row["actor"]),
            )
            mismatches += int(expected != str(row["event_id"]))
        if mismatches:
            failures.append(f"safety_event_id_mismatches:{mismatches}")

        state_matches = False
        if rows and current.initialized:
            latest = rows[-1]
            state_matches = (
                current.mode.value == str(latest["mode"])
                and current.source_release_id == str(latest["source_release_id"])
                and current.reason == str(latest["reason"])
                and current.updated_ts_utc
                == datetime.fromisoformat(str(latest["created_ts_utc"])).astimezone(UTC)
                and current.updated_by == str(latest["actor"])
            )
        if current.initialized and not rows:
            failures.append("initialized_safety_state_has_no_event_history")
        elif rows and not state_matches:
            failures.append("safety_state_does_not_match_latest_event")
        return SafetyLedgerIntegrityReport(
            component=component,
            initialized=current.initialized,
            events=len(rows),
            event_id_mismatches=mismatches,
            state_matches_latest_event=state_matches,
            valid=not failures,
            failures=tuple(failures),
        )

    def assert_execution_allowed(self, component: str) -> None:
        current = self.state(component)
        if not current.execution_allowed:
            raise RuntimeError(
                f"component {component} is fail-closed in NO_TRADE mode: {current.reason}"
            )


def quarantine_and_trip_no_trade(
    registry: ReleaseRegistry,
    latch: NoTradeSafetyLatch,
    *,
    component: str,
    reason: str,
    now: datetime | None = None,
) -> ProtectedRollback:
    """Automatically remove a degrading release from authority and fail closed.

    The prior release is returned only as a rollback candidate. This function never activates
    that candidate; replacing the quarantined release remains an explicit operator action.
    """
    rollback = registry.quarantine_active(component, reason=reason, now=now)
    source_release_id = rollback.quarantined_release_id if rollback is not None else ""
    safety = latch.trip_no_trade(
        component,
        reason=reason,
        source_release_id=source_release_id,
        now=now,
    )
    return ProtectedRollback(
        rollback_plan=rollback,
        safety_state=safety,
        replacement_requires_operator=True,
    )


def _event_id(
    *,
    component: str,
    mode: SafetyMode,
    source_release_id: str,
    reason: str,
    timestamp: datetime,
    actor: str,
) -> str:
    material = "|".join(
        (
            "component-safety-event-v1",
            component,
            mode.value,
            source_release_id,
            reason,
            timestamp.isoformat(),
            actor,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_to_state(row: sqlite3.Row) -> SafetyLatchState:
    return SafetyLatchState(
        component=str(row["component"]),
        mode=SafetyMode(str(row["mode"])),
        source_release_id=str(row["source_release_id"]),
        reason=str(row["reason"]),
        updated_ts_utc=datetime.fromisoformat(str(row["updated_ts_utc"])).astimezone(UTC),
        updated_by=str(row["updated_by"]),
        initialized=True,
    )
