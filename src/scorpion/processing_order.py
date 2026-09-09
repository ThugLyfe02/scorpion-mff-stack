from __future__ import annotations

import datetime
import decimal
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .domain import EventKind, RawDiscordMessage, SignalEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_receipt_order (
    receipt_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_event_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(raw_event_id) REFERENCES raw_discord_events(raw_event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_receipt_order_event
ON raw_receipt_order(raw_event_id);

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
    raw_count: int
    receipt_ordered_count: int
    signal_count: int
    process_ordered_count: int
    missing_raw_ids: tuple[str, ...]
    missing_event_ids: tuple[str, ...]
    receipt_contiguous: bool
    process_contiguous: bool
    receipt_fingerprint: str
    process_fingerprint: str

    @property
    def complete(self) -> bool:
        return (
            self.raw_count == self.receipt_ordered_count
            and self.signal_count == self.process_ordered_count
            and not self.missing_raw_ids
            and not self.missing_event_ids
            and self.receipt_contiguous
            and self.process_contiguous
        )


def ensure_processing_order_schema(db: sqlite3.Connection) -> None:
    """Create/migrate durable arrival and live-state mutation orders.

    Existing records predate these tables. Missing raw rows are backfilled by persisted receive
    time; missing normalized rows are backfilled by the previous canonical replay order. New raw
    arrivals and normalized commits should call the bind helpers so order becomes explicit rather
    than inferred from clocks.
    """
    db.executescript(_SCHEMA)
    missing_raw = db.execute(
        """
        SELECT r.raw_event_id
        FROM raw_discord_events r
        LEFT JOIN raw_receipt_order o ON o.raw_event_id=r.raw_event_id
        WHERE o.raw_event_id IS NULL
        ORDER BY r.received_ts_utc,r.raw_event_id
        """
    ).fetchall()
    for row in missing_raw:
        db.execute(
            "INSERT OR IGNORE INTO raw_receipt_order(raw_event_id) VALUES (?)",
            (str(row[0]),),
        )

    missing_events = db.execute(
        """
        SELECT s.event_id
        FROM signal_events s
        LEFT JOIN event_processing_order o ON o.event_id=s.event_id
        WHERE o.event_id IS NULL
        ORDER BY s.source_ts_utc,s.received_ts_utc,s.event_id
        """
    ).fetchall()
    for row in missing_events:
        db.execute(
            "INSERT OR IGNORE INTO event_processing_order(event_id) VALUES (?)",
            (str(row[0]),),
        )


def bind_raw_receipt_order(db: sqlite3.Connection, raw_event_id: str) -> int:
    if not raw_event_id.strip():
        raise ValueError("raw_event_id is required")
    db.execute(
        "INSERT OR IGNORE INTO raw_receipt_order(raw_event_id) VALUES (?)",
        (raw_event_id,),
    )
    row = db.execute(
        "SELECT receipt_seq FROM raw_receipt_order WHERE raw_event_id=?",
        (raw_event_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("raw receipt order was not persisted")
    return int(row[0])


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


def register_raw_receipt(path: str | Path, raw_event_id: str) -> int:
    with sqlite3.connect(str(path), timeout=5.0, isolation_level=None) as db:
        db.execute("PRAGMA foreign_keys=ON")
        ensure_processing_order_schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            sequence = bind_raw_receipt_order(db, raw_event_id)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    return sequence


def _decode_signal(payload_json: str) -> SignalEvent:
    payload = json.loads(payload_json)
    payload["kind"] = EventKind(payload["kind"])
    payload["source_ts_utc"] = datetime.datetime.fromisoformat(
        payload["source_ts_utc"]
    ).astimezone(datetime.UTC)
    payload["received_ts_utc"] = datetime.datetime.fromisoformat(
        payload["received_ts_utc"]
    ).astimezone(datetime.UTC)
    if payload.get("expiry"):
        payload["expiry"] = datetime.date.fromisoformat(payload["expiry"])
    for key in ("strike", "referenced_price", "referenced_pct"):
        if payload.get(key) is not None:
            payload[key] = decimal.Decimal(payload[key])
    return SignalEvent(**payload)


def load_signals_in_processing_order(path: str | Path) -> list[SignalEvent]:
    with sqlite3.connect(str(path)) as db:
        ensure_processing_order_schema(db)
        rows = db.execute(
            """
            SELECT s.payload_json
            FROM event_processing_order o
            JOIN signal_events s ON s.event_id=o.event_id
            ORDER BY o.process_seq
            """
        ).fetchall()
    return [_decode_signal(str(row[0])) for row in rows]


def load_pending_raw_in_receipt_order(path: str | Path) -> list[RawDiscordMessage]:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        ensure_processing_order_schema(db)
        rows = db.execute(
            """
            SELECT r.*
            FROM raw_receipt_order o
            JOIN raw_discord_events r ON r.raw_event_id=o.raw_event_id
            JOIN raw_processing p ON p.raw_event_id=r.raw_event_id
            WHERE p.status='PENDING'
            ORDER BY o.receipt_seq
            """
        ).fetchall()
    messages: list[RawDiscordMessage] = []
    for row in rows:
        messages.append(
            RawDiscordMessage(
                message_id=str(row["message_id"]),
                guild_id=str(row["guild_id"]),
                channel_id=str(row["channel_id"]),
                author_id=str(row["author_id"]),
                content=str(row["content"]),
                source_ts_utc=datetime.datetime.fromisoformat(str(row["source_ts_utc"])),
                received_ts_utc=datetime.datetime.fromisoformat(str(row["received_ts_utc"])),
                edited_ts_utc=(
                    datetime.datetime.fromisoformat(str(row["edited_ts_utc"]))
                    if row["edited_ts_utc"]
                    else None
                ),
                referenced_message_id=(
                    str(row["referenced_message_id"])
                    if row["referenced_message_id"] is not None
                    else None
                ),
            )
        )
    return messages


def _contiguous(values: list[int]) -> bool:
    return values == list(range(1, len(values) + 1)) if values else True


def inspect_processing_order(path: str | Path) -> ProcessingOrderReport:
    with sqlite3.connect(str(path)) as db:
        ensure_processing_order_schema(db)
        raw_count = int(db.execute("SELECT COUNT(*) FROM raw_discord_events").fetchone()[0])
        signal_count = int(db.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0])
        raw_rows = db.execute(
            "SELECT receipt_seq,raw_event_id FROM raw_receipt_order ORDER BY receipt_seq"
        ).fetchall()
        event_rows = db.execute(
            "SELECT process_seq,event_id FROM event_processing_order ORDER BY process_seq"
        ).fetchall()
        missing_raw = tuple(
            str(row[0])
            for row in db.execute(
                """
                SELECT r.raw_event_id FROM raw_discord_events r
                LEFT JOIN raw_receipt_order o ON o.raw_event_id=r.raw_event_id
                WHERE o.raw_event_id IS NULL ORDER BY r.raw_event_id
                """
            ).fetchall()
        )
        missing_events = tuple(
            str(row[0])
            for row in db.execute(
                """
                SELECT s.event_id FROM signal_events s
                LEFT JOIN event_processing_order o ON o.event_id=s.event_id
                WHERE o.event_id IS NULL ORDER BY s.event_id
                """
            ).fetchall()
        )
    receipt_sequences = [int(row[0]) for row in raw_rows]
    process_sequences = [int(row[0]) for row in event_rows]
    receipt_material = "\n".join(f"{int(seq)}:{raw_id}" for seq, raw_id in raw_rows)
    process_material = "\n".join(f"{int(seq)}:{event_id}" for seq, event_id in event_rows)
    return ProcessingOrderReport(
        raw_count=raw_count,
        receipt_ordered_count=len(raw_rows),
        signal_count=signal_count,
        process_ordered_count=len(event_rows),
        missing_raw_ids=missing_raw,
        missing_event_ids=missing_events,
        receipt_contiguous=_contiguous(receipt_sequences),
        process_contiguous=_contiguous(process_sequences),
        receipt_fingerprint=hashlib.sha256(receipt_material.encode("utf-8")).hexdigest(),
        process_fingerprint=hashlib.sha256(process_material.encode("utf-8")).hexdigest(),
    )
