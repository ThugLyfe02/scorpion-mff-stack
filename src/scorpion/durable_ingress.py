from datetime import datetime
from typing import Protocol


class DurableRawMessage(Protocol):
    @property
    def revision_id(self) -> str: ...

    @property
    def message_id(self) -> str: ...

    @property
    def guild_id(self) -> str: ...

    @property
    def channel_id(self) -> str: ...

    @property
    def author_id(self) -> str: ...

    @property
    def source_ts_utc(self) -> datetime: ...

    @property
    def received_ts_utc(self) -> datetime: ...

    @property
    def edited_ts_utc(self) -> datetime | None: ...

    @property
    def referenced_message_id(self) -> str | None: ...

    @property
    def content(self) -> str: ...

    @property
    def content_sha256(self) -> str: ...


_RECEIPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_receipt_order (
    receipt_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_event_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(raw_event_id) REFERENCES raw_discord_events(raw_event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_receipt_order_event
ON raw_receipt_order(raw_event_id);
"""


def append_raw_with_receipt(path: object, raw: DurableRawMessage) -> bool:
    """Persist raw evidence, pending state and receipt order in one FULL-sync transaction."""
    import sqlite3

    now = raw.received_ts_utc.isoformat()
    with sqlite3.connect(str(path), timeout=5.0, isolation_level=None) as db:
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
                    now,
                    raw.edited_ts_utc.isoformat() if raw.edited_ts_utc is not None else None,
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
