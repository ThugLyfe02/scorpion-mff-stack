from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import datetime
import decimal
import json
import pathlib
import sqlite3

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

-- Outbox state lives beside the effects, not inside them: `proposed_effects` is the
-- immutable proposal record, and mutating it to add delivery bookkeeping would make the
-- event log lie. One row per effect, created lazily, additive on an existing database.
CREATE TABLE IF NOT EXISTS outbox_state (
    effect_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING|SENT|RETRY|DEAD|REFUSED
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_utc TEXT NOT NULL,
    lease_owner TEXT,
    lease_until_utc TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    broker_ref TEXT NOT NULL DEFAULT '',
    updated_ts_utc TEXT NOT NULL,
    FOREIGN KEY(effect_id) REFERENCES proposed_effects(effect_id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox_state(status, next_attempt_utc);

-- Realised executions, as reported by a venue. Price columns are TEXT to keep Decimal
-- exactness across the SQLite boundary; TCA reads them back with Decimal().
CREATE TABLE IF NOT EXISTS executions (
    execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    effect_id INTEGER,
    idempotency_key TEXT NOT NULL UNIQUE,
    contract_key TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    limit_price TEXT NOT NULL,
    fill_price TEXT NOT NULL,
    fill_qty INTEGER NOT NULL,
    intent_ts_utc TEXT NOT NULL,
    fill_ts_utc TEXT NOT NULL,
    broker_ref TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_executions_contract ON executions(contract_key);
"""


class Store:
    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = str(path)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextlib.contextmanager
    def connect(self) -> collections.abc.Iterator[sqlite3.Connection]:
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
        payload = dataclasses.asdict(event)
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

    def append_effects(self, effects: collections.abc.Iterable[Effect]) -> int:
        created = datetime.datetime.now(datetime.UTC).isoformat()
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
        now = datetime.datetime.now(datetime.UTC).isoformat()
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
        now = datetime.datetime.now(datetime.UTC).isoformat()
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

    def halt_state(self) -> tuple[bool, str]:
        """The persisted halt, so a restart cannot forget a stand-down."""
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM runtime_flags WHERE key='halt'"
            ).fetchone()
        if not row:
            return False, ""
        payload = json.loads(row["value"])
        return bool(payload.get("halted", False)), str(payload.get("reason", ""))

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

    # ── durable outbox ───────────────────────────────────────────────────────
    @staticmethod
    def _ensure_outbox_rows(db: sqlite3.Connection) -> None:
        """Materialize delivery state for effects that have none yet.

        Idempotent and additive, so an existing database gains the outbox without a
        migration and a crash between the two inserts cannot strand an effect.
        """
        db.execute(
            """
            INSERT OR IGNORE INTO outbox_state
            (effect_id,status,next_attempt_utc,updated_ts_utc)
            SELECT effect_id,'PENDING',created_ts_utc,created_ts_utc
            FROM proposed_effects
            WHERE effect_id NOT IN (SELECT effect_id FROM outbox_state)
            """
        )

    def claim_due(
        self,
        *,
        owner: str,
        now: datetime.datetime,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, object]]:
        """Lease up to `limit` due effects to `owner`, and count the attempt.

        One `BEGIN IMMEDIATE` transaction so two workers cannot claim the same row: the
        select and the lease update are atomic with respect to other writers. The attempt
        counter increments at claim time, which is what stops a crash-looping worker from
        retrying a poison effect forever.
        """
        if limit < 1:
            return []
        if not owner.strip():
            raise ValueError("owner is required")
        now_s = now.isoformat()
        lease_s = (now + datetime.timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_outbox_rows(db)
                rows = db.execute(
                    """
                    SELECT e.effect_id, e.source_event_id, e.kind, e.contract_key,
                           e.generation, e.reason, e.quantity_hint, o.attempts
                    FROM outbox_state o
                    JOIN proposed_effects e ON e.effect_id = o.effect_id
                    WHERE o.status IN ('PENDING','RETRY')
                      AND o.next_attempt_utc <= ?
                      AND (o.lease_until_utc IS NULL OR o.lease_until_utc < ?)
                    ORDER BY o.effect_id
                    LIMIT ?
                    """,
                    (now_s, now_s, limit),
                ).fetchall()
                ids = [int(row["effect_id"]) for row in rows]
                for effect_id in ids:
                    db.execute(
                        """
                        UPDATE outbox_state
                        SET lease_owner=?, lease_until_utc=?, attempts=attempts+1,
                            updated_ts_utc=?
                        WHERE effect_id=?
                        """,
                        (owner, lease_s, now_s, effect_id),
                    )
                # Report the attempt that is now IN PROGRESS, not the count from before it:
                # the dispatcher derives its backoff from this number, so returning the
                # stale value makes every retry one step too eager.
                claimed = []
                for row in rows:
                    item = dict(row)
                    item["attempts"] = int(row["attempts"]) + 1
                    claimed.append(item)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return claimed

    def mark_sent(
        self, *, effect_id: int, broker_ref: str, now: datetime.datetime
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE outbox_state
                SET status='SENT', broker_ref=?, last_error='', lease_owner=NULL,
                    lease_until_utc=NULL, updated_ts_utc=?
                WHERE effect_id=?
                """,
                (broker_ref, now.isoformat(), effect_id),
            )

    def mark_refused(
        self, *, effect_id: int, reason: str, now: datetime.datetime
    ) -> None:
        """A refusal is terminal. Retrying it would be retrying a decision, not a delivery."""
        with self.connect() as db:
            db.execute(
                """
                UPDATE outbox_state
                SET status='REFUSED', last_error=?, lease_owner=NULL,
                    lease_until_utc=NULL, updated_ts_utc=?
                WHERE effect_id=?
                """,
                (reason, now.isoformat(), effect_id),
            )

    def mark_failed(
        self,
        *,
        effect_id: int,
        error: str,
        now: datetime.datetime,
        backoff_seconds: int,
        max_attempts: int,
    ) -> str:
        """Record a failed attempt; dead-letter once the budget is spent. Returns the status."""
        with self.connect() as db:
            row = db.execute(
                "SELECT attempts FROM outbox_state WHERE effect_id=?", (effect_id,)
            ).fetchone()
            attempts = int(row["attempts"]) if row else max_attempts
            status = "DEAD" if attempts >= max_attempts else "RETRY"
            next_attempt = (
                now + datetime.timedelta(seconds=max(0, backoff_seconds))
            ).isoformat()
            db.execute(
                """
                UPDATE outbox_state
                SET status=?, last_error=?, next_attempt_utc=?, lease_owner=NULL,
                    lease_until_utc=NULL, updated_ts_utc=?
                WHERE effect_id=?
                """,
                (status, error[:500], next_attempt, now.isoformat(), effect_id),
            )
        return status

    def record_execution(
        self,
        *,
        effect_id: int | None,
        idempotency_key: str,
        contract_key: str,
        side: str,
        quantity: int,
        limit_price: str,
        fill_price: str,
        fill_qty: int,
        intent_ts: datetime.datetime,
        fill_ts: datetime.datetime,
        broker_ref: str = "",
    ) -> bool:
        """Persist one venue execution. Idempotent on the key, so a retry cannot double-count."""
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO executions
                (effect_id,idempotency_key,contract_key,side,quantity,limit_price,
                 fill_price,fill_qty,intent_ts_utc,fill_ts_utc,broker_ref)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    effect_id,
                    idempotency_key,
                    contract_key,
                    side,
                    quantity,
                    limit_price,
                    fill_price,
                    fill_qty,
                    intent_ts.isoformat(),
                    fill_ts.isoformat(),
                    broker_ref,
                ),
            )
            return cur.rowcount == 1

    def load_executions(self) -> list[dict[str, object]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM executions ORDER BY fill_ts_utc, execution_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def outbox_snapshot(self) -> dict[str, object]:
        with self.connect() as db:
            self._ensure_outbox_rows(db)
            counts = {
                str(row["status"]): int(row["n"])
                for row in db.execute(
                    "SELECT status, COUNT(*) AS n FROM outbox_state GROUP BY status"
                )
            }
            oldest = db.execute(
                "SELECT MIN(next_attempt_utc) AS ts FROM outbox_state WHERE status IN "
                "('PENDING','RETRY')"
            ).fetchone()["ts"]
            dead = db.execute(
                "SELECT COUNT(*) AS n FROM outbox_state WHERE status='DEAD'"
            ).fetchone()["n"]
        return {
            "counts": counts,
            "oldest_pending_utc": oldest,
            "dead_letter": int(dead),
        }
