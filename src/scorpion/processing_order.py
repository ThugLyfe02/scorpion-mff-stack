from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_processing_order (
    process_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(event_id) REFERENCES signal_events(event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_processing_order_event
ON event_processing_order(event_id);
"""


@dataclass(frozen=True, slots=True)
class ProcessingOrderReport:
    signal_count: int
    ordered_count: int
    missing_event_ids: tuple[str, ...]
    contiguous: bool
    order_fingerprint: str

    @property
    def complete(self) -> bool:
        return self.signal_count == self.ordered_count and not self.missing_event_ids and self.contiguous


def ensure_processing_order_schema(db: sqlite3.Connection) -> None:
    """Create and migrate the durable live-state processing order.

    Existing signals predate this table. They are backfilled exactly once using the previous
    canonical replay order so an upgrade preserves historical behavior. New normalized events are
    appended inside their atomic transition transaction, making process_seq the durable live-state
    mutation order from that point onward.
    """
    db.executescript(_SCHEMA)
    missing = db.execute(
        """
        SELECT s.event_id
        FROM signal_events s
        LEFT JOIN event_processing_order o ON o.event_id=s.event_id
        WHERE o.event_id IS NULL
        ORDER BY s.source_ts_utc,s.received_ts_utc,s.event_id
        """
    ).fetchall()
    for row in missing:
        db.execute(
            "INSERT OR IGNORE INTO event_processing_order(event_id) VALUES (?)",
            (str(row[0]),),
        )


def bind_event_processing_order(db: sqlite3.Connection, event_id: str) -> int:
    if not event_id.strip():
        raise ValueError("event_id is required")
    db.execute(
        "INSERT OR IGNORE INTO event_processing_order(event_id) VALUES (?)",
        (event_id,),
    )
    row = db.execute(
        "SELECT process_seq FROM event_processing_order WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("event processing order was not persisted")
    return int(row[0])


def inspect_processing_order(path: str | Path) -> ProcessingOrderReport:
    with sqlite3.connect(str(path)) as db:
        ensure_processing_order_schema(db)
        signal_count = int(db.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0])
        rows = db.execute(
            "SELECT process_seq,event_id FROM event_processing_order ORDER BY process_seq"
        ).fetchall()
        missing = tuple(
            str(row[0])
            for row in db.execute(
                """
                SELECT s.event_id FROM signal_events s
                LEFT JOIN event_processing_order o ON o.event_id=s.event_id
                WHERE o.event_id IS NULL ORDER BY s.event_id
                """
            ).fetchall()
        )
    sequences = [int(row[0]) for row in rows]
    contiguous = sequences == list(range(1, len(sequences) + 1)) if sequences else True
    material = "\n".join(f"{int(seq)}:{event_id}" for seq, event_id in rows)
    return ProcessingOrderReport(
        signal_count=signal_count,
        ordered_count=len(rows),
        missing_event_ids=missing,
        contiguous=contiguous,
        order_fingerprint=hashlib.sha256(material.encode("utf-8")).hexdigest(),
    )
