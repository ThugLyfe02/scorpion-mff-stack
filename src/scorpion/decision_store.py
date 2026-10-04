from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .decision_packet import OperatorDecisionPacket

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS operator_decision_packets (
    packet_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    disposition TEXT NOT NULL,
    evidence_strength REAL NOT NULL,
    system_mode TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    resolved_ts_utc TEXT,
    resolved_by TEXT,
    resolution TEXT,
    resolution_note TEXT NOT NULL DEFAULT ''
)
"""
_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_operator_packet_disposition
ON operator_decision_packets(disposition,created_ts_utc)
"""


def ensure_decision_packet_schema(db: sqlite3.Connection) -> None:
    db.execute(_TABLE_SQL)
    db.execute(_INDEX_SQL)


def _payload(packet: OperatorDecisionPacket) -> str:
    payload = asdict(packet)
    payload["event_kind"] = packet.event_kind.value
    payload["disposition"] = packet.disposition.value
    payload["system_mode"] = packet.system_mode.value
    payload["created_ts_utc"] = packet.created_ts_utc.isoformat()
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def append_decision_packet(
    db: sqlite3.Connection,
    packet: OperatorDecisionPacket,
) -> bool:
    ensure_decision_packet_schema(db)
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO operator_decision_packets
        (packet_id,event_id,disposition,evidence_strength,system_mode,payload_json,created_ts_utc)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            packet.packet_id,
            packet.event_id,
            packet.disposition.value,
            packet.evidence_strength,
            packet.system_mode.value,
            _payload(packet),
            packet.created_ts_utc.isoformat(),
        ),
    )
    return cursor.rowcount == 1


def resolve_decision_packet(
    path: str | Path,
    packet_id: str,
    *,
    resolved_by: str,
    resolution: str,
    note: str = "",
) -> None:
    if not resolved_by.strip():
        raise ValueError("resolved_by is required")
    if not resolution.strip():
        raise ValueError("resolution is required")
    db = sqlite3.connect(str(path), isolation_level=None)
    try:
        ensure_decision_packet_schema(db)
        cursor = db.execute(
            """
            UPDATE operator_decision_packets
            SET resolved_ts_utc=?,resolved_by=?,resolution=?,resolution_note=?
            WHERE packet_id=? AND resolved_ts_utc IS NULL
            """,
            (datetime.now(UTC).isoformat(), resolved_by, resolution, note[:1000], packet_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("packet missing or already resolved")
    finally:
        db.close()


def unresolved_packet_count(path: str | Path) -> int:
    db = sqlite3.connect(str(path))
    try:
        ensure_decision_packet_schema(db)
        row = db.execute(
            "SELECT COUNT(*) FROM operator_decision_packets WHERE resolved_ts_utc IS NULL"
        ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        db.close()
