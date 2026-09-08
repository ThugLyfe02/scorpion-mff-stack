from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

Scalar = str | int | float | bool | None
_GENESIS = "0" * 64
_INTEGRITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS integrity_ledger (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL UNIQUE,
    previous_hash TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    record_hash TEXT NOT NULL UNIQUE,
    created_ts_utc TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class IntegrityRecord:
    sequence: int
    record_id: str
    previous_hash: str
    payload_sha256: str
    record_hash: str


@dataclass(frozen=True, slots=True)
class IntegrityVerification:
    ok: bool
    checked: int
    failure_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class DatabaseEvidenceVerification:
    ok: bool
    checked: int
    legacy_uncovered_signals: int
    failures: tuple[str, ...]


def canonical_payload(payload: Mapping[str, Scalar]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _payload_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def compute_record_hash(
    sequence: int,
    record_id: str,
    previous_hash: str,
    payload_sha256: str,
) -> str:
    material = f"{sequence}|{record_id}|{previous_hash}|{payload_sha256}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_chain(records: Sequence[tuple[str, Mapping[str, Scalar]]]) -> tuple[IntegrityRecord, ...]:
    chain: list[IntegrityRecord] = []
    previous = _GENESIS
    for sequence, (record_id, payload) in enumerate(records, start=1):
        payload_sha256 = _payload_hash(canonical_payload(payload))
        record_hash = compute_record_hash(sequence, record_id, previous, payload_sha256)
        chain.append(
            IntegrityRecord(sequence, record_id, previous, payload_sha256, record_hash)
        )
        previous = record_hash
    return tuple(chain)


def verify_chain(records: Sequence[IntegrityRecord]) -> IntegrityVerification:
    previous = _GENESIS
    for expected_sequence, record in enumerate(records, start=1):
        expected_hash = compute_record_hash(
            expected_sequence,
            record.record_id,
            previous,
            record.payload_sha256,
        )
        if (
            record.sequence != expected_sequence
            or record.previous_hash != previous
            or record.record_hash != expected_hash
        ):
            return IntegrityVerification(False, expected_sequence - 1, expected_sequence)
        previous = record.record_hash
    return IntegrityVerification(True, len(records))


def ensure_integrity_schema(db: sqlite3.Connection) -> None:
    db.execute(_INTEGRITY_SCHEMA)
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(integrity_ledger)")}
    if "payload_json" not in columns:
        db.execute(
            "ALTER TABLE integrity_ledger "
            "ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'"
        )


def append_integrity_record(
    db: sqlite3.Connection,
    record_id: str,
    payload: Mapping[str, Scalar],
    *,
    created_ts_utc: str | None = None,
) -> IntegrityRecord:
    ensure_integrity_schema(db)
    payload_json = canonical_payload(payload)
    payload_sha256 = _payload_hash(payload_json)
    row = db.execute(
        "SELECT sequence,record_hash FROM integrity_ledger ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(row["sequence"]) + 1 if row else 1
    previous_hash = str(row["record_hash"]) if row else _GENESIS
    record_hash = compute_record_hash(sequence, record_id, previous_hash, payload_sha256)
    db.execute(
        """
        INSERT INTO integrity_ledger
        (sequence,record_id,previous_hash,payload_sha256,payload_json,record_hash,created_ts_utc)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            sequence,
            record_id,
            previous_hash,
            payload_sha256,
            payload_json,
            record_hash,
            created_ts_utc or datetime.now(UTC).isoformat(),
        ),
    )
    return IntegrityRecord(sequence, record_id, previous_hash, payload_sha256, record_hash)


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _extended_expected_payload(
    db: sqlite3.Connection,
    *,
    event_id: str,
    payload: dict[str, object],
    base: dict[str, Scalar],
    effects: Sequence[sqlite3.Row],
    failures: list[str],
) -> dict[str, Scalar] | None:
    if "decision_packet_id" not in payload:
        return base
    if not _table_exists(db, "operator_decision_packets"):
        failures.append(f"missing_decision_packet_table:{event_id}")
        return None
    packet = db.execute(
        """
        SELECT packet_id,disposition,system_mode
        FROM operator_decision_packets WHERE event_id=?
        """,
        (event_id,),
    ).fetchone()
    if packet is None:
        failures.append(f"missing_decision_packet:{event_id}")
        return None
    statuses = {str(effect["status"]) for effect in effects}
    effect_status = next(iter(statuses)) if len(statuses) == 1 else ""
    if len(statuses) > 1:
        failures.append(f"mixed_effect_status:{event_id}")
    return {
        **base,
        "effect_status": effect_status,
        "decision_packet_id": str(packet["packet_id"]),
        "decision_disposition": str(packet["disposition"]),
        "operational_mode": str(packet["system_mode"]),
    }


def verify_database_evidence(path: str | Path) -> DatabaseEvidenceVerification:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    failures: list[str] = []
    try:
        ensure_integrity_schema(db)
        rows = db.execute(
            "SELECT sequence,record_id,previous_hash,payload_sha256,payload_json,record_hash "
            "FROM integrity_ledger ORDER BY sequence"
        ).fetchall()
        records = tuple(
            IntegrityRecord(
                int(row["sequence"]),
                str(row["record_id"]),
                str(row["previous_hash"]),
                str(row["payload_sha256"]),
                str(row["record_hash"]),
            )
            for row in rows
        )
        chain = verify_chain(records)
        if not chain.ok:
            failures.append(f"chain_failure:{chain.failure_sequence}")

        for row in rows:
            event_id = str(row["record_id"])
            payload_json = str(row["payload_json"])
            if _payload_hash(payload_json) != str(row["payload_sha256"]):
                failures.append(f"payload_hash_mismatch:{event_id}")
                continue
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                failures.append(f"payload_json_invalid:{event_id}")
                continue
            if not isinstance(payload, dict):
                failures.append(f"payload_not_object:{event_id}")
                continue

            signal = db.execute(
                "SELECT message_id,kind,contract_key FROM signal_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            audit = db.execute(
                "SELECT parser_rule,parser_confidence,association_method "
                "FROM decision_audit WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if signal is None or audit is None:
                failures.append(f"missing_source_evidence:{event_id}")
                continue

            effects = db.execute(
                """
                SELECT kind,status FROM proposed_effects
                WHERE source_event_id=? ORDER BY effect_id
                """,
                (event_id,),
            ).fetchall()
            raw_revision_id = str(payload.get("raw_revision_id", ""))
            raw = db.execute(
                """
                SELECT r.message_id,p.status
                FROM raw_discord_events r
                JOIN raw_processing p ON p.raw_event_id=r.raw_event_id
                WHERE r.raw_event_id=?
                """,
                (raw_revision_id,),
            ).fetchone()
            if raw is None:
                failures.append(f"missing_raw_revision:{event_id}")
                continue
            if raw["status"] != "DONE" or raw["message_id"] != signal["message_id"]:
                failures.append(f"raw_link_mismatch:{event_id}")

            base_payload: dict[str, Scalar] = {
                "raw_revision_id": raw_revision_id,
                "message_id": str(signal["message_id"]),
                "kind": str(signal["kind"]),
                "contract_key": str(signal["contract_key"] or ""),
                "parser_rule": str(audit["parser_rule"]),
                "parser_confidence": float(audit["parser_confidence"]),
                "association_method": str(audit["association_method"]),
                "effect_count": len(effects),
                "effect_kinds": ",".join(str(effect["kind"]) for effect in effects),
            }
            expected_payload = _extended_expected_payload(
                db,
                event_id=event_id,
                payload=payload,
                base=base_payload,
                effects=effects,
                failures=failures,
            )
            if expected_payload is None:
                continue
            expected_hash = _payload_hash(canonical_payload(expected_payload))
            if expected_hash != str(row["payload_sha256"]):
                failures.append(f"source_evidence_mismatch:{event_id}")

        uncovered = int(
            db.execute(
                """
                SELECT COUNT(*)
                FROM signal_events s
                LEFT JOIN integrity_ledger i ON i.record_id=s.event_id
                WHERE i.record_id IS NULL
                """
            ).fetchone()[0]
        )
    finally:
        db.close()
    return DatabaseEvidenceVerification(
        ok=not failures,
        checked=len(rows),
        legacy_uncovered_signals=uncovered,
        failures=tuple(failures),
    )


class IntegrityLedger:
    """Append-only SHA-256 hash chain for forensic evidence.

    The chain detects partial/accidental record mutation. For protection against an attacker
    who can rewrite the entire database, periodically anchor the returned head hash in an
    independent system; external anchoring is intentionally outside this package.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        db = self._connect()
        try:
            ensure_integrity_schema(db)
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def append(self, record_id: str, payload: Mapping[str, Scalar]) -> IntegrityRecord:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            record = append_integrity_record(db, record_id, payload)
            db.execute("COMMIT")
            return record
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def records(self) -> tuple[IntegrityRecord, ...]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT sequence,record_id,previous_hash,payload_sha256,record_hash "
                "FROM integrity_ledger ORDER BY sequence"
            ).fetchall()
        finally:
            db.close()
        return tuple(
            IntegrityRecord(
                int(row["sequence"]),
                str(row["record_id"]),
                str(row["previous_hash"]),
                str(row["payload_sha256"]),
                str(row["record_hash"]),
            )
            for row in rows
        )

    def verify(self) -> IntegrityVerification:
        return verify_chain(self.records())

    def verify_database(self) -> DatabaseEvidenceVerification:
        return verify_database_evidence(self.path)

    def head_hash(self) -> str:
        records = self.records()
        return records[-1].record_hash if records else _GENESIS
