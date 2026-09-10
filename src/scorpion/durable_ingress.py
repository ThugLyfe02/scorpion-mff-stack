_RECEIPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_receipt_order (
    receipt_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_event_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(raw_event_id) REFERENCES raw_discord_events(raw_event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_receipt_order_event
ON raw_receipt_order(raw_event_id);
"""


def _iso(value: object) -> str:
    return str(getattr(value, "isoformat")())


def append_raw_with_receipt(path: object, raw: object) -> bool:
    """Persist raw evidence, pending state and receipt order in one FULL-sync transaction."""
    import sqlite3

    revision_id = str(getattr(raw, "revision_id"))
    received = getattr(raw, "received_ts_utc")
    edited = getattr(raw, "edited_ts_utc")
    now = _iso(received)
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
                    revision_id,
                    str(getattr(raw, "message_id")),
                    str(getattr(raw, "guild_id")),
                    str(getattr(raw, "channel_id")),
                    str(getattr(raw, "author_id")),
                    _iso(getattr(raw, "source_ts_utc")),
                    now,
                    _iso(edited) if edited is not None else None,
                    getattr(raw, "referenced_message_id"),
                    str(getattr(raw, "content")),
                    str(getattr(raw, "content_sha256")),
                ),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO raw_processing(raw_event_id,status,updated_ts_utc,error)
                VALUES (?, 'PENDING', ?, '')
                """,
                (revision_id, now),
            )
            db.execute(
                "INSERT OR IGNORE INTO raw_receipt_order(raw_event_id) VALUES (?)",
                (revision_id,),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    return cursor.rowcount == 1
