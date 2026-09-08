from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CausalTrace:
    event_id: str
    signal: dict[str, Any]
    raw: dict[str, Any] | None
    decision_audit: dict[str, Any] | None
    decision_packet: dict[str, Any] | None
    effects: tuple[dict[str, Any], ...]
    stage_latency: dict[str, Any] | None
    integrity: dict[str, Any] | None
    approvals: tuple[dict[str, Any], ...]
    complete: bool
    missing: tuple[str, ...]


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _decode_payload(row: dict[str, Any] | None, field: str) -> None:
    if row is None or field not in row or row[field] is None:
        return
    try:
        row[field] = json.loads(str(row[field]))
    except json.JSONDecodeError:
        row[field] = {"decode_error": True, "raw": str(row[field])}


def trace_event(path: str | Path, event_id: str) -> CausalTrace:
    if not event_id.strip():
        raise ValueError("event_id is required")
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    missing: list[str] = []
    try:
        signal_row = db.execute(
            "SELECT * FROM signal_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if signal_row is None:
            raise KeyError(event_id)
        signal = dict(signal_row)
        _decode_payload(signal, "payload_json")
        message_id = str(signal["message_id"])

        raw_row = db.execute(
            """
            SELECT r.*,p.status AS processing_status,p.error AS processing_error
            FROM raw_discord_events r
            LEFT JOIN raw_processing p ON p.raw_event_id=r.raw_event_id
            WHERE r.message_id=?
            ORDER BY COALESCE(r.edited_ts_utc,r.source_ts_utc) DESC
            LIMIT 1
            """,
            (message_id,),
        ).fetchone()
        raw = _row_dict(raw_row)
        if raw is None:
            missing.append("raw_discord_event")

        audit = _row_dict(
            db.execute(
                "SELECT * FROM decision_audit WHERE event_id=?",
                (event_id,),
            ).fetchone()
        )
        if audit is None:
            missing.append("decision_audit")
        else:
            _decode_payload(audit, "matched_terms_json")
            _decode_payload(audit, "conflicts_json")

        packet: dict[str, Any] | None = None
        if _table_exists(db, "operator_decision_packets"):
            packet = _row_dict(
                db.execute(
                    "SELECT * FROM operator_decision_packets WHERE event_id=?",
                    (event_id,),
                ).fetchone()
            )
            if packet is not None:
                _decode_payload(packet, "payload_json")
        if packet is None:
            missing.append("operator_decision_packet")

        effect_rows = db.execute(
            "SELECT * FROM proposed_effects WHERE source_event_id=? ORDER BY effect_id",
            (event_id,),
        ).fetchall()
        effects = tuple(dict(row) for row in effect_rows)
        for effect in effects:
            _decode_payload(effect, "metadata_json")

        stage: dict[str, Any] | None = None
        if _table_exists(db, "transition_stage_latency"):
            stage = _row_dict(
                db.execute(
                    "SELECT * FROM transition_stage_latency WHERE event_id=?",
                    (event_id,),
                ).fetchone()
            )
        if stage is None:
            missing.append("stage_latency")

        integrity: dict[str, Any] | None = None
        if _table_exists(db, "integrity_ledger"):
            integrity = _row_dict(
                db.execute(
                    "SELECT * FROM integrity_ledger WHERE record_id=?",
                    (event_id,),
                ).fetchone()
            )
            if integrity is not None:
                _decode_payload(integrity, "payload_json")
        if integrity is None:
            missing.append("integrity_record")

        approvals: tuple[dict[str, Any], ...] = ()
        if effects:
            effect_ids = [int(effect["effect_id"]) for effect in effects]
            placeholders = ",".join("?" for _ in effect_ids)
            rows = db.execute(
                f"SELECT * FROM approvals WHERE effect_id IN ({placeholders}) ORDER BY approval_id",
                effect_ids,
            ).fetchall()
            approvals = tuple(dict(row) for row in rows)
    finally:
        db.close()

    return CausalTrace(
        event_id=event_id,
        signal=signal,
        raw=raw,
        decision_audit=audit,
        decision_packet=packet,
        effects=effects,
        stage_latency=stage,
        integrity=integrity,
        approvals=approvals,
        complete=not missing,
        missing=tuple(missing),
    )
