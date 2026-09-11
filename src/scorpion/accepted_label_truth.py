from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .domain import EventKind
from .label_consensus import ConsensusStatus, EventConsensus

_GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accepted_label_revisions (
    revision_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    source_revision_id TEXT NOT NULL,
    label TEXT NOT NULL,
    confidence REAL NOT NULL,
    method TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    accepted_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    supersedes_revision_id TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    UNIQUE(event_id,revision_number)
);
CREATE TABLE IF NOT EXISTS accepted_label_heads (
    event_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    updated_ts_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accepted_label_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_hash TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    previous_event_hash TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accepted_label_integrity_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    event_count INTEGER NOT NULL,
    head_event_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS accepted_label_revisions_no_update
BEFORE UPDATE ON accepted_label_revisions
BEGIN SELECT RAISE(ABORT,'accepted_label_revisions_append_only'); END;
CREATE TRIGGER IF NOT EXISTS accepted_label_revisions_no_delete
BEFORE DELETE ON accepted_label_revisions
BEGIN SELECT RAISE(ABORT,'accepted_label_revisions_append_only'); END;
CREATE TRIGGER IF NOT EXISTS accepted_label_events_no_update
BEFORE UPDATE ON accepted_label_events
BEGIN SELECT RAISE(ABORT,'accepted_label_events_append_only'); END;
CREATE TRIGGER IF NOT EXISTS accepted_label_events_no_delete
BEFORE DELETE ON accepted_label_events
BEGIN SELECT RAISE(ABORT,'accepted_label_events_append_only'); END;
CREATE INDEX IF NOT EXISTS idx_accepted_label_event_revision
ON accepted_label_revisions(event_id,revision_number);
CREATE INDEX IF NOT EXISTS idx_accepted_label_events_sequence
ON accepted_label_events(sequence);
"""


class AcceptedLabelMethod(StrEnum):
    CONSENSUS = "CONSENSUS"
    MANUAL_ADJUDICATION = "MANUAL_ADJUDICATION"
    CORRECTION = "CORRECTION"


@dataclass(frozen=True, slots=True)
class AcceptedLabelRevision:
    revision_id: str
    event_id: str
    revision_number: int
    source_revision_id: str
    label: EventKind
    confidence: float
    method: AcceptedLabelMethod
    evidence_ids: tuple[str, ...]
    accepted_by: str
    reason: str
    supersedes_revision_id: str
    created_ts_utc: datetime


@dataclass(frozen=True, slots=True)
class AcceptedTruthBinding:
    event_id: str
    revision_id: str
    revision_number: int
    source_revision_id: str
    label: EventKind
    confidence: float


@dataclass(frozen=True, slots=True)
class AcceptedTruthSnapshot:
    as_of_sequence: int
    ledger_head_event_hash: str
    ledger_chain_hash: str
    bindings: tuple[AcceptedTruthBinding, ...]
    snapshot_hash: str

    @property
    def by_event(self) -> dict[str, AcceptedTruthBinding]:
        return {item.event_id: item for item in self.bindings}


@dataclass(frozen=True, slots=True)
class AcceptedTruthLedgerVerification:
    revisions: int
    events: int
    head_event_hash: str
    chain_hash: str
    valid: bool
    failures: tuple[str, ...]


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _revision_payload(
    *,
    event_id: str,
    revision_number: int,
    source_revision_id: str,
    label: EventKind,
    confidence: float,
    method: AcceptedLabelMethod,
    evidence_ids: tuple[str, ...],
    accepted_by: str,
    reason: str,
    supersedes_revision_id: str,
    created_ts_utc: datetime,
) -> dict[str, object]:
    return {
        "version": "accepted-label-revision-v1",
        "event_id": event_id,
        "revision_number": revision_number,
        "source_revision_id": source_revision_id,
        "label": label.value,
        "confidence": round(confidence, 12),
        "method": method.value,
        "evidence_ids": evidence_ids,
        "accepted_by": accepted_by,
        "reason": reason,
        "supersedes_revision_id": supersedes_revision_id,
        "created_ts_utc": created_ts_utc.astimezone(UTC).isoformat(),
    }


def _event_hash(
    *,
    sequence: int,
    event_id: str,
    revision_id: str,
    previous_event_hash: str,
    created_ts_utc: datetime,
) -> str:
    return _hash(
        {
            "version": "accepted-label-ledger-event-v1",
            "sequence": sequence,
            "event_id": event_id,
            "revision_id": revision_id,
            "previous_event_hash": previous_event_hash,
            "created_ts_utc": created_ts_utc.astimezone(UTC).isoformat(),
        }
    )


def _decode_revision(row: sqlite3.Row) -> AcceptedLabelRevision:
    raw_evidence = json.loads(str(row["evidence_ids_json"]))
    if not isinstance(raw_evidence, list):
        raise ValueError("accepted label evidence_ids_json must be a list")
    return AcceptedLabelRevision(
        revision_id=str(row["revision_id"]),
        event_id=str(row["event_id"]),
        revision_number=int(row["revision_number"]),
        source_revision_id=str(row["source_revision_id"]),
        label=EventKind(str(row["label"])),
        confidence=float(row["confidence"]),
        method=AcceptedLabelMethod(str(row["method"])),
        evidence_ids=tuple(str(item) for item in raw_evidence),
        accepted_by=str(row["accepted_by"]),
        reason=str(row["reason"]),
        supersedes_revision_id=str(row["supersedes_revision_id"]),
        created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC),
    )


def accept_label_revision(
    path: str | Path,
    *,
    event_id: str,
    source_revision_id: str,
    label: EventKind,
    confidence: float,
    method: AcceptedLabelMethod,
    evidence_ids: tuple[str, ...],
    accepted_by: str,
    reason: str,
    expected_current_revision_id: str | None,
    created_ts_utc: datetime | None = None,
) -> AcceptedLabelRevision:
    """Append accepted truth under a compare-and-swap head precondition."""
    for name, value in (
        ("event_id", event_id),
        ("source_revision_id", source_revision_id),
        ("accepted_by", accepted_by),
        ("reason", reason),
    ):
        if not value.strip():
            raise ValueError(f"{name} is required")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be finite and in [0,1]")
    evidence = tuple(sorted(set(evidence_ids)))
    if not evidence or any(not item.strip() for item in evidence):
        raise ValueError("evidence_ids must contain non-empty immutable evidence identities")
    timestamp = created_ts_utc or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("created_ts_utc must be timezone-aware")
    timestamp = timestamp.astimezone(UTC)

    db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        head = db.execute(
            """
            SELECT head.revision_id,head.revision_number,revision.created_ts_utc
            FROM accepted_label_heads AS head
            JOIN accepted_label_revisions AS revision ON revision.revision_id=head.revision_id
            WHERE head.event_id=?
            """,
            (event_id,),
        ).fetchone()
        current_revision_id = str(head["revision_id"]) if head is not None else None
        if current_revision_id != expected_current_revision_id:
            raise ValueError("accepted truth head changed; refresh before revising")
        if head is not None:
            previous_created = datetime.fromisoformat(str(head["created_ts_utc"])).astimezone(UTC)
            if timestamp < previous_created:
                raise ValueError("accepted truth revision timestamp cannot precede current head")
        revision_number = int(head["revision_number"]) + 1 if head is not None else 1
        supersedes = current_revision_id or ""
        payload = _revision_payload(
            event_id=event_id,
            revision_number=revision_number,
            source_revision_id=source_revision_id,
            label=label,
            confidence=confidence,
            method=method,
            evidence_ids=evidence,
            accepted_by=accepted_by,
            reason=reason,
            supersedes_revision_id=supersedes,
            created_ts_utc=timestamp,
        )
        revision_id = _hash(payload)
        db.execute(
            """
            INSERT INTO accepted_label_revisions
            (revision_id,event_id,revision_number,source_revision_id,label,confidence,method,
             evidence_ids_json,accepted_by,reason,supersedes_revision_id,created_ts_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                revision_id,
                event_id,
                revision_number,
                source_revision_id,
                label.value,
                confidence,
                method.value,
                _canonical_json(list(evidence)),
                accepted_by,
                reason,
                supersedes,
                timestamp.isoformat(),
            ),
        )
        db.execute(
            """
            INSERT INTO accepted_label_heads(event_id,revision_id,revision_number,updated_ts_utc)
            VALUES (?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                revision_id=excluded.revision_id,
                revision_number=excluded.revision_number,
                updated_ts_utc=excluded.updated_ts_utc
            """,
            (event_id, revision_id, revision_number, timestamp.isoformat()),
        )
        previous_event = db.execute(
            "SELECT sequence,event_hash FROM accepted_label_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        sequence = int(previous_event["sequence"]) + 1 if previous_event is not None else 1
        previous_event_hash = (
            str(previous_event["event_hash"]) if previous_event is not None else _GENESIS
        )
        ledger_event_hash = _event_hash(
            sequence=sequence,
            event_id=event_id,
            revision_id=revision_id,
            previous_event_hash=previous_event_hash,
            created_ts_utc=timestamp,
        )
        db.execute(
            """
            INSERT INTO accepted_label_events
            (sequence,event_hash,event_id,revision_id,previous_event_hash,created_ts_utc)
            VALUES (?,?,?,?,?,?)
            """,
            (
                sequence,
                ledger_event_hash,
                event_id,
                revision_id,
                previous_event_hash,
                timestamp.isoformat(),
            ),
        )
        checkpoint = db.execute(
            "SELECT event_count,chain_hash FROM accepted_label_integrity_state WHERE singleton_id=1"
        ).fetchone()
        count = int(checkpoint["event_count"]) if checkpoint is not None else 0
        chain = str(checkpoint["chain_hash"]) if checkpoint is not None else _GENESIS
        chain = hashlib.sha256(f"{chain}|{ledger_event_hash}".encode()).hexdigest()
        db.execute(
            """
            INSERT INTO accepted_label_integrity_state
            (singleton_id,event_count,head_event_hash,chain_hash) VALUES (1,?,?,?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                event_count=excluded.event_count,
                head_event_hash=excluded.head_event_hash,
                chain_hash=excluded.chain_hash
            """,
            (count + 1, ledger_event_hash, chain),
        )
        db.execute("COMMIT")
        return AcceptedLabelRevision(
            revision_id=revision_id,
            event_id=event_id,
            revision_number=revision_number,
            source_revision_id=source_revision_id,
            label=label,
            confidence=confidence,
            method=method,
            evidence_ids=evidence,
            accepted_by=accepted_by,
            reason=reason,
            supersedes_revision_id=supersedes,
            created_ts_utc=timestamp,
        )
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def accept_consensus_revision(
    path: str | Path,
    *,
    consensus: EventConsensus,
    source_revision_id: str,
    annotation_ids: tuple[str, ...],
    accepted_by: str,
    reason: str,
    expected_current_revision_id: str | None,
    created_ts_utc: datetime | None = None,
) -> AcceptedLabelRevision:
    if consensus.status is not ConsensusStatus.ACCEPTED or consensus.label is None:
        raise ValueError("consensus event is not accepted")
    return accept_label_revision(
        path,
        event_id=consensus.event_id,
        source_revision_id=source_revision_id,
        label=EventKind(consensus.label),
        confidence=consensus.probability,
        method=AcceptedLabelMethod.CONSENSUS,
        evidence_ids=annotation_ids,
        accepted_by=accepted_by,
        reason=reason,
        expected_current_revision_id=expected_current_revision_id,
        created_ts_utc=created_ts_utc,
    )


def get_current_revision(path: str | Path, event_id: str) -> AcceptedLabelRevision | None:
    if not event_id.strip():
        raise ValueError("event_id is required")
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        row = db.execute(
            """
            SELECT revision.* FROM accepted_label_heads AS head
            JOIN accepted_label_revisions AS revision ON revision.revision_id=head.revision_id
            WHERE head.event_id=?
            """,
            (event_id,),
        ).fetchone()
    return _decode_revision(row) if row is not None else None


def get_revision(path: str | Path, revision_id: str) -> AcceptedLabelRevision:
    if not revision_id.strip():
        raise ValueError("revision_id is required")
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        row = db.execute(
            "SELECT * FROM accepted_label_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
    if row is None:
        raise KeyError(revision_id)
    return _decode_revision(row)


def verify_accepted_truth_ledger(path: str | Path) -> AcceptedTruthLedgerVerification:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        revisions = db.execute(
            "SELECT * FROM accepted_label_revisions ORDER BY event_id,revision_number"
        ).fetchall()
        events = db.execute("SELECT * FROM accepted_label_events ORDER BY sequence").fetchall()
        heads = db.execute("SELECT * FROM accepted_label_heads ORDER BY event_id").fetchall()
        checkpoint = db.execute(
            "SELECT * FROM accepted_label_integrity_state WHERE singleton_id=1"
        ).fetchone()

    failures: list[str] = []
    revision_by_id: dict[str, AcceptedLabelRevision] = {}
    per_event_previous: dict[str, str] = {}
    per_event_number: dict[str, int] = {}
    for row in revisions:
        try:
            revision = _decode_revision(row)
        except (ValueError, TypeError, json.JSONDecodeError):
            failures.append("accepted_truth_revision_decode_failure")
            continue
        expected_id = _hash(
            _revision_payload(
                event_id=revision.event_id,
                revision_number=revision.revision_number,
                source_revision_id=revision.source_revision_id,
                label=revision.label,
                confidence=revision.confidence,
                method=revision.method,
                evidence_ids=revision.evidence_ids,
                accepted_by=revision.accepted_by,
                reason=revision.reason,
                supersedes_revision_id=revision.supersedes_revision_id,
                created_ts_utc=revision.created_ts_utc,
            )
        )
        if revision.revision_id != expected_id:
            failures.append(f"accepted_truth_revision_hash_mismatch:{revision.event_id}")
        expected_number = per_event_number.get(revision.event_id, 0) + 1
        expected_parent = per_event_previous.get(revision.event_id, "")
        if revision.revision_number != expected_number:
            failures.append(f"accepted_truth_revision_sequence_gap:{revision.event_id}")
        if revision.supersedes_revision_id != expected_parent:
            failures.append(f"accepted_truth_revision_parent_mismatch:{revision.event_id}")
        per_event_number[revision.event_id] = revision.revision_number
        per_event_previous[revision.event_id] = revision.revision_id
        revision_by_id[revision.revision_id] = revision

    previous_event_hash = _GENESIS
    chain = _GENESIS
    replay_heads: dict[str, str] = {}
    for expected_sequence, row in enumerate(events, start=1):
        sequence = int(row["sequence"])
        created = datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC)
        expected = _event_hash(
            sequence=sequence,
            event_id=str(row["event_id"]),
            revision_id=str(row["revision_id"]),
            previous_event_hash=str(row["previous_event_hash"]),
            created_ts_utc=created,
        )
        if sequence != expected_sequence:
            failures.append("accepted_truth_event_sequence_gap")
        if str(row["previous_event_hash"]) != previous_event_hash:
            failures.append("accepted_truth_event_parent_mismatch")
        if str(row["event_hash"]) != expected:
            failures.append("accepted_truth_event_hash_mismatch")
        revision = revision_by_id.get(str(row["revision_id"]))
        if revision is None or revision.event_id != str(row["event_id"]):
            failures.append("accepted_truth_event_revision_mismatch")
        replay_heads[str(row["event_id"])] = str(row["revision_id"])
        previous_event_hash = str(row["event_hash"])
        chain = hashlib.sha256(f"{chain}|{row['event_hash']}".encode()).hexdigest()

    event_revision_ids = {str(row["revision_id"]) for row in events}
    if event_revision_ids != set(revision_by_id):
        failures.append("accepted_truth_revision_event_coverage_mismatch")
    checkpoint_valid = checkpoint is not None and (
        int(checkpoint["event_count"]) == len(events)
        and str(checkpoint["head_event_hash"]) == previous_event_hash
        and str(checkpoint["chain_hash"]) == chain
    )
    if not checkpoint_valid:
        failures.append("accepted_truth_integrity_checkpoint_mismatch")

    stored_heads = {str(row["event_id"]): str(row["revision_id"]) for row in heads}
    if stored_heads != replay_heads:
        failures.append("accepted_truth_materialized_head_mismatch")
    unique_failures = tuple(dict.fromkeys(failures))
    return AcceptedTruthLedgerVerification(
        revisions=len(revisions),
        events=len(events),
        head_event_hash=previous_event_hash,
        chain_hash=chain,
        valid=not unique_failures,
        failures=unique_failures,
    )


def build_accepted_truth_snapshot(
    path: str | Path,
    *,
    event_ids: frozenset[str] | None = None,
    as_of_sequence: int | None = None,
) -> AcceptedTruthSnapshot:
    verification = verify_accepted_truth_ledger(path)
    if not verification.valid:
        raise RuntimeError("accepted truth ledger invalid: " + ",".join(verification.failures))
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        maximum_row = db.execute(
            "SELECT COALESCE(MAX(sequence),0) AS maximum FROM accepted_label_events"
        ).fetchone()
        maximum = int(maximum_row["maximum"])
        sequence = maximum if as_of_sequence is None else as_of_sequence
        if sequence < 0 or sequence > maximum:
            raise ValueError("as_of_sequence is outside the accepted truth ledger")
        rows = db.execute(
            "SELECT * FROM accepted_label_events WHERE sequence<=? ORDER BY sequence",
            (sequence,),
        ).fetchall()
        revisions = {
            str(row["revision_id"]): _decode_revision(row)
            for row in db.execute("SELECT * FROM accepted_label_revisions")
        }

    selected_heads: dict[str, str] = {}
    prefix_head = _GENESIS
    prefix_chain = _GENESIS
    for row in rows:
        event_id = str(row["event_id"])
        selected_heads[event_id] = str(row["revision_id"])
        prefix_head = str(row["event_hash"])
        prefix_chain = hashlib.sha256(f"{prefix_chain}|{prefix_head}".encode()).hexdigest()
    requested = event_ids if event_ids is not None else frozenset(selected_heads)
    missing = sorted(requested - set(selected_heads))
    if missing:
        raise ValueError(f"accepted truth snapshot missing requested events: {missing}")
    bindings = tuple(
        AcceptedTruthBinding(
            event_id=event_id,
            revision_id=revisions[selected_heads[event_id]].revision_id,
            revision_number=revisions[selected_heads[event_id]].revision_number,
            source_revision_id=revisions[selected_heads[event_id]].source_revision_id,
            label=revisions[selected_heads[event_id]].label,
            confidence=revisions[selected_heads[event_id]].confidence,
        )
        for event_id in sorted(requested)
    )
    snapshot_hash = _hash(
        {
            "version": "accepted-truth-snapshot-v1",
            "as_of_sequence": sequence,
            "ledger_head_event_hash": prefix_head,
            "ledger_chain_hash": prefix_chain,
            "bindings": [
                {
                    "event_id": item.event_id,
                    "revision_id": item.revision_id,
                    "revision_number": item.revision_number,
                    "source_revision_id": item.source_revision_id,
                    "label": item.label.value,
                    "confidence": round(item.confidence, 12),
                }
                for item in bindings
            ],
        }
    )
    return AcceptedTruthSnapshot(
        as_of_sequence=sequence,
        ledger_head_event_hash=prefix_head,
        ledger_chain_hash=prefix_chain,
        bindings=bindings,
        snapshot_hash=snapshot_hash,
    )


def require_truth_binding(
    snapshot: AcceptedTruthSnapshot,
    *,
    event_id: str,
    label: EventKind,
    revision_id: str | None = None,
    source_revision_id: str | None = None,
    minimum_confidence: float = 0.0,
) -> AcceptedTruthBinding:
    binding = snapshot.by_event.get(event_id)
    if binding is None:
        raise ValueError(f"event {event_id} is absent from accepted truth snapshot")
    if binding.label is not label:
        raise ValueError(f"accepted truth label mismatch for event {event_id}")
    if revision_id is not None and binding.revision_id != revision_id:
        raise ValueError(f"accepted truth revision mismatch for event {event_id}")
    if source_revision_id is not None and binding.source_revision_id != source_revision_id:
        raise ValueError(f"accepted source revision mismatch for event {event_id}")
    if binding.confidence < minimum_confidence:
        raise ValueError(f"accepted truth confidence below threshold for event {event_id}")
    return binding
