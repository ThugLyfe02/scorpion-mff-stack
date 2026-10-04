from __future__ import annotations

import contextlib
import datetime as dt
import decimal
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from .domain import BookState, EventKind, SignalEvent
from .reducer import apply_fill
from .replay import replay

_GENESIS = "0" * 64
_ACTION_EFFECTS = frozenset(
    {"PROPOSE_OPEN", "PROPOSE_ADD", "PROPOSE_TRIM", "PROPOSE_CLOSE"}
)
_BLOCKED = frozenset({"BLOCKED_STRATEGY", "BLOCKED_SYSTEM"})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_fill_ledger (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    contract_key TEXT NOT NULL,
    generation INTEGER NOT NULL,
    quantity_delta INTEGER NOT NULL,
    fill_price TEXT NOT NULL,
    final INTEGER NOT NULL,
    source TEXT NOT NULL,
    external_ref TEXT,
    recorded_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    filled_ts_utc TEXT NOT NULL,
    recorded_ts_utc TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE
)
"""
_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS idx_execution_fill_event
    ON execution_fill_ledger(event_id,sequence)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_execution_fill_contract
    ON execution_fill_ledger(contract_key,filled_ts_utc)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_fill_external_ref
    ON execution_fill_ledger(source,external_ref)
    WHERE external_ref IS NOT NULL AND external_ref <> ''
    """,
)


class FillSource(StrEnum):
    PAPER = "PAPER"
    EXTERNAL_CONFIRMATION = "EXTERNAL_CONFIRMATION"
    MANUAL_RECONCILIATION = "MANUAL_RECONCILIATION"


