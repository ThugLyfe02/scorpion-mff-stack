from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Iterator

from .domain import Effect, EventKind, RawDiscordMessage, SignalEvent


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS raw_discord_events (
    raw_event_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    source_ts_utc TEXT NOT NULL,
    received_ts_utc TEXT NOT NULL,
    edited_ts_utc TEXT,
    referenced_message_id TEXT,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    UNIQUE(message_id, content_sha256)
);
CREATE INDEX IF NOT EXISTS idx_raw_message_id ON raw_discord_events(message_id);

CREATE TABLE IF NOT EXISTS signal_events (
    event_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    contract_key TEXT,
    source_ts_utc TEXT NOT NULL,
    received_ts_utc TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signal_message_id ON signal_events(message_id);
CREATE INDEX IF NOT EXISTS idx_signal_source_ts ON signal_events(source_ts_utc);

CREATE TABLE IF NOT EXISTS proposed_effects (
    effect_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    contract_key TEXT,
    generation INTEGER NOT NULL,
    reason TEXT NOT NULL,
    quantity_hint INTEGER,
    metadata_json TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
    UNIQUE(source_event_id, kind, generation)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    effect_id INTEGER NOT NULL UNIQUE,
    approved_by TEXT NOT NULL,
    approved_ts_utc TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(effect_id) REFERENCES proposed_effects(effect_id)
);

CREATE TABLE IF NOT EXISTS heartbeats (
    component TEXT PRIMARY KEY,
    last_seen_ts_utc TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_flags (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts_utc TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=5000")
            yield db
        finally:
            db.close()

    def append_raw(self, raw: RawDiscordMessage) -> bool:
        import hashlib

        digest = hashlib.sha256(raw.content.encode()).hexdigest()
        revision = raw.edited_ts_utc.isoformat() if raw.edited_ts_utc else "create"
        raw_event_id = hashlib.sha256(f"{raw.message_id}|{revision}|{digest}".encode()).hexdigest()
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO raw_discord_events
                (raw_event_id,message_id,guild_id,channel_id,author_id,source_ts_utc,
                 received_ts_utc,
                 edited_ts_utc,referenced_message_id,content,content_sha256)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    raw_event_id,
                    raw.message_id,
                    raw.guild_id,
                    raw.channel_id,
                    raw.author_id,
                    raw.source_ts_utc.isoformat(),
                    raw.received_ts_utc.isoformat(),
                    raw.edited_ts_utc.isoformat() if raw.edited_ts_utc else None,
                    raw.referenced_message_id,
                    raw.content,
                    digest,
                ),
            )
            return cur.rowcount == 1

    @staticmethod
    def _signal_payload(event: SignalEvent) -> str:
        payload = asdict(event)
        payload["kind"] = event.kind.value
        for key in ("source_ts_utc", "received_ts_utc"):
            payload[key] = payload[key].isoformat()
        for key in ("strike", "referenced_price", "referenced_pct"):
            if payload.get(key) is not None:
                payload[key] = str(payload[key])
        if payload.get("expiry") is not None:
            payload["expiry"] = payload["expiry"].isoformat()
        return json.dumps(payload, sort_keys=True)

    def append_signal(self, event: SignalEvent) -> bool:
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO signal_events
                (event_id,message_id,kind,contract_key,source_ts_utc,received_ts_utc,
                 parser_version,payload_json)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    event.event_id,
                    event.message_id,
                    event.kind.value,
                    event.contract_key,
                    event.source_ts_utc.isoformat(),
                    event.received_ts_utc.isoformat(),
                    event.parser_version,
                    self._signal_payload(event),
                ),
            )
            return cur.rowcount == 1

    def load_signals(self) -> list[SignalEvent]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload_json FROM signal_events "
                "ORDER BY source_ts_utc, received_ts_utc, event_id"
            ).fetchall()
        events: list[SignalEvent] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["kind"] = EventKind(payload["kind"])
            payload["source_ts_utc"] = datetime.fromisoformat(
                payload["source_ts_utc"]
            ).astimezone(UTC)
            payload["received_ts_utc"] = datetime.fromisoformat(
                payload["received_ts_utc"]
            ).astimezone(UTC)
            if payload.get("expiry"):
                payload["expiry"] = date.fromisoformat(payload["expiry"])
            for key in ("strike", "referenced_price", "referenced_pct"):
                if payload.get(key) is not None:
                    payload[key] = Decimal(payload[key])
            events.append(SignalEvent(**payload))
        return events

    def contract_for_message(self, message_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT contract_key FROM signal_events
                WHERE message_id=? AND contract_key IS NOT NULL
                ORDER BY received_ts_utc DESC LIMIT 1
                """,
                (message_id,),
            ).fetchone()
        return row["contract_key"] if row else None

    def append_effects(self, effects: Iterable[Effect]) -> int:
        created = datetime.now(UTC).isoformat()
        inserted = 0
        with self.connect() as db:
            for effect in effects:
                cur = db.execute(
                    """
                    INSERT OR IGNORE INTO proposed_effects
                    (source_event_id,kind,contract_key,generation,reason,quantity_hint,
                     metadata_json,created_ts_utc)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        effect.source_event_id,
                        effect.kind.value,
                        effect.contract_key,
                        effect.generation,
                        effect.reason,
                        effect.quantity_hint,
                        json.dumps(effect.metadata, sort_keys=True),
                        created,
                    ),
                )
                inserted += max(cur.rowcount, 0)
        return inserted

    def heartbeat(self, component: str, **metadata: object) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO heartbeats(component,last_seen_ts_utc,metadata_json)
                VALUES (?,?,?)
                ON CONFLICT(component) DO UPDATE SET
                    last_seen_ts_utc=excluded.last_seen_ts_utc,
                    metadata_json=excluded.metadata_json
                """,
                (component, now, json.dumps(metadata, sort_keys=True)),
            )

    def set_halt(self, halted: bool, reason: str) -> None:
        now = datetime.now(UTC).isoformat()
        value = json.dumps({"halted": halted, "reason": reason}, sort_keys=True)
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO runtime_flags(key,value,updated_ts_utc)
                VALUES ('halt',?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,updated_ts_utc=excluded.updated_ts_utc
                """,
                (value, now),
            )

    def health_snapshot(self) -> dict[str, object]:
        with self.connect() as db:
            beats = {
                row["component"]: {
                    "last_seen_ts_utc": row["last_seen_ts_utc"],
                    "metadata": json.loads(row["metadata_json"]),
                }
                for row in db.execute("SELECT * FROM heartbeats")
            }
            flag = db.execute(
                "SELECT value,updated_ts_utc FROM runtime_flags WHERE key='halt'"
            ).fetchone()
            pending = db.execute(
                "SELECT COUNT(*) AS n FROM proposed_effects WHERE status='PENDING_REVIEW'"
            ).fetchone()["n"]
        return {
            "heartbeats": beats,
            "halt": json.loads(flag["value"]) if flag else {"halted": False, "reason": ""},
            "halt_updated_ts_utc": flag["updated_ts_utc"] if flag else None,
            "pending_review_effects": pending,
        }
