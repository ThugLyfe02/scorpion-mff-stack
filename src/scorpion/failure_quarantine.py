from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class FailureDisposition(StrEnum):
    RETRY = "RETRY"
    QUARANTINED = "QUARANTINED"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_failure_state (
    raw_event_id TEXT PRIMARY KEY,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    last_error TEXT NOT NULL,
    updated_ts_utc TEXT NOT NULL,
    requeued_by TEXT NOT NULL DEFAULT '',
    requeue_note TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(raw_event_id) REFERENCES raw_discord_events(raw_event_id)
);
CREATE TABLE IF NOT EXISTS raw_failure_events (
    failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_event_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    error TEXT NOT NULL,
    occurred_ts_utc TEXT NOT NULL,
    FOREIGN KEY(raw_event_id) REFERENCES raw_discord_events(raw_event_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_failure_state_state
ON raw_failure_state(state,updated_ts_utc);
"""


@dataclass(frozen=True, slots=True)
class ProcessingFailure:
    raw_event_id: str
    attempt_count: int
    disposition: FailureDisposition
    error: str


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(_SCHEMA)


def record_processing_failure(
    path: str | Path,
    raw_event_id: str,
    error: str,
    *,
    maximum_attempts: int = 3,
) -> ProcessingFailure:
    if maximum_attempts <= 0:
        raise ValueError("maximum_attempts must be positive")
    now = datetime.now(UTC).isoformat()
    clean_error = error[:500]
    with sqlite3.connect(str(path), timeout=5.0, isolation_level=None) as db:
        db.execute("PRAGMA foreign_keys=ON")
        _ensure_schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT attempt_count FROM raw_failure_state WHERE raw_event_id=?",
                (raw_event_id,),
            ).fetchone()
            attempts = (int(row[0]) if row is not None else 0) + 1
            disposition = (
                FailureDisposition.QUARANTINED
                if attempts >= maximum_attempts
                else FailureDisposition.RETRY
            )
            db.execute(
                """
                INSERT INTO raw_failure_state
                (raw_event_id,attempt_count,state,last_error,updated_ts_utc)
                VALUES (?,?,?,?,?)
                ON CONFLICT(raw_event_id) DO UPDATE SET
                    attempt_count=excluded.attempt_count,
                    state=excluded.state,
                    last_error=excluded.last_error,
                    updated_ts_utc=excluded.updated_ts_utc
                """,
                (raw_event_id, attempts, disposition.value, clean_error, now),
            )
            db.execute(
                """
                INSERT INTO raw_failure_events
                (raw_event_id,attempt_number,error,occurred_ts_utc)
                VALUES (?,?,?,?)
                """,
                (raw_event_id, attempts, clean_error, now),
            )
            raw_status = (
                "QUARANTINED"
                if disposition is FailureDisposition.QUARANTINED
                else "PENDING"
            )
            db.execute(
                """
                UPDATE raw_processing
                SET status=?,updated_ts_utc=?,error=?
                WHERE raw_event_id=?
                """,
                (raw_status, now, clean_error, raw_event_id),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    return ProcessingFailure(raw_event_id, attempts, disposition, clean_error)


def requeue_quarantined(
    path: str | Path,
    raw_event_id: str,
    *,
    operator: str,
    note: str,
) -> None:
    if not operator.strip():
        raise ValueError("operator is required")
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(str(path), timeout=5.0, isolation_level=None) as db:
        db.execute("PRAGMA foreign_keys=ON")
        _ensure_schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT state FROM raw_failure_state WHERE raw_event_id=?",
                (raw_event_id,),
            ).fetchone()
            if row is None or str(row[0]) != FailureDisposition.QUARANTINED.value:
                raise ValueError("raw revision is not quarantined")
            db.execute(
                """
                UPDATE raw_failure_state
                SET attempt_count=0,state='RETRY',updated_ts_utc=?,requeued_by=?,requeue_note=?
                WHERE raw_event_id=?
                """,
                (now, operator, note[:1000], raw_event_id),
            )
            db.execute(
                """
                UPDATE raw_processing
                SET status='PENDING',updated_ts_utc=?,error=''
                WHERE raw_event_id=?
                """,
                (now, raw_event_id),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise


def quarantined_count(path: str | Path) -> int:
    with sqlite3.connect(str(path)) as db:
        _ensure_schema(db)
        row = db.execute(
            "SELECT COUNT(*) FROM raw_failure_state WHERE state='QUARANTINED'"
        ).fetchone()
    return int(row[0]) if row is not None else 0
