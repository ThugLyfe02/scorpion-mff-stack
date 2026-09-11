from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .research_breakthrough import BreakthroughReport, BreakthroughStatus

_GENESIS = "0" * 64


class ResearchInsightKind(StrEnum):
    BREAKTHROUGH_CANDIDATE = "BREAKTHROUGH_CANDIDATE"
    ROBUST_NEGATIVE_RESULT = "ROBUST_NEGATIVE_RESULT"
    CAUSAL_POLICY_UPGRADE = "CAUSAL_POLICY_UPGRADE"


@dataclass(frozen=True, slots=True)
class ResearchInsight:
    insight_id: str
    kind: ResearchInsightKind
    subject_id: str
    evidence_hash: str
    summary: str
    created_ts_utc: datetime
    record_hash: str


@dataclass(frozen=True, slots=True)
class ResearchInsightAck:
    ack_id: str
    insight_id: str
    operator: str
    note: str
    acknowledged_ts_utc: datetime
    record_hash: str


@dataclass(frozen=True, slots=True)
class ResearchInsightOutboxVerification:
    insights: int
    acknowledgements: int
    events: int
    head_event_hash: str
    chain_hash: str
    valid: bool
    failures: tuple[str, ...]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_insights (
    insight_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE,
    UNIQUE(kind,subject_id,evidence_hash)
);
CREATE TABLE IF NOT EXISTS research_insight_acks (
    ack_id TEXT PRIMARY KEY,
    insight_id TEXT NOT NULL UNIQUE,
    operator TEXT NOT NULL,
    note TEXT NOT NULL,
    acknowledged_ts_utc TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS research_insight_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    previous_event_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_ts_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_insight_integrity_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    event_count INTEGER NOT NULL,
    head_event_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS research_insights_no_update
BEFORE UPDATE ON research_insights
BEGIN SELECT RAISE(ABORT,'research_insights_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_insights_no_delete
BEFORE DELETE ON research_insights
BEGIN SELECT RAISE(ABORT,'research_insights_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_insight_acks_no_update
BEFORE UPDATE ON research_insight_acks
BEGIN SELECT RAISE(ABORT,'research_insight_acks_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_insight_acks_no_delete
BEFORE DELETE ON research_insight_acks
BEGIN SELECT RAISE(ABORT,'research_insight_acks_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_insight_events_no_update
BEFORE UPDATE ON research_insight_events
BEGIN SELECT RAISE(ABORT,'research_insight_events_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_insight_events_no_delete
BEFORE DELETE ON research_insight_events
BEGIN SELECT RAISE(ABORT,'research_insight_events_append_only'); END;
"""


def _json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(payload: object) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _insight_payload(item: ResearchInsight) -> dict[str, object]:
    return {
        "version": "research-insight-v1",
        "kind": item.kind.value,
        "subject_id": item.subject_id,
        "evidence_hash": item.evidence_hash,
        "summary": item.summary,
        "created_ts_utc": item.created_ts_utc.astimezone(UTC).isoformat(),
    }


def _ack_payload(item: ResearchInsightAck) -> dict[str, object]:
    return {
        "version": "research-insight-ack-v1",
        "insight_id": item.insight_id,
        "operator": item.operator,
        "note": item.note,
        "acknowledged_ts_utc": item.acknowledged_ts_utc.astimezone(UTC).isoformat(),
    }


def _append_event(
    db: sqlite3.Connection,
    *,
    event_kind: str,
    record_id: str,
    record_hash: str,
    created_ts_utc: datetime,
) -> None:
    previous = db.execute(
        "SELECT sequence,event_hash FROM research_insight_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(previous["sequence"]) + 1 if previous is not None else 1
    previous_hash = str(previous["event_hash"]) if previous is not None else _GENESIS
    event_hash = _hash(
        {
            "version": "research-insight-event-v1",
            "sequence": sequence,
            "event_kind": event_kind,
            "record_id": record_id,
            "record_hash": record_hash,
            "previous_event_hash": previous_hash,
            "created_ts_utc": created_ts_utc.astimezone(UTC).isoformat(),
        }
    )
    db.execute(
        """
        INSERT INTO research_insight_events
        (sequence,event_kind,record_id,record_hash,previous_event_hash,event_hash,created_ts_utc)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            sequence,
            event_kind,
            record_id,
            record_hash,
            previous_hash,
            event_hash,
            created_ts_utc.astimezone(UTC).isoformat(),
        ),
    )
    checkpoint = db.execute(
        "SELECT event_count,chain_hash FROM research_insight_integrity_state WHERE singleton_id=1"
    ).fetchone()
    count = int(checkpoint["event_count"]) if checkpoint is not None else 0
    chain = str(checkpoint["chain_hash"]) if checkpoint is not None else _GENESIS
    chain = hashlib.sha256(f"{chain}|{event_hash}".encode()).hexdigest()
    db.execute(
        """
        INSERT INTO research_insight_integrity_state
        (singleton_id,event_count,head_event_hash,chain_hash) VALUES (1,?,?,?)
        ON CONFLICT(singleton_id) DO UPDATE SET
            event_count=excluded.event_count,
            head_event_hash=excluded.head_event_hash,
            chain_hash=excluded.chain_hash
        """,
        (count + 1, event_hash, chain),
    )


def publish_research_insight(
    path: str | Path,
    *,
    kind: ResearchInsightKind,
    subject_id: str,
    evidence_hash: str,
    summary: str,
    created_ts_utc: datetime | None = None,
) -> ResearchInsight:
    for name, value in (
        ("subject_id", subject_id),
        ("evidence_hash", evidence_hash),
        ("summary", summary),
    ):
        if not value.strip():
            raise ValueError(f"{name} is required")
    created = (created_ts_utc or datetime.now(UTC)).astimezone(UTC)
    provisional = ResearchInsight(
        insight_id="",
        kind=kind,
        subject_id=subject_id,
        evidence_hash=evidence_hash,
        summary=summary,
        created_ts_utc=created,
        record_hash="",
    )
    payload = _insight_payload(provisional)
    insight_id = _hash({"version": "research-insight-id-v1", "payload": payload})
    record_hash = _hash(payload)
    db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            INSERT INTO research_insights
            (insight_id,kind,subject_id,evidence_hash,summary,created_ts_utc,record_hash)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                insight_id,
                kind.value,
                subject_id,
                evidence_hash,
                summary,
                created.isoformat(),
                record_hash,
            ),
        )
        _append_event(
            db,
            event_kind="INSIGHT",
            record_id=insight_id,
            record_hash=record_hash,
            created_ts_utc=created,
        )
        db.execute("COMMIT")
        return ResearchInsight(
            insight_id=insight_id,
            kind=kind,
            subject_id=subject_id,
            evidence_hash=evidence_hash,
            summary=summary,
            created_ts_utc=created,
            record_hash=record_hash,
        )
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def publish_breakthrough_candidate(
    path: str | Path,
    *,
    report: BreakthroughReport,
    summary: str,
    created_ts_utc: datetime | None = None,
) -> ResearchInsight:
    if report.status is not BreakthroughStatus.BREAKTHROUGH_CANDIDATE:
        raise ValueError("only replicated breakthrough candidates may enter the insight outbox")
    return publish_research_insight(
        path,
        kind=ResearchInsightKind.BREAKTHROUGH_CANDIDATE,
        subject_id=report.hypothesis_id,
        evidence_hash=report.report_hash,
        summary=summary,
        created_ts_utc=created_ts_utc,
    )


def acknowledge_research_insight(
    path: str | Path,
    *,
    insight_id: str,
    operator: str,
    note: str,
    acknowledged_ts_utc: datetime | None = None,
) -> ResearchInsightAck:
    if not insight_id.strip() or not operator.strip() or not note.strip():
        raise ValueError("insight_id, operator, and note are required")
    acknowledged = (acknowledged_ts_utc or datetime.now(UTC)).astimezone(UTC)
    provisional = ResearchInsightAck(
        ack_id="",
        insight_id=insight_id,
        operator=operator,
        note=note,
        acknowledged_ts_utc=acknowledged,
        record_hash="",
    )
    payload = _ack_payload(provisional)
    ack_id = _hash({"version": "research-insight-ack-id-v1", "payload": payload})
    record_hash = _hash(payload)
    db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        exists = db.execute(
            "SELECT 1 FROM research_insights WHERE insight_id=?",
            (insight_id,),
        ).fetchone()
        if exists is None:
            raise ValueError("research insight does not exist")
        db.execute(
            """
            INSERT INTO research_insight_acks
            (ack_id,insight_id,operator,note,acknowledged_ts_utc,record_hash)
            VALUES (?,?,?,?,?,?)
            """,
            (
                ack_id,
                insight_id,
                operator,
                note,
                acknowledged.isoformat(),
                record_hash,
            ),
        )
        _append_event(
            db,
            event_kind="ACK",
            record_id=ack_id,
            record_hash=record_hash,
            created_ts_utc=acknowledged,
        )
        db.execute("COMMIT")
        return ResearchInsightAck(
            ack_id=ack_id,
            insight_id=insight_id,
            operator=operator,
            note=note,
            acknowledged_ts_utc=acknowledged,
            record_hash=record_hash,
        )
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def list_pending_research_insights(path: str | Path) -> tuple[ResearchInsight, ...]:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        rows = db.execute(
            """
            SELECT insight.* FROM research_insights AS insight
            LEFT JOIN research_insight_acks AS ack ON ack.insight_id=insight.insight_id
            WHERE ack.insight_id IS NULL
            ORDER BY insight.created_ts_utc,insight.insight_id
            """
        ).fetchall()
    return tuple(
        ResearchInsight(
            insight_id=str(row["insight_id"]),
            kind=ResearchInsightKind(str(row["kind"])),
            subject_id=str(row["subject_id"]),
            evidence_hash=str(row["evidence_hash"]),
            summary=str(row["summary"]),
            created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC),
            record_hash=str(row["record_hash"]),
        )
        for row in rows
    )


def verify_research_insight_outbox(path: str | Path) -> ResearchInsightOutboxVerification:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        insights = db.execute("SELECT * FROM research_insights").fetchall()
        acknowledgements = db.execute("SELECT * FROM research_insight_acks").fetchall()
        events = db.execute("SELECT * FROM research_insight_events ORDER BY sequence").fetchall()
        checkpoint = db.execute(
            "SELECT * FROM research_insight_integrity_state WHERE singleton_id=1"
        ).fetchone()
    failures: list[str] = []
    records: dict[str, str] = {}
    insight_ids: set[str] = set()
    for row in insights:
        item = ResearchInsight(
            insight_id=str(row["insight_id"]),
            kind=ResearchInsightKind(str(row["kind"])),
            subject_id=str(row["subject_id"]),
            evidence_hash=str(row["evidence_hash"]),
            summary=str(row["summary"]),
            created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC),
            record_hash=str(row["record_hash"]),
        )
        payload = _insight_payload(item)
        if item.record_hash != _hash(payload):
            failures.append("research_insight_record_hash_mismatch")
        expected_id = _hash({"version": "research-insight-id-v1", "payload": payload})
        if item.insight_id != expected_id:
            failures.append("research_insight_id_mismatch")
        records[item.insight_id] = item.record_hash
        insight_ids.add(item.insight_id)
    for row in acknowledgements:
        item = ResearchInsightAck(
            ack_id=str(row["ack_id"]),
            insight_id=str(row["insight_id"]),
            operator=str(row["operator"]),
            note=str(row["note"]),
            acknowledged_ts_utc=datetime.fromisoformat(
                str(row["acknowledged_ts_utc"])
            ).astimezone(UTC),
            record_hash=str(row["record_hash"]),
        )
        if item.insight_id not in insight_ids:
            failures.append("research_insight_ack_parent_missing")
        payload = _ack_payload(item)
        if item.record_hash != _hash(payload):
            failures.append("research_insight_ack_record_hash_mismatch")
        expected_id = _hash({"version": "research-insight-ack-id-v1", "payload": payload})
        if item.ack_id != expected_id:
            failures.append("research_insight_ack_id_mismatch")
        records[item.ack_id] = item.record_hash
    previous = _GENESIS
    chain = _GENESIS
    covered: set[str] = set()
    for expected_sequence, row in enumerate(events, start=1):
        sequence = int(row["sequence"])
        if sequence != expected_sequence:
            failures.append("research_insight_event_sequence_gap")
        if str(row["previous_event_hash"]) != previous:
            failures.append("research_insight_event_parent_mismatch")
        record_id = str(row["record_id"])
        record_hash = str(row["record_hash"])
        if records.get(record_id) != record_hash:
            failures.append("research_insight_event_record_mismatch")
        created = datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC)
        expected_hash = _hash(
            {
                "version": "research-insight-event-v1",
                "sequence": sequence,
                "event_kind": str(row["event_kind"]),
                "record_id": record_id,
                "record_hash": record_hash,
                "previous_event_hash": str(row["previous_event_hash"]),
                "created_ts_utc": created.isoformat(),
            }
        )
        if str(row["event_hash"]) != expected_hash:
            failures.append("research_insight_event_hash_mismatch")
        covered.add(record_id)
        previous = str(row["event_hash"])
        chain = hashlib.sha256(f"{chain}|{row['event_hash']}".encode()).hexdigest()
    if covered != set(records):
        failures.append("research_insight_event_coverage_mismatch")
    checkpoint_valid = checkpoint is not None and (
        int(checkpoint["event_count"]) == len(events)
        and str(checkpoint["head_event_hash"]) == previous
        and str(checkpoint["chain_hash"]) == chain
    )
    if not checkpoint_valid:
        failures.append("research_insight_integrity_checkpoint_mismatch")
    unique = tuple(dict.fromkeys(failures))
    return ResearchInsightOutboxVerification(
        insights=len(insights),
        acknowledgements=len(acknowledgements),
        events=len(events),
        head_event_hash=previous,
        chain_hash=chain,
        valid=not unique,
        failures=unique,
    )
