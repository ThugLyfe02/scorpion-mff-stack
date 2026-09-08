from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_HISTORY_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS historical_discord_messages (
        message_id TEXT PRIMARY KEY,
        guild_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        author_id TEXT NOT NULL,
        source_ts_utc TEXT NOT NULL,
        edited_ts_utc TEXT,
        referenced_message_id TEXT,
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        archived_ts_utc TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_historical_channel_source
    ON historical_discord_messages(channel_id,source_ts_utc)
    """,
    """
    CREATE TABLE IF NOT EXISTS history_channel_checkpoints (
        channel_id TEXT PRIMARY KEY,
        oldest_message_id TEXT,
        oldest_source_ts_utc TEXT,
        newest_message_id TEXT,
        newest_source_ts_utc TEXT,
        message_count INTEGER NOT NULL DEFAULT 0,
        reached_beginning INTEGER NOT NULL DEFAULT 0,
        last_sync_ts_utc TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS history_sync_runs (
        run_id TEXT PRIMARY KEY,
        started_ts_utc TEXT NOT NULL,
        completed_ts_utc TEXT,
        channels_requested INTEGER NOT NULL,
        messages_seen INTEGER NOT NULL DEFAULT 0,
        messages_inserted INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    )
    """,
)


@dataclass(frozen=True, slots=True)
class ArchivedDiscordMessage:
    message_id: str
    guild_id: str
    channel_id: str
    author_id: str
    source_ts_utc: datetime
    content: str
    edited_ts_utc: datetime | None = None
    referenced_message_id: str | None = None

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChannelCompleteness:
    channel_id: str
    message_count: int
    oldest_source_ts_utc: datetime | None
    newest_source_ts_utc: datetime | None
    reached_beginning: bool

    @property
    def exhaustive(self) -> bool:
        return self.reached_beginning and self.message_count > 0


class HistoryArchive:
    """Read-only-source archive for legitimate Discord bot history collection.

    This store is intentionally separate from the live raw-processing journal. Historical
    backfills must never enqueue old Discord messages into the live money-path reducer.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self.connect() as db:
            for statement in _HISTORY_SCHEMA:
                db.execute(statement)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10.0)
        db.row_factory = sqlite3.Row
        return db

    def start_run(self, channels_requested: int) -> str:
        started = datetime.now(UTC)
        run_id = hashlib.sha256(
            f"{started.isoformat()}|{channels_requested}".encode("utf-8")
        ).hexdigest()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO history_sync_runs
                (run_id,started_ts_utc,channels_requested,status)
                VALUES (?,?,?,'RUNNING')
                """,
                (run_id, started.isoformat(), channels_requested),
            )
            db.commit()
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        messages_seen: int,
        messages_inserted: int,
        status: str = "COMPLETED",
        note: str = "",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE history_sync_runs
                SET completed_ts_utc=?,messages_seen=?,messages_inserted=?,status=?,note=?
                WHERE run_id=?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    messages_seen,
                    messages_inserted,
                    status,
                    note[:1000],
                    run_id,
                ),
            )
            db.commit()

    def append(self, message: ArchivedDiscordMessage) -> bool:
        source = message.source_ts_utc.astimezone(UTC)
        edited = message.edited_ts_utc.astimezone(UTC) if message.edited_ts_utc else None
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO historical_discord_messages
                (message_id,guild_id,channel_id,author_id,source_ts_utc,edited_ts_utc,
                 referenced_message_id,content,content_sha256,archived_ts_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    message.message_id,
                    message.guild_id,
                    message.channel_id,
                    message.author_id,
                    source.isoformat(),
                    edited.isoformat() if edited else None,
                    message.referenced_message_id,
                    message.content,
                    message.content_sha256,
                    datetime.now(UTC).isoformat(),
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def mark_channel_synced(self, channel_id: str, *, reached_beginning: bool) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            oldest = db.execute(
                """
                SELECT message_id,source_ts_utc FROM historical_discord_messages
                WHERE channel_id=? ORDER BY source_ts_utc ASC,message_id ASC LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
            newest = db.execute(
                """
                SELECT message_id,source_ts_utc FROM historical_discord_messages
                WHERE channel_id=? ORDER BY source_ts_utc DESC,message_id DESC LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
            count = int(
                db.execute(
                    "SELECT COUNT(*) FROM historical_discord_messages WHERE channel_id=?",
                    (channel_id,),
                ).fetchone()[0]
            )
            db.execute(
                """
                INSERT INTO history_channel_checkpoints
                (channel_id,oldest_message_id,oldest_source_ts_utc,newest_message_id,
                 newest_source_ts_utc,message_count,reached_beginning,last_sync_ts_utc)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    oldest_message_id=excluded.oldest_message_id,
                    oldest_source_ts_utc=excluded.oldest_source_ts_utc,
                    newest_message_id=excluded.newest_message_id,
                    newest_source_ts_utc=excluded.newest_source_ts_utc,
                    message_count=excluded.message_count,
                    reached_beginning=MAX(history_channel_checkpoints.reached_beginning,
                                          excluded.reached_beginning),
                    last_sync_ts_utc=excluded.last_sync_ts_utc
                """,
                (
                    channel_id,
                    str(oldest["message_id"]) if oldest else None,
                    str(oldest["source_ts_utc"]) if oldest else None,
                    str(newest["message_id"]) if newest else None,
                    str(newest["source_ts_utc"]) if newest else None,
                    count,
                    int(reached_beginning),
                    now,
                ),
            )
            db.commit()

    def completeness(self, channel_id: str) -> ChannelCompleteness:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM history_channel_checkpoints WHERE channel_id=?",
                (channel_id,),
            ).fetchone()
        if row is None:
            return ChannelCompleteness(channel_id, 0, None, None, False)
        oldest = (
            datetime.fromisoformat(str(row["oldest_source_ts_utc"])).astimezone(UTC)
            if row["oldest_source_ts_utc"]
            else None
        )
        newest = (
            datetime.fromisoformat(str(row["newest_source_ts_utc"])).astimezone(UTC)
            if row["newest_source_ts_utc"]
            else None
        )
        return ChannelCompleteness(
            channel_id=channel_id,
            message_count=int(row["message_count"]),
            oldest_source_ts_utc=oldest,
            newest_source_ts_utc=newest,
            reached_beginning=bool(row["reached_beginning"]),
        )

    def iter_channel(self, channel_id: str) -> tuple[ArchivedDiscordMessage, ...]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM historical_discord_messages
                WHERE channel_id=? ORDER BY source_ts_utc,message_id
                """,
                (channel_id,),
            ).fetchall()
        return tuple(
            ArchivedDiscordMessage(
                message_id=str(row["message_id"]),
                guild_id=str(row["guild_id"]),
                channel_id=str(row["channel_id"]),
                author_id=str(row["author_id"]),
                source_ts_utc=datetime.fromisoformat(str(row["source_ts_utc"])).astimezone(UTC),
                edited_ts_utc=(
                    datetime.fromisoformat(str(row["edited_ts_utc"])).astimezone(UTC)
                    if row["edited_ts_utc"]
                    else None
                ),
                referenced_message_id=(
                    str(row["referenced_message_id"])
                    if row["referenced_message_id"]
                    else None
                ),
                content=str(row["content"]),
            )
            for row in rows
        )
