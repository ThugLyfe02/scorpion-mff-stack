from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import datetime
import decimal
import json
import pathlib
import sqlite3

from .accuracy import AssociationEvidence, DecisionEvidence, LabeledDecision, percentile
from .domain import Effect, EventKind, RawDiscordMessage, SignalEvent
from .shadow import ShadowComparison, ShadowPrediction

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
    UNIQUE(message_id, content_sha256, edited_ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_raw_message_id ON raw_discord_events(message_id);

CREATE TABLE IF NOT EXISTS raw_processing (
    raw_event_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'PENDING',
    updated_ts_utc TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(raw_event_id) REFERENCES raw_discord_events(raw_event_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_processing_status ON raw_processing(status);

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

CREATE TABLE IF NOT EXISTS decision_audit (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    parser_rule TEXT NOT NULL,
    parser_confidence REAL NOT NULL,
    parser_latency_us INTEGER NOT NULL,
    pipeline_latency_us INTEGER NOT NULL,
    matched_terms_json TEXT NOT NULL,
    conflicts_json TEXT NOT NULL,
    association_method TEXT NOT NULL,
    association_confidence REAL NOT NULL,
    association_candidate_count INTEGER NOT NULL,
    created_ts_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_audit_created ON decision_audit(created_ts_utc);

CREATE TABLE IF NOT EXISTS adjudications (
    event_id TEXT PRIMARY KEY,
    expected_kind TEXT NOT NULL,
    expected_contract_key TEXT,
    reviewer TEXT NOT NULL,
    adjudicated_ts_utc TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shadow_predictions (
    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    predicted_kind TEXT NOT NULL,
    confidence REAL NOT NULL,
    latency_ms REAL NOT NULL,
    rationale TEXT NOT NULL,
    disagreement TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    UNIQUE(event_id, model_name, model_version)
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
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute(
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
            db.execute("COMMIT")
            return cur.rowcount == 1

    def load_pending_raw(self) -> list[RawDiscordMessage]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT r.* FROM raw_discord_events r
                JOIN raw_processing p ON p.raw_event_id=r.raw_event_id
                WHERE p.status='PENDING'
                ORDER BY r.source_ts_utc, r.received_ts_utc, r.raw_event_id
                """
            ).fetchall()
        messages: list[RawDiscordMessage] = []
        for row in rows:
            messages.append(
                RawDiscordMessage(
                    message_id=row["message_id"],
                    guild_id=row["guild_id"],
                    channel_id=row["channel_id"],
                    author_id=row["author_id"],
                    content=row["content"],
                    source_ts_utc=datetime.datetime.fromisoformat(row["source_ts_utc"]),
                    received_ts_utc=datetime.datetime.fromisoformat(row["received_ts_utc"]),
                    edited_ts_utc=(
                        datetime.datetime.fromisoformat(row["edited_ts_utc"])
                        if row["edited_ts_utc"]
                        else None
                    ),
                    referenced_message_id=row["referenced_message_id"],
                )
            )
        return messages

    def mark_raw_processed(self, raw_event_id: str) -> None:
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self.connect() as db:
            db.execute(
                "UPDATE raw_processing SET status='DONE',updated_ts_utc=?,error='' "
                "WHERE raw_event_id=?",
                (now, raw_event_id),
            )

    def mark_raw_failed(self, raw_event_id: str, error: str) -> None:
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self.connect() as db:
            db.execute(
                "UPDATE raw_processing SET status='PENDING',updated_ts_utc=?,error=? "
                "WHERE raw_event_id=?",
                (now, error[:500], raw_event_id),
            )

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

    def append_decision_audit(
        self,
        event: SignalEvent,
        parser: DecisionEvidence,
        association: AssociationEvidence,
        *,
        pipeline_latency_us: int,
    ) -> None:
        created = datetime.datetime.now(datetime.UTC).isoformat()
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO decision_audit
                (event_id,kind,reason,parser_rule,parser_confidence,parser_latency_us,
                 pipeline_latency_us,matched_terms_json,conflicts_json,association_method,
                 association_confidence,association_candidate_count,created_ts_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.event_id,
                    event.kind.value,
                    event.reason,
                    parser.rule_id,
                    parser.confidence,
                    parser.latency_us,
                    pipeline_latency_us,
                    json.dumps(parser.matched_terms),
                    json.dumps(parser.conflicts),
                    association.method,
                    association.confidence,
                    association.candidate_count,
                    created,
                ),
            )

    def decision_health(self, *, window: int = 200) -> dict[str, float | int]:
        if window <= 0:
            raise ValueError("window must be positive")
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM decision_audit ORDER BY created_ts_utc DESC LIMIT ?",
                (window,),
            ).fetchall()
        count = len(rows)
        if count == 0:
            return {
                "count": 0,
                "ambiguity_rate": 0.0,
                "low_confidence_rate": 0.0,
                "unresolved_association_rate": 0.0,
                "parser_p95_us": 0.0,
                "pipeline_p95_us": 0.0,
            }
        ambiguity = sum(row["kind"] == EventKind.AMBIGUOUS.value for row in rows)
        low_confidence = sum(float(row["parser_confidence"]) < 0.75 for row in rows)
        unresolved = sum(
            row["association_method"]
            in {"unassociated", "explicit_ticker_ambiguous", "source_lineage_ambiguous"}
            for row in rows
        )
        parser_latencies = [int(row["parser_latency_us"]) for row in rows]
        pipeline_latencies = [int(row["pipeline_latency_us"]) for row in rows]
        return {
            "count": count,
            "ambiguity_rate": ambiguity / count,
            "low_confidence_rate": low_confidence / count,
            "unresolved_association_rate": unresolved / count,
            "parser_p95_us": percentile(parser_latencies, 0.95),
            "pipeline_p95_us": percentile(pipeline_latencies, 0.95),
        }

    def record_adjudication(
        self,
        event_id: str,
        expected_kind: EventKind,
        *,
        reviewer: str,
        expected_contract_key: str | None = None,
        note: str = "",
    ) -> None:
        if not reviewer.strip():
            raise ValueError("reviewer is required")
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO adjudications
                (event_id,expected_kind,expected_contract_key,reviewer,adjudicated_ts_utc,note)
                VALUES (?,?,?,?,?,?)
                """,
                (event_id, expected_kind.value, expected_contract_key, reviewer, now, note),
            )

    def adjudicated_samples(self) -> list[LabeledDecision]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT s.kind AS predicted_kind,a.expected_kind AS expected_kind
                FROM adjudications a JOIN signal_events s ON s.event_id=a.event_id
                ORDER BY a.adjudicated_ts_utc
                """
            ).fetchall()
        return [
            LabeledDecision(
                expected=EventKind(row["expected_kind"]),
                predicted=EventKind(row["predicted_kind"]),
            )
            for row in rows
        ]

    def append_shadow_prediction(
        self,
        event_id: str,
        prediction: ShadowPrediction,
        comparison: ShadowComparison,
    ) -> None:
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO shadow_predictions
                (event_id,model_name,model_version,predicted_kind,confidence,latency_ms,
                 rationale,disagreement,created_ts_utc)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    prediction.model_name,
                    prediction.model_version,
                    prediction.predicted_kind.value,
                    prediction.confidence,
                    prediction.latency_ms,
                    prediction.rationale,
                    comparison.disagreement.value,
                    now,
                ),
            )

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
            pending_raw = db.execute(
                "SELECT COUNT(*) AS n FROM raw_processing WHERE status='PENDING'"
            ).fetchone()["n"]
        return {
            "heartbeats": beats,
            "halt": json.loads(flag["value"]) if flag else {"halted": False, "reason": ""},
            "halt_updated_ts_utc": flag["updated_ts_utc"] if flag else None,
            "pending_review_effects": pending,
            "pending_raw_revisions": pending_raw,
            "decision_health": self.decision_health(),
        }
