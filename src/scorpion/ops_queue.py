from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path

from .decision_store import ensure_decision_packet_schema


class QueuePriority(IntEnum):
    P0 = 0
    P1 = 1
    P2 = 2
    P3 = 3


@dataclass(frozen=True, slots=True)
class OperatorQueueItem:
    packet_id: str
    event_id: str
    event_kind: str
    contract_key: str | None
    disposition: str
    system_mode: str
    evidence_strength: float
    age_seconds: float
    stale: bool
    priority: QueuePriority
    reason_codes: tuple[str, ...]


def _priority(disposition: str, event_kind: str, age_seconds: float) -> QueuePriority:
    if disposition == "BLOCKED_SYSTEM":
        value = QueuePriority.P0
    elif disposition == "REVIEW_REQUIRED":
        value = QueuePriority.P1
    elif disposition == "READY_FOR_OPERATOR_REVIEW":
        value = QueuePriority.P2
    else:
        value = QueuePriority.P3

    # Research-only strategy packets are intentionally non-urgent. They remain visible for
    # research/audit, but must never age into the active execution-review queue.
    if disposition == "BLOCKED_STRATEGY":
        return QueuePriority.P3

    if event_kind in {"EXIT", "TRIM", "STOP"} and value > QueuePriority.P0:
        value = QueuePriority(value - 1)
    if age_seconds >= 30.0 and value > QueuePriority.P0:
        value = QueuePriority(value - 1)
    return value


def load_operator_inbox(
    path: str | Path,
    *,
    now: datetime | None = None,
    limit: int = 50,
    stale_after_seconds: float = 15.0,
) -> tuple[OperatorQueueItem, ...]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        ensure_decision_packet_schema(db)
        rows = db.execute(
            """
            SELECT packet_id,event_id,disposition,evidence_strength,system_mode,
                   payload_json,created_ts_utc
            FROM operator_decision_packets
            WHERE resolved_ts_utc IS NULL
            ORDER BY created_ts_utc ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        db.close()

    items: list[OperatorQueueItem] = []
    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        created = datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC)
        age = max(0.0, (now - created).total_seconds())
        event_kind = str(payload.get("event_kind", "UNKNOWN"))
        disposition = str(row["disposition"])
        actionable = event_kind in {"ENTRY", "ADD", "TRIM", "EXIT", "STOP"}
        stale = (
            actionable
            and disposition != "BLOCKED_STRATEGY"
            and age >= stale_after_seconds
        )
        items.append(
            OperatorQueueItem(
                packet_id=str(row["packet_id"]),
                event_id=str(row["event_id"]),
                event_kind=event_kind,
                contract_key=(
                    str(payload["contract_key"])
                    if payload.get("contract_key") is not None
                    else None
                ),
                disposition=disposition,
                system_mode=str(row["system_mode"]),
                evidence_strength=float(row["evidence_strength"]),
                age_seconds=age,
                stale=stale,
                priority=_priority(disposition, event_kind, age),
                reason_codes=tuple(str(value) for value in payload.get("reason_codes", [])),
            )
        )
    return tuple(
        sorted(
            items,
            key=lambda item: (int(item.priority), -item.age_seconds, item.packet_id),
        )
    )
