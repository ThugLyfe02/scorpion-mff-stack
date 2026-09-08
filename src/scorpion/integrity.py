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


class IntegrityLedger:
    """Append-only SHA-256 hash chain for forensic evidence.

    The chain detects partial/accidental record mutation. For protection against an attacker
    who can rewrite the entire database, periodically anchor the returned head hash in an
    independent system; external anchoring is intentionally outside this package.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS integrity_ledger (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL UNIQUE,
                    previous_hash TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    record_hash TEXT NOT NULL UNIQUE,
                    created_ts_utc TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def append(self, record_id: str, payload: Mapping[str, Scalar]) -> IntegrityRecord:
        payload_sha256 = _payload_hash(canonical_payload(payload))
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sequence,record_hash FROM integrity_ledger ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence = int(row["sequence"]) + 1 if row else 1
            previous_hash = str(row["record_hash"]) if row else _GENESIS
            record_hash = compute_record_hash(
                sequence,
                record_id,
                previous_hash,
                payload_sha256,
            )
            db.execute(
                """
                INSERT INTO integrity_ledger
                (sequence,record_id,previous_hash,payload_sha256,record_hash,created_ts_utc)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    sequence,
                    record_id,
                    previous_hash,
                    payload_sha256,
                    record_hash,
                    datetime.now(UTC).isoformat(),
                ),
            )
            db.execute("COMMIT")
            return IntegrityRecord(
                sequence,
                record_id,
                previous_hash,
                payload_sha256,
                record_hash,
            )
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

    def head_hash(self) -> str:
        records = self.records()
        return records[-1].record_hash if records else _GENESIS
