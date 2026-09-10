from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import connect

from .domain import RawDiscordMessage


_RECEIPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_receipt_order (
    receipt_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_event_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(raw_event_id) REFERENCES raw_discord_events(raw_event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_receipt_order_event
ON raw_receipt_order(raw_event_id);
"""


def append_raw_with_receipt(path: str | Path, raw: RawDiscordMessage) -> bool:
    """Persist raw evidence, pending state and receipt order in one FULL-sync transaction."""
    now = datetime.now(UTC).isoformat()
    with connect(str(path), timeout=5.0, isolation_level=None) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        db.executescript(_RECEIPT_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        try:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO raw_discord_events
                (raw_event_id,message_id,guild_id,channel_id,author_id,source_ts_utc,
                 received_ts_utc,edited_ts_utc,referenced_message_id,content,content_sha256)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    raw.revision_id,
                    raw.message_id,
                    raw.guild_id,
                    raw.channel_id,
                    raw.author_id,
                    raw.source_ts_utc.isoformat(),
                    raw.received_ts_utc.isoformat(),
                    raw.edited_ts_utc.isoformat() if raw.edited_ts_utc else None,
                    raw.referenced_message_id,
                    raw.content,
                    raw.content_sha256,
                ),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO raw_processing(raw_event_id,status,updated_ts_utc,error)
                VALUES (?, 'PENDING', ?, '')
                """,
                (raw.revision_id, now),
            )
            db.execute(
                "INSERT OR IGNORE INTO raw_receipt_order(raw_event_id) VALUES (?)",
                (raw.revision_id,),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    return cursor.rowcount == 1
