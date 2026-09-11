from __future__ import annotations

import hashlib
import json
import os
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
    checkpoint_matches_history: bool
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
CREATE TABLE IF NOT EXISTS component_safety_integrity_state (
    component TEXT PRIMARY KEY,
    event_count INTEGER NOT NULL,
    head_event_id TEXT NOT NULL,
    head_chain_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_component_safety_events_component_created
ON component_safety_events(component,created_ts_utc,event_id);
"""


def _normalize_reason(reason: str) -> str:
    return reason[:1000]


def _chain_hash(previous: str, event_id: str) -> str:
    return hashlib.sha256(f"{previous}|{event_id}".encode()).hexdigest()


def _sentinel_path(database_path: str | Path, component: str) -> Path:
    suffix = hashlib.sha256(component.encode()).hexdigest()[:20]
    return Path(f"{database_path}.{suffix}.NO_TRADE")


def _fsync_directory(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, os.O_RDONLY | flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_sentinel(
    database_path: str | Path,
    *,
    component: str,
    source_release_id: str,
    reason: str,
    actor: str,
    timestamp: datetime,
) -> None:
    target = _sentinel_path(database_path, component)
    payload = json.dumps(
        {
            "version": "no-trade-sentinel-v1",
            "component": component,
            "source_release_id": source_release_id,
            "reason": reason,
            "actor": actor,
            "timestamp": timestamp.astimezone(UTC).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    _fsync_directory(target.parent)


def _remove_sentinel(database_path: str | Path, component: str) -> None:
    target = _sentinel_path(database_path, component)
    if not target.exists():
        return
    target.unlink()
    _fsync_directory(target.parent)


def _read_sentinel(database_path: str | Path, component: str) -> SafetyLatchState | None:
    target = _sentinel_path(database_path, component)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("component") != component:
            raise ValueError("sentinel component mismatch")
        timestamp = datetime.fromisoformat(str(payload["timestamp"])).astimezone(UTC)
        return SafetyLatchState(
            component=component,
            mode=SafetyMode.NO_TRADE,
            source_release_id=str(payload.get("source_release_id", "")),
            reason=str(payload.get("reason", "external_no_trade_sentinel")),
            updated_ts_utc=timestamp,
            updated_by=str(payload.get("actor", "external-no-trade-sentinel")),
            initialized=True,
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return SafetyLatchState(
            component=component,
            mode=SafetyMode.NO_TRADE,
            source_release_id="",
            reason="external_no_trade_sentinel_unreadable",
            updated_ts_utc=datetime.fromtimestamp(0, tz=UTC),
            updated_by="external-no-trade-sentinel",
            initialized=True,
        )


def _rebuild_integrity_checkpoint(db: sqlite3.Connection, component: str) -> None:
    rows = db.execute(
        """
        SELECT event_id FROM component_safety_events
        WHERE component=? ORDER BY created_ts_utc,event_id
        """,
        (component,),
    ).fetchall()
    chain = ""
    head = ""
    for row in rows:
        head = str(row[0])
        chain = _chain_hash(chain, head)
    db.execute(
        """
        INSERT INTO component_safety_integrity_state
        (component,event_count,head_event_id,head_chain_hash)
        VALUES (?,?,?,?)
        ON CONFLICT(component) DO UPDATE SET
            event_count=excluded.event_count,
            head_event_id=excluded.head_event_id,
            head_chain_hash=excluded.head_chain_hash
        """,
        (component, len(rows), head, chain),
    )


class NoTradeSafetyLatch:
    """Durable fail-closed latch with an independent emergency sentinel.

    Risk-decreasing trips write the fsync'd sentinel before touching SQLite. Risk-increasing clears
    commit the audited SQLite NORMAL transition first and remove the sentinel last. A crash or DB
    outage at either boundary therefore leaves execution blocked rather than accidentally enabled.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.executescript(_SCHEMA)

    def emergency_sentinel_active(self, component: str) -> bool:
        return _sentinel_path(self.path, component).exists()

    def state(self, component: str) -> SafetyLatchState:
        if not component.strip():
            raise ValueError("component is required")
        sentinel = _read_sentinel(self.path, component)
        if sentinel is not None:
            return sentinel
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
        normalized_reason = _normalize_reason(reason)
        _write_sentinel(
            self.path,
            component=component,
            source_release_id=source_release_id,
            reason=normalized_reason,
            actor=actor,
            timestamp=timestamp,
        )
        event_id = _event_id(
            component=component,
            mode=SafetyMode.NO_TRADE,
            source_release_id=source_release_id,
            reason=normalized_reason,
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
                    normalized_reason,
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
                    normalized_reason,
                    timestamp.isoformat(),
                    actor,
                ),
            )
            _rebuild_integrity_checkpoint(db, component)
            db.commit()
        return self.state(component)

    def clear_no_trade(
        self,
        component: str,
        *,
        operator: str,
        reason: str,
        source_release_id: str | None = None,
        now: datetime | None = None,
    ) -> SafetyLatchState:
        if not operator.strip() or not reason.strip():
            raise ValueError("operator identity and clear reason are required")
        if source_release_id is not None and not source_release_id.strip():
            raise ValueError("explicit source_release_id cannot be blank")
        current = self.state(component)
        resolved_release_id = (
            current.source_release_id if source_release_id is None else source_release_id
        )
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        normalized_reason = _normalize_reason(reason)
        event_id = _event_id(
            component=component,
            mode=SafetyMode.NORMAL,
            source_release_id=resolved_release_id,
            reason=normalized_reason,
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
                    resolved_release_id,
                    normalized_reason,
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
                    resolved_release_id,
                    normalized_reason,
                    timestamp.isoformat(),
                    operator,
                ),
            )
            _rebuild_integrity_checkpoint(db, component)
            db.commit()
        _remove_sentinel(self.path, component)
        return self.state(component)

    def rebind_normal_release(
        self,
        component: str,
        *,
        expected_source_release_id: str,
        new_source_release_id: str,
        operator: str,
        reason: str,
        now: datetime | None = None,
    ) -> SafetyLatchState:
        if not new_source_release_id.strip():
            raise ValueError("new_source_release_id is required")
        current = self.state(component)
        if not current.execution_allowed:
            raise ValueError("safety latch must be verified NORMAL before release rebind")
        if current.source_release_id != expected_source_release_id:
            raise ValueError("safety latch source release changed before rebind")
        return self.clear_no_trade(
            component,
            operator=operator,
            reason=reason,
            source_release_id=new_source_release_id,
            now=now,
        )

    def verify_integrity(self, component: str) -> SafetyLedgerIntegrityReport:
        effective = self.state(component)
        sentinel = _read_sentinel(self.path, component)
        failures: list[str] = []
        if not effective.initialized:
            failures.append("safety_component_uninitialized")
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            state_row = db.execute(
                "SELECT * FROM component_safety_state WHERE component=?",
                (component,),
            ).fetchone()
            rows = db.execute(
                """
                SELECT event_id,component,mode,source_release_id,reason,created_ts_utc,actor
                FROM component_safety_events
                WHERE component=? ORDER BY created_ts_utc,event_id
                """,
                (component,),
            ).fetchall()
            checkpoint = db.execute(
                """
                SELECT event_count,head_event_id,head_chain_hash
                FROM component_safety_integrity_state WHERE component=?
                """,
                (component,),
            ).fetchone()

        mismatches = 0
        chain = ""
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
            event_id = str(row["event_id"])
            mismatches += int(expected != event_id)
            chain = _chain_hash(chain, event_id)
        if mismatches:
            failures.append(f"safety_event_id_mismatches:{mismatches}")

        state_matches = False
        materialized = _row_to_state(state_row) if state_row is not None else None
        if rows and materialized is not None:
            latest = rows[-1]
            state_matches = (
                materialized.mode.value == str(latest["mode"])
                and materialized.source_release_id == str(latest["source_release_id"])
                and materialized.reason == str(latest["reason"])
                and materialized.updated_ts_utc
                == datetime.fromisoformat(str(latest["created_ts_utc"])).astimezone(UTC)
                and materialized.updated_by == str(latest["actor"])
            )
        if state_row is not None and not rows:
            failures.append("initialized_safety_state_has_no_event_history")
        elif rows and not state_matches:
            failures.append("safety_state_does_not_match_latest_event")

        checkpoint_matches = False
        if checkpoint is not None:
            expected_head = str(rows[-1]["event_id"]) if rows else ""
            checkpoint_matches = (
                int(checkpoint["event_count"]) == len(rows)
                and str(checkpoint["head_event_id"]) == expected_head
                and str(checkpoint["head_chain_hash"]) == chain
            )
        if state_row is not None and not checkpoint_matches:
            failures.append("safety_integrity_checkpoint_mismatch")

        if rows:
            latest = rows[-1]
            latest_mode = SafetyMode(str(latest["mode"]))
            if latest_mode is SafetyMode.NO_TRADE and sentinel is None:
                failures.append("no_trade_emergency_sentinel_missing")
            if latest_mode is SafetyMode.NORMAL and sentinel is not None:
                failures.append("unexpected_no_trade_emergency_sentinel")
            if latest_mode is SafetyMode.NO_TRADE and sentinel is not None:
                sentinel_matches = (
                    sentinel.source_release_id == str(latest["source_release_id"])
                    and sentinel.reason == str(latest["reason"])
                    and sentinel.updated_ts_utc
                    == datetime.fromisoformat(str(latest["created_ts_utc"])).astimezone(UTC)
                    and sentinel.updated_by == str(latest["actor"])
                )
                if not sentinel_matches:
                    failures.append("no_trade_emergency_sentinel_payload_mismatch")

        return SafetyLedgerIntegrityReport(
            component=component,
            initialized=materialized is not None,
            events=len(rows),
            event_id_mismatches=mismatches,
            state_matches_latest_event=state_matches,
            checkpoint_matches_history=checkpoint_matches,
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
    return hashlib.sha256(material.encode()).hexdigest()


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