@dataclass(frozen=True, slots=True)
class ExecutionFill:
    fill_id: str
    event_id: str
    contract_key: str
    generation: int
    quantity_delta: int
    fill_price: Decimal
    final: bool
    source: FillSource
    external_ref: str | None
    recorded_by: str
    note: str
    filled_ts_utc: datetime
    recorded_ts_utc: datetime

    def __post_init__(self) -> None:
        if not self.fill_id or not self.event_id or not self.contract_key:
            raise ValueError("fill_id, event_id, and contract_key are required")
        if self.generation <= 0:
            raise ValueError("generation must be positive")
        if self.quantity_delta == 0:
            raise ValueError("quantity_delta must be non-zero")
        if self.fill_price <= 0:
            raise ValueError("fill_price must be positive")
        if not self.recorded_by.strip():
            raise ValueError("recorded_by is required")
        for field_name in ("filled_ts_utc", "recorded_ts_utc"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
            object.__setattr__(self, field_name, value.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class ExecutionFillRecord:
    sequence: int
    fill: ExecutionFill
    payload_sha256: str
    previous_hash: str
    record_hash: str


@dataclass(frozen=True, slots=True)
class ExecutionJournalVerification:
    ok: bool
    checked: int
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionTruthSnapshot:
    state: BookState | None
    fills_applied: int
    verification: ExecutionJournalVerification
    anomalies: tuple[str, ...]

    @property
    def available(self) -> bool:
        return self.state is not None and self.verification.ok and not self.anomalies


@dataclass(frozen=True, slots=True)
class _EffectAdmission:
    kind: str
    contract_key: str
    generation: int
    status: str
    quantity_hint: int | None
    received_ts_utc: datetime


def ensure_execution_journal_schema(db: sqlite3.Connection) -> None:
    db.execute(_SCHEMA)
    for statement in _INDEXES:
        db.execute(statement)


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _payload(fill: ExecutionFill) -> dict[str, str | int | bool | None]:
    return {
        "fill_id": fill.fill_id,
        "event_id": fill.event_id,
        "contract_key": fill.contract_key,
        "generation": fill.generation,
        "quantity_delta": fill.quantity_delta,
        "fill_price": str(fill.fill_price),
        "final": fill.final,
        "source": fill.source.value,
        "external_ref": fill.external_ref,
        "recorded_by": fill.recorded_by,
        "note": fill.note,
        "filled_ts_utc": fill.filled_ts_utc.isoformat(),
        "recorded_ts_utc": fill.recorded_ts_utc.isoformat(),
    }


def _canonical_payload(fill: ExecutionFill) -> str:
    return json.dumps(_payload(fill), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _payload_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _record_hash(
    sequence: int,
    fill_id: str,
    previous_hash: str,
    payload_sha256: str,
) -> str:
    material = f"{sequence}|{fill_id}|{previous_hash}|{payload_sha256}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_to_fill(row: sqlite3.Row) -> ExecutionFill:
    return ExecutionFill(
        fill_id=str(row["fill_id"]),
        event_id=str(row["event_id"]),
        contract_key=str(row["contract_key"]),
        generation=int(row["generation"]),
        quantity_delta=int(row["quantity_delta"]),
        fill_price=Decimal(str(row["fill_price"])),
        final=bool(row["final"]),
        source=FillSource(str(row["source"])),
        external_ref=str(row["external_ref"]) if row["external_ref"] else None,
        recorded_by=str(row["recorded_by"]),
        note=str(row["note"]),
        filled_ts_utc=datetime.fromisoformat(str(row["filled_ts_utc"])).astimezone(UTC),
        recorded_ts_utc=datetime.fromisoformat(str(row["recorded_ts_utc"])).astimezone(UTC),
    )


def _rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    ensure_execution_journal_schema(db)
    return db.execute(
        """
        SELECT sequence,fill_id,event_id,contract_key,generation,quantity_delta,fill_price,
               final,source,external_ref,recorded_by,note,filled_ts_utc,recorded_ts_utc,
               payload_json,payload_sha256,previous_hash,record_hash
        FROM execution_fill_ledger ORDER BY sequence
        """
    ).fetchall()


def _verify_rows(rows: list[sqlite3.Row]) -> ExecutionJournalVerification:
    failures: list[str] = []
    previous = _GENESIS
    for expected_sequence, row in enumerate(rows, start=1):
        sequence = int(row["sequence"])
        fill_id = str(row["fill_id"])
        payload_json = str(row["payload_json"])
        payload_sha256 = str(row["payload_sha256"])
        if sequence != expected_sequence:
            failures.append(f"sequence_gap:{expected_sequence}:{sequence}")
        try:
            fill = _row_to_fill(row)
        except (ValueError, decimal.InvalidOperation) as exc:
            failures.append(f"invalid_fill_row:{fill_id}:{type(exc).__name__}")
            previous = str(row["record_hash"])
            continue
        if _canonical_payload(fill) != payload_json:
            failures.append(f"column_payload_mismatch:{fill_id}")
        if _payload_hash(payload_json) != payload_sha256:
            failures.append(f"payload_hash_mismatch:{fill_id}")
        if str(row["previous_hash"]) != previous:
            failures.append(f"previous_hash_mismatch:{fill_id}")
        expected_hash = _record_hash(sequence, fill_id, previous, payload_sha256)
        if str(row["record_hash"]) != expected_hash:
            failures.append(f"record_hash_mismatch:{fill_id}")
        previous = str(row["record_hash"])
    return ExecutionJournalVerification(not failures, len(rows), tuple(failures))


def _decode_signal(payload_json: str) -> SignalEvent:
    raw: Any = json.loads(payload_json)
    if not isinstance(raw, dict):
        raise ValueError("signal payload must be an object")
    payload: dict[str, Any] = dict(raw)
    payload["kind"] = EventKind(str(payload["kind"]))
    payload["source_ts_utc"] = dt.datetime.fromisoformat(
        str(payload["source_ts_utc"])
    ).astimezone(UTC)
    payload["received_ts_utc"] = dt.datetime.fromisoformat(
        str(payload["received_ts_utc"])
    ).astimezone(UTC)
    if payload.get("expiry"):
        payload["expiry"] = dt.date.fromisoformat(str(payload["expiry"]))
    for key in ("strike", "referenced_price", "referenced_pct"):
        if payload.get(key) is not None:
            payload[key] = Decimal(str(payload[key]))
    return SignalEvent(**payload)


def _admitted_state(db: sqlite3.Connection) -> BookState:
    signal_rows = db.execute(
        "SELECT event_id,payload_json FROM signal_events "
        "ORDER BY source_ts_utc,received_ts_utc,event_id"
    ).fetchall()
    blocked: set[str] = set()
    if _table_exists(db, "operator_decision_packets"):
        blocked = {
            str(row[0])
            for row in db.execute(
                "SELECT event_id FROM operator_decision_packets "
                "WHERE disposition IN ('BLOCKED_STRATEGY','BLOCKED_SYSTEM')"
            ).fetchall()
        }
    events = [
        _decode_signal(str(row["payload_json"]))
        for row in signal_rows
        if str(row["event_id"]) not in blocked
    ]
    state, _ = replay(events)
    return state


def _effect_admission(db: sqlite3.Connection, event_id: str) -> _EffectAdmission:
    row = db.execute(
        """
        SELECT p.kind,p.contract_key,p.generation,p.status,p.quantity_hint,s.received_ts_utc
        FROM proposed_effects p
        JOIN signal_events s ON s.event_id=p.source_event_id
        WHERE p.source_event_id=? AND p.kind IN
              ('PROPOSE_OPEN','PROPOSE_ADD','PROPOSE_TRIM','PROPOSE_CLOSE')
        ORDER BY p.effect_id
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        raise ValueError("event has no actionable proposed effect")
    status = str(row["status"])
    if status in _BLOCKED:
        raise ValueError(f"effect is blocked: {status}")
    if _table_exists(db, "operator_decision_packets"):
        packet = db.execute(
            "SELECT disposition FROM operator_decision_packets WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if packet is not None and str(packet[0]) in _BLOCKED:
            raise ValueError(f"decision packet is blocked: {packet[0]}")
    contract_key = str(row["contract_key"] or "")
    if not contract_key:
        raise ValueError("actionable effect has no contract")
    return _EffectAdmission(
        kind=str(row["kind"]),
        contract_key=contract_key,
        generation=int(row["generation"]),
        status=status,
        quantity_hint=(int(row["quantity_hint"]) if row["quantity_hint"] is not None else None),
        received_ts_utc=datetime.fromisoformat(str(row["received_ts_utc"])).astimezone(UTC),
    )


def _validate_fill(
    state: BookState,
    fill: ExecutionFill,
    admission: _EffectAdmission,
) -> None:
    if fill.contract_key != admission.contract_key:
        raise ValueError("fill contract does not match proposed effect")
    if fill.generation != admission.generation:
        raise ValueError("fill generation does not match proposed effect")
    if fill.filled_ts_utc < admission.received_ts_utc:
        raise ValueError("fill timestamp precedes signal receipt")
    position = state.positions.get(fill.contract_key)
    if position is None or position.generation != fill.generation:
        raise ValueError("fill has no matching admitted position generation")

    if admission.kind in {"PROPOSE_OPEN", "PROPOSE_ADD"}:
        if fill.quantity_delta <= 0:
            raise ValueError("open/add fill must increase quantity")
        if fill.final:
            raise ValueError("open/add fill cannot be final")
        if (
            admission.kind == "PROPOSE_OPEN"
            and admission.quantity_hint is not None
            and position.quantity + fill.quantity_delta > admission.quantity_hint
        ):
            raise ValueError("open fill exceeds deterministic quantity hint")
        if admission.kind == "PROPOSE_ADD" and position.quantity <= 0:
            raise ValueError("add fill requires an existing executed position")
    elif admission.kind in {"PROPOSE_TRIM", "PROPOSE_CLOSE"}:
        if fill.quantity_delta >= 0:
            raise ValueError("trim/close fill must reduce quantity")
        if position.quantity <= 0:
            raise ValueError("trim/close fill requires an executed position")
        if -fill.quantity_delta > position.quantity:
            raise ValueError("fill would reduce more quantity than is held")
        if fill.final:
            if admission.kind != "PROPOSE_CLOSE":
                raise ValueError("only close fills may be final")
            if -fill.quantity_delta != position.quantity:
                raise ValueError("final close must consume the entire held quantity")
    else:
        raise ValueError(f"unsupported effect kind: {admission.kind}")


def _apply_records(
    db: sqlite3.Connection,
    base_state: BookState,
    records: list[ExecutionFillRecord],
) -> tuple[BookState | None, tuple[str, ...]]:
    state = base_state
    anomalies: list[str] = []
    ordered = sorted(records, key=lambda item: (item.fill.filled_ts_utc, item.sequence))
    for record in ordered:
        fill = record.fill
        try:
            admission = _effect_admission(db, fill.event_id)
            _validate_fill(state, fill, admission)
            state = apply_fill(
                state,
                fill.contract_key,
                fill.generation,
                fill.quantity_delta,
                fill.fill_price,
                final=fill.final,
            )
        except (KeyError, ValueError) as exc:
            anomalies.append(f"{fill.fill_id}:{type(exc).__name__}:{exc}")
            return None, tuple(anomalies)
    return state, tuple(anomalies)


def _records_from_rows(rows: list[sqlite3.Row]) -> list[ExecutionFillRecord]:
    return [
        ExecutionFillRecord(
            sequence=int(row["sequence"]),
            fill=_row_to_fill(row),
            payload_sha256=str(row["payload_sha256"]),
            previous_hash=str(row["previous_hash"]),
            record_hash=str(row["record_hash"]),
        )
        for row in rows
    ]


def reconstruct_execution_state(
    path: str | Path,
    events: list[SignalEvent] | tuple[SignalEvent, ...] | None = None,
) -> ExecutionTruthSnapshot:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        ensure_execution_journal_schema(db)
        rows = _rows(db)
        verification = _verify_rows(rows)
        if not verification.ok:
            return ExecutionTruthSnapshot(None, 0, verification, verification.failures)
        if events is None:
            base_state = _admitted_state(db)
        else:
            blocked: set[str] = set()
            if _table_exists(db, "operator_decision_packets"):
                blocked = {
                    str(row[0])
                    for row in db.execute(
                        "SELECT event_id FROM operator_decision_packets "
                        "WHERE disposition IN ('BLOCKED_STRATEGY','BLOCKED_SYSTEM')"
                    ).fetchall()
                }
            base_state, _ = replay([event for event in events if event.event_id not in blocked])
        records = _records_from_rows(rows)
        state, anomalies = _apply_records(db, base_state, records)
        return ExecutionTruthSnapshot(
            state,
            len(records) if state is not None else 0,
            verification,
            anomalies,
        )
    finally:
        db.close()


class ExecutionJournal:
    """Append-only execution-truth journal.

    It records fills that happened elsewhere (paper or human/broker confirmed). It never submits,
    routes, modifies, or cancels an order. The hash chain detects partial/accidental mutation;
    protection against an attacker rewriting the entire database requires external head anchoring.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        db = sqlite3.connect(self.path)
        try:
            ensure_execution_journal_schema(db)
            db.commit()
        finally:
            db.close()

    def verify(self) -> ExecutionJournalVerification:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            return _verify_rows(_rows(db))
        finally:
            db.close()

    def records(self) -> tuple[ExecutionFillRecord, ...]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            rows = _rows(db)
            verification = _verify_rows(rows)
            if not verification.ok:
                raise ValueError("execution journal integrity verification failed")
            return tuple(_records_from_rows(rows))
        finally:
            db.close()

    def head_hash(self) -> str:
        records = self.records()
        return records[-1].record_hash if records else _GENESIS

    def record(
        self,
        event_id: str,
        *,
        quantity_delta: int,
        fill_price: Decimal,
        source: FillSource,
        recorded_by: str,
        filled_ts_utc: datetime | None = None,
        external_ref: str | None = None,
        note: str = "",
        final: bool = False,
        fill_id: str | None = None,
    ) -> ExecutionFillRecord:
        requested_fill_time = filled_ts_utc or datetime.now(UTC)
        if requested_fill_time.tzinfo is None or requested_fill_time.utcoffset() is None:
            raise ValueError("filled_ts_utc must be timezone-aware")
        filled = requested_fill_time.astimezone(UTC)
        recorded = datetime.now(UTC)
        if source is FillSource.EXTERNAL_CONFIRMATION and not (external_ref or "").strip():
            raise ValueError("external_ref is required for external confirmations")

        db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=5000")
            ensure_execution_journal_schema(db)
            db.execute("BEGIN IMMEDIATE")
            rows = _rows(db)
            verification = _verify_rows(rows)
            if not verification.ok:
                raise ValueError("execution journal integrity verification failed")

            admission = _effect_admission(db, event_id)
            external_key = (external_ref or "").strip() or None
            if external_key is not None:
                existing = db.execute(
                    "SELECT * FROM execution_fill_ledger WHERE source=? AND external_ref=?",
                    (source.value, external_key),
                ).fetchone()
                if existing is not None:
                    existing_fill = _row_to_fill(existing)
                    if (
                        existing_fill.event_id == event_id
                        and existing_fill.quantity_delta == quantity_delta
                        and existing_fill.fill_price == fill_price
                        and existing_fill.final == final
                    ):
                        db.execute("COMMIT")
                        return ExecutionFillRecord(
                            int(existing["sequence"]),
                            existing_fill,
                            str(existing["payload_sha256"]),
                            str(existing["previous_hash"]),
                            str(existing["record_hash"]),
                        )
                    raise ValueError("external_ref already belongs to a different fill")

            actual_fill_id = (fill_id or "").strip()
            if not actual_fill_id:
                stable = (
                    f"{source.value}|{external_key}|{event_id}|{quantity_delta}|"
                    f"{fill_price}|{filled.isoformat()}"
                )
                if external_key is None:
                    stable = f"{stable}|{uuid.uuid4().hex}"
                actual_fill_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()

            candidate = ExecutionFill(
                fill_id=actual_fill_id,
                event_id=event_id,
                contract_key=admission.contract_key,
                generation=admission.generation,
                quantity_delta=quantity_delta,
                fill_price=fill_price,
                final=final,
                source=source,
                external_ref=external_key,
                recorded_by=recorded_by,
                note=note[:1000],
                filled_ts_utc=filled,
                recorded_ts_utc=recorded,
            )

            base_state = _admitted_state(db)
            existing_records = _records_from_rows(rows)
            candidate_sequence = len(rows) + 1
            candidate_record = ExecutionFillRecord(
                candidate_sequence,
                candidate,
                "",
                "",
                "",
            )
            state, anomalies = _apply_records(
                db,
                base_state,
                [*existing_records, candidate_record],
            )
            if state is None:
                raise ValueError(
                    "fill violates execution-state invariants: " + ";".join(anomalies)
                )

            payload_json = _canonical_payload(candidate)
            payload_sha256 = _payload_hash(payload_json)
            previous_hash = (
                str(rows[-1]["record_hash"]) if rows else _GENESIS
            )
            record_hash = _record_hash(
                candidate_sequence,
                candidate.fill_id,
                previous_hash,
                payload_sha256,
            )
            db.execute(
                """
                INSERT INTO execution_fill_ledger
                (fill_id,event_id,contract_key,generation,quantity_delta,fill_price,final,
                 source,external_ref,recorded_by,note,filled_ts_utc,recorded_ts_utc,payload_json,
                 payload_sha256,previous_hash,record_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    candidate.fill_id,
                    candidate.event_id,
                    candidate.contract_key,
                    candidate.generation,
                    candidate.quantity_delta,
                    str(candidate.fill_price),
                    int(candidate.final),
                    candidate.source.value,
                    candidate.external_ref,
                    candidate.recorded_by,
                    candidate.note,
                    candidate.filled_ts_utc.isoformat(),
                    candidate.recorded_ts_utc.isoformat(),
                    payload_json,
                    payload_sha256,
                    previous_hash,
                    record_hash,
                ),
            )
            db.execute("COMMIT")
            return ExecutionFillRecord(
                candidate_sequence,
                candidate,
                payload_sha256,
                previous_hash,
                record_hash,
            )
        except Exception:
            with contextlib.suppress(sqlite3.OperationalError):
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
