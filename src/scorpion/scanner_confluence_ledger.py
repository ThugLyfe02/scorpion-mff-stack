from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from .execution_forensics import CompletedTrade
from .scanner_batch import ScannerBatchRowMetadata, ScannerContextBatch
from .scanner_context import (
    ScannerContextObservation,
    latest_scanner_context_available_before,
    latest_scanner_context_before,
)

_GENESIS_HASH = "0" * 64


class AttributionMode(StrEnum):
    SOURCE_TIME = "SOURCE_TIME"
    OPERATIONAL = "OPERATIONAL"


class ContextBindingStatus(StrEnum):
    BOUND = "BOUND"
    NO_CONTEXT = "NO_CONTEXT"
    STALE_CONTEXT = "STALE_CONTEXT"
    RECEIVE_CLOCK_MISSING = "RECEIVE_CLOCK_MISSING"
    NOT_AVAILABLE_BY_RECEIVE_TIME = "NOT_AVAILABLE_BY_RECEIVE_TIME"


@dataclass(frozen=True, slots=True)
class ConfluenceSnapshotPolicy:
    max_context_age_seconds: float = 1800.0

    def __post_init__(self) -> None:
        if self.max_context_age_seconds <= 0:
            raise ValueError("max_context_age_seconds must be positive")


@dataclass(frozen=True, slots=True)
class FrozenConfluenceSnapshot:
    snapshot_id: str
    authority: str
    execution_authority: bool
    entry_event_id: str
    contract_key: str
    symbol: str
    mode: AttributionMode
    mff_source_ts_utc: datetime
    mff_received_ts_utc: datetime | None
    binding_status: ContextBindingStatus
    binding_reason: str
    observation_id: str | None
    scanner_run_id: str | None
    scanner_observed_at_utc: datetime | None
    scanner_received_at_utc: datetime | None
    context_age_seconds: float | None
    availability_lag_seconds: float | None
    selection_disposition: str | None
    full_universe_comparable: bool
    tape_flag: str | None
    ross_boxes_hit: int | None
    ross_boxes_known: int | None
    rvol: float | None
    rvol_basis: str | None
    scanner_policy_fingerprint: str | None

    def canonical_payload(self) -> dict[str, object]:
        return {
            "authority": self.authority,
            "execution_authority": self.execution_authority,
            "entry_event_id": self.entry_event_id,
            "contract_key": self.contract_key,
            "symbol": self.symbol,
            "mode": self.mode.value,
            "mff_source_ts_utc": self.mff_source_ts_utc.isoformat(),
            "mff_received_ts_utc": (
                self.mff_received_ts_utc.isoformat()
                if self.mff_received_ts_utc is not None
                else None
            ),
            "binding_status": self.binding_status.value,
            "binding_reason": self.binding_reason,
            "observation_id": self.observation_id,
            "scanner_run_id": self.scanner_run_id,
            "scanner_observed_at_utc": (
                self.scanner_observed_at_utc.isoformat()
                if self.scanner_observed_at_utc is not None
                else None
            ),
            "scanner_received_at_utc": (
                self.scanner_received_at_utc.isoformat()
                if self.scanner_received_at_utc is not None
                else None
            ),
            "context_age_seconds": self.context_age_seconds,
            "availability_lag_seconds": self.availability_lag_seconds,
            "selection_disposition": self.selection_disposition,
            "full_universe_comparable": self.full_universe_comparable,
            "tape_flag": self.tape_flag,
            "ross_boxes_hit": self.ross_boxes_hit,
            "ross_boxes_known": self.ross_boxes_known,
            "rvol": self.rvol,
            "rvol_basis": self.rvol_basis,
            "scanner_policy_fingerprint": self.scanner_policy_fingerprint,
        }

    def verify_snapshot_id(self) -> bool:
        return self.snapshot_id == _hash(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ConfluenceOutcome:
    outcome_id: str
    snapshot_id: str
    entry_event_id: str
    contract_key: str
    closed_ts_utc: datetime
    return_fraction: str
    pnl: str
    holding_seconds: float
    depth_evidence_complete: bool

    def canonical_payload(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "entry_event_id": self.entry_event_id,
            "contract_key": self.contract_key,
            "closed_ts_utc": self.closed_ts_utc.isoformat(),
            "return_fraction": self.return_fraction,
            "pnl": self.pnl,
            "holding_seconds": self.holding_seconds,
            "depth_evidence_complete": self.depth_evidence_complete,
        }

    def verify_outcome_id(self) -> bool:
        return self.outcome_id == _hash(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ConfluenceLedgerVerification:
    snapshots: int
    bound_snapshots: int
    explicit_missing_snapshots: int
    outcomes: int
    completed_fraction: float
    chain_head: str
    chain_valid: bool
    rows_valid: bool


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _ensure_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _symbol(contract_key: str) -> str:
    symbol = contract_key.split("|", 1)[0].strip().upper()
    if not symbol:
        raise ValueError("contract_key must contain an underlying symbol")
    return symbol


def _observation_index(
    batches: tuple[ScannerContextBatch, ...],
) -> tuple[
    tuple[ScannerContextObservation, ...],
    dict[str, ScannerBatchRowMetadata],
]:
    observations: dict[str, ScannerContextObservation] = {}
    comparable_metadata: dict[str, ScannerBatchRowMetadata] = {}
    for batch in batches:
        for observation in batch.observations:
            prior = observations.get(observation.observation_id)
            if prior is not None and prior != observation:
                raise ValueError("conflicting scanner observation identity across batches")
            observations[observation.observation_id] = observation
        if batch.absence_is_interpretable:
            for metadata in batch.row_metadata:
                prior_meta = comparable_metadata.get(metadata.observation_id)
                if prior_meta is not None and prior_meta != metadata:
                    raise ValueError("conflicting scanner row metadata across batches")
                comparable_metadata[metadata.observation_id] = metadata
    return tuple(observations.values()), comparable_metadata


def _candidate_before(
    observations: tuple[ScannerContextObservation, ...],
    *,
    symbol: str,
    source_ts_utc: datetime,
    received_ts_utc: datetime | None,
    mode: AttributionMode,
) -> ScannerContextObservation | None:
    if mode is AttributionMode.SOURCE_TIME:
        return latest_scanner_context_before(
            observations,
            symbol=symbol,
            source_ts_utc=source_ts_utc,
            require_causal_safe=True,
        )
    if received_ts_utc is None:
        return None
    return latest_scanner_context_available_before(
        observations,
        symbol=symbol,
        source_ts_utc=source_ts_utc,
        received_ts_utc=received_ts_utc,
        require_causal_safe=True,
    )


def _source_time_candidate(
    observations: tuple[ScannerContextObservation, ...],
    *,
    symbol: str,
    source_ts_utc: datetime,
) -> ScannerContextObservation | None:
    return latest_scanner_context_before(
        observations,
        symbol=symbol,
        source_ts_utc=source_ts_utc,
        require_causal_safe=True,
    )


def freeze_confluence_snapshot(
    *,
    entry_event_id: str,
    contract_key: str,
    mff_source_ts_utc: datetime,
    mff_received_ts_utc: datetime | None,
    batches: tuple[ScannerContextBatch, ...],
    mode: AttributionMode,
    policy: ConfluenceSnapshotPolicy | None = None,
) -> FrozenConfluenceSnapshot:
    """Freeze outcome-blind scanner context for one MFF entry.

    The function always returns a snapshot, including explicit missing-context
    states, so later analysis cannot condition only on trades where scanner
    context happened to exist.
    """

    if not entry_event_id.strip():
        raise ValueError("entry_event_id is required")
    source_ts = _ensure_aware(mff_source_ts_utc, "mff_source_ts_utc")
    received_ts = (
        _ensure_aware(mff_received_ts_utc, "mff_received_ts_utc")
        if mff_received_ts_utc is not None
        else None
    )
    if received_ts is not None and received_ts < source_ts:
        raise ValueError("mff_received_ts_utc cannot precede source time")
    policy = policy or ConfluenceSnapshotPolicy()
    symbol = _symbol(contract_key)
    observations, comparable_metadata = _observation_index(batches)

    source_candidate = _source_time_candidate(
        observations,
        symbol=symbol,
        source_ts_utc=source_ts,
    )
    candidate = _candidate_before(
        observations,
        symbol=symbol,
        source_ts_utc=source_ts,
        received_ts_utc=received_ts,
        mode=mode,
    )

    status: ContextBindingStatus
    reason: str
    if mode is AttributionMode.OPERATIONAL and received_ts is None:
        status = ContextBindingStatus.RECEIVE_CLOCK_MISSING
        reason = "operational attribution requires MFF receive clock"
    elif candidate is None and source_candidate is not None and mode is AttributionMode.OPERATIONAL:
        status = ContextBindingStatus.NOT_AVAILABLE_BY_RECEIVE_TIME
        reason = "causal source-time context existed but had not arrived by MFF receive time"
    elif candidate is None:
        status = ContextBindingStatus.NO_CONTEXT
        reason = "no causal-safe scanner context existed before MFF source time"
    else:
        age = (source_ts - candidate.observed_at_utc).total_seconds()
        if age < 0:
            raise AssertionError("context selector returned future scanner observation")
        if age > policy.max_context_age_seconds:
            status = ContextBindingStatus.STALE_CONTEXT
            reason = (
                f"context age {age:.3f}s exceeds "
                f"{policy.max_context_age_seconds:.3f}s freshness limit"
            )
        else:
            status = ContextBindingStatus.BOUND
            reason = "causal-safe scanner context frozen before outcome"

    bound = candidate if status is ContextBindingStatus.BOUND else None
    metadata = (
        comparable_metadata.get(bound.observation_id)
        if bound is not None
        else None
    )
    disposition = metadata.selection_disposition if metadata is not None else None
    full_universe = disposition in {"SELECTED", "NOT_SELECTED"}

    age_seconds = (
        (source_ts - bound.observed_at_utc).total_seconds()
        if bound is not None
        else None
    )
    availability_lag = (
        (bound.received_at_utc - bound.observed_at_utc).total_seconds()
        if bound is not None and bound.received_at_utc is not None
        else None
    )

    provisional = FrozenConfluenceSnapshot(
        snapshot_id="0" * 64,
        authority="RESEARCH_ONLY",
        execution_authority=False,
        entry_event_id=entry_event_id.strip(),
        contract_key=contract_key.strip(),
        symbol=symbol,
        mode=mode,
        mff_source_ts_utc=source_ts,
        mff_received_ts_utc=received_ts,
        binding_status=status,
        binding_reason=reason,
        observation_id=(bound.observation_id if bound is not None else None),
        scanner_run_id=(bound.run_id if bound is not None else None),
        scanner_observed_at_utc=(bound.observed_at_utc if bound is not None else None),
        scanner_received_at_utc=(bound.received_at_utc if bound is not None else None),
        context_age_seconds=age_seconds,
        availability_lag_seconds=availability_lag,
        selection_disposition=disposition,
        full_universe_comparable=full_universe,
        tape_flag=(bound.tape_flag if bound is not None else None),
        ross_boxes_hit=(bound.ross_boxes_hit if bound is not None else None),
        ross_boxes_known=(bound.ross_boxes_known if bound is not None else None),
        rvol=(bound.rvol if bound is not None else None),
        rvol_basis=(bound.rvol_basis if bound is not None else None),
        scanner_policy_fingerprint=(
            bound.policy_fingerprint if bound is not None else None
        ),
    )
    return FrozenConfluenceSnapshot(
        **{
            **asdict(provisional),
            "snapshot_id": _hash(provisional.canonical_payload()),
        }
    )


def outcome_from_completed_trade(
    snapshot: FrozenConfluenceSnapshot,
    trade: CompletedTrade,
) -> ConfluenceOutcome:
    if not snapshot.verify_snapshot_id():
        raise ValueError("snapshot identity verification failed")
    if trade.entry_event_id != snapshot.entry_event_id:
        raise ValueError("completed trade entry_event_id does not match snapshot")
    if trade.contract_key != snapshot.contract_key:
        raise ValueError("completed trade contract_key does not match snapshot")
    closed = _ensure_aware(trade.closed_ts_utc, "closed_ts_utc")
    if closed < snapshot.mff_source_ts_utc:
        raise ValueError("completed trade closes before frozen entry source time")
    provisional = ConfluenceOutcome(
        outcome_id="0" * 64,
        snapshot_id=snapshot.snapshot_id,
        entry_event_id=trade.entry_event_id,
        contract_key=trade.contract_key,
        closed_ts_utc=closed,
        return_fraction=str(trade.return_fraction),
        pnl=str(trade.pnl),
        holding_seconds=trade.holding_seconds,
        depth_evidence_complete=trade.depth_evidence_complete,
    )
    return ConfluenceOutcome(
        **{
            **asdict(provisional),
            "outcome_id": _hash(provisional.canonical_payload()),
        }
    )


def _connect(path: Path | str) -> sqlite3.Connection:
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=5000")
    _initialize(connection)
    return connection


def _initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS confluence_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            entry_event_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(entry_event_id, mode)
        );

        CREATE TABLE IF NOT EXISTS confluence_outcomes (
            outcome_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES confluence_snapshots(snapshot_id)
        );

        CREATE TABLE IF NOT EXISTS confluence_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_kind TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL UNIQUE,
            created_at_utc TEXT NOT NULL
        );

        CREATE TRIGGER IF NOT EXISTS confluence_snapshots_no_update
        BEFORE UPDATE ON confluence_snapshots BEGIN
            SELECT RAISE(ABORT, 'confluence snapshots are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS confluence_snapshots_no_delete
        BEFORE DELETE ON confluence_snapshots BEGIN
            SELECT RAISE(ABORT, 'confluence snapshots are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS confluence_outcomes_no_update
        BEFORE UPDATE ON confluence_outcomes BEGIN
            SELECT RAISE(ABORT, 'confluence outcomes are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS confluence_outcomes_no_delete
        BEFORE DELETE ON confluence_outcomes BEGIN
            SELECT RAISE(ABORT, 'confluence outcomes are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS confluence_events_no_update
        BEFORE UPDATE ON confluence_events BEGIN
            SELECT RAISE(ABORT, 'confluence events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS confluence_events_no_delete
        BEFORE DELETE ON confluence_events BEGIN
            SELECT RAISE(ABORT, 'confluence events are append-only');
        END;
        """
    )


def _chain_head(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT event_hash FROM confluence_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    return str(row["event_hash"]) if row is not None else _GENESIS_HASH


def _append_event(
    connection: sqlite3.Connection,
    *,
    event_kind: str,
    entity_id: str,
    payload: dict[str, object],
    created_at_utc: datetime,
) -> str:
    previous = _chain_head(connection)
    material = {
        "event_kind": event_kind,
        "entity_id": entity_id,
        "payload": payload,
        "previous_hash": previous,
        "created_at_utc": created_at_utc.isoformat(),
    }
    event_hash = _hash(material)
    connection.execute(
        """
        INSERT INTO confluence_events(
            event_kind, entity_id, payload_json, previous_hash, event_hash, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event_kind,
            entity_id,
            _canonical_json(payload),
            previous,
            event_hash,
            created_at_utc.isoformat(),
        ),
    )
    return event_hash


def persist_confluence_snapshot(
    path: Path | str,
    snapshot: FrozenConfluenceSnapshot,
    *,
    recorded_at_utc: datetime | None = None,
) -> FrozenConfluenceSnapshot:
    if not snapshot.verify_snapshot_id():
        raise ValueError("snapshot identity verification failed")
    recorded = _ensure_aware(recorded_at_utc or datetime.now(UTC), "recorded_at_utc")
    payload = snapshot.canonical_payload()
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT snapshot_id, payload_json FROM confluence_snapshots "
            "WHERE entry_event_id=? AND mode=?",
            (snapshot.entry_event_id, snapshot.mode.value),
        ).fetchone()
        if existing is not None:
            if str(existing["snapshot_id"]) != snapshot.snapshot_id:
                raise ValueError(
                    "entry/mode snapshot already frozen with different context"
                )
            if str(existing["payload_json"]) != _canonical_json(payload):
                raise ValueError("stored confluence snapshot payload mismatch")
            connection.commit()
            return snapshot
        connection.execute(
            "INSERT INTO confluence_snapshots VALUES (?, ?, ?, ?)",
            (
                snapshot.snapshot_id,
                snapshot.entry_event_id,
                snapshot.mode.value,
                _canonical_json(payload),
            ),
        )
        _append_event(
            connection,
            event_kind="ENTRY_SNAPSHOT",
            entity_id=snapshot.snapshot_id,
            payload=payload,
            created_at_utc=recorded,
        )
        connection.commit()
    return snapshot


def attach_confluence_outcome(
    path: Path | str,
    snapshot: FrozenConfluenceSnapshot,
    trade: CompletedTrade,
    *,
    recorded_at_utc: datetime | None = None,
) -> ConfluenceOutcome:
    outcome = outcome_from_completed_trade(snapshot, trade)
    recorded = _ensure_aware(recorded_at_utc or datetime.now(UTC), "recorded_at_utc")
    payload = outcome.canonical_payload()
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        stored_snapshot = connection.execute(
            "SELECT snapshot_id FROM confluence_snapshots WHERE snapshot_id=?",
            (snapshot.snapshot_id,),
        ).fetchone()
        if stored_snapshot is None:
            raise ValueError("snapshot must be persisted before outcome attachment")
        existing = connection.execute(
            "SELECT outcome_id, payload_json FROM confluence_outcomes WHERE snapshot_id=?",
            (snapshot.snapshot_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["outcome_id"]) != outcome.outcome_id:
                raise ValueError("snapshot already has a different confluence outcome")
            if str(existing["payload_json"]) != _canonical_json(payload):
                raise ValueError("stored confluence outcome payload mismatch")
            connection.commit()
            return outcome
        connection.execute(
            "INSERT INTO confluence_outcomes VALUES (?, ?, ?)",
            (outcome.outcome_id, snapshot.snapshot_id, _canonical_json(payload)),
        )
        _append_event(
            connection,
            event_kind="OUTCOME_ATTACHED",
            entity_id=outcome.outcome_id,
            payload=payload,
            created_at_utc=recorded,
        )
        connection.commit()
    return outcome


def _snapshot_from_payload(payload: dict[str, Any], snapshot_id: str) -> FrozenConfluenceSnapshot:
    return FrozenConfluenceSnapshot(
        snapshot_id=snapshot_id,
        authority=str(payload["authority"]),
        execution_authority=bool(payload["execution_authority"]),
        entry_event_id=str(payload["entry_event_id"]),
        contract_key=str(payload["contract_key"]),
        symbol=str(payload["symbol"]),
        mode=AttributionMode(str(payload["mode"])),
        mff_source_ts_utc=datetime.fromisoformat(str(payload["mff_source_ts_utc"])),
        mff_received_ts_utc=(
            datetime.fromisoformat(str(payload["mff_received_ts_utc"]))
            if payload["mff_received_ts_utc"] is not None
            else None
        ),
        binding_status=ContextBindingStatus(str(payload["binding_status"])),
        binding_reason=str(payload["binding_reason"]),
        observation_id=(
            str(payload["observation_id"]) if payload["observation_id"] is not None else None
        ),
        scanner_run_id=(
            str(payload["scanner_run_id"]) if payload["scanner_run_id"] is not None else None
        ),
        scanner_observed_at_utc=(
            datetime.fromisoformat(str(payload["scanner_observed_at_utc"]))
            if payload["scanner_observed_at_utc"] is not None
            else None
        ),
        scanner_received_at_utc=(
            datetime.fromisoformat(str(payload["scanner_received_at_utc"]))
            if payload["scanner_received_at_utc"] is not None
            else None
        ),
        context_age_seconds=(
            float(payload["context_age_seconds"])
            if payload["context_age_seconds"] is not None
            else None
        ),
        availability_lag_seconds=(
            float(payload["availability_lag_seconds"])
            if payload["availability_lag_seconds"] is not None
            else None
        ),
        selection_disposition=(
            str(payload["selection_disposition"])
            if payload["selection_disposition"] is not None
            else None
        ),
        full_universe_comparable=bool(payload["full_universe_comparable"]),
        tape_flag=(str(payload["tape_flag"]) if payload["tape_flag"] is not None else None),
        ross_boxes_hit=(
            int(payload["ross_boxes_hit"]) if payload["ross_boxes_hit"] is not None else None
        ),
        ross_boxes_known=(
            int(payload["ross_boxes_known"])
            if payload["ross_boxes_known"] is not None
            else None
        ),
        rvol=float(payload["rvol"]) if payload["rvol"] is not None else None,
        rvol_basis=(
            str(payload["rvol_basis"]) if payload["rvol_basis"] is not None else None
        ),
        scanner_policy_fingerprint=(
            str(payload["scanner_policy_fingerprint"])
            if payload["scanner_policy_fingerprint"] is not None
            else None
        ),
    )


def verify_confluence_ledger(path: Path | str) -> ConfluenceLedgerVerification:
    with _connect(path) as connection:
        snapshot_rows = connection.execute(
            "SELECT snapshot_id, payload_json FROM confluence_snapshots ORDER BY snapshot_id"
        ).fetchall()
        outcome_rows = connection.execute(
            "SELECT outcome_id, snapshot_id, payload_json "
            "FROM confluence_outcomes ORDER BY outcome_id"
        ).fetchall()
        event_rows = connection.execute(
            "SELECT event_kind, entity_id, payload_json, previous_hash, "
            "event_hash, created_at_utc FROM confluence_events ORDER BY sequence"
        ).fetchall()

        rows_valid = True
        bound = 0
        snapshot_ids: set[str] = set()
        for row in snapshot_rows:
            try:
                decoded = json.loads(str(row["payload_json"]))
                if not isinstance(decoded, dict):
                    rows_valid = False
                    break
                snapshot = _snapshot_from_payload(decoded, str(row["snapshot_id"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                rows_valid = False
                break
            if not snapshot.verify_snapshot_id():
                rows_valid = False
                break
            if snapshot.authority != "RESEARCH_ONLY" or snapshot.execution_authority:
                rows_valid = False
                break
            if snapshot.binding_status is ContextBindingStatus.BOUND:
                bound += 1
            snapshot_ids.add(snapshot.snapshot_id)

        if rows_valid:
            for row in outcome_rows:
                try:
                    decoded = json.loads(str(row["payload_json"]))
                    if not isinstance(decoded, dict):
                        rows_valid = False
                        break
                    canonical = dict(decoded)
                    outcome_id = str(row["outcome_id"])
                    if _hash(canonical) != outcome_id:
                        rows_valid = False
                        break
                    if str(row["snapshot_id"]) not in snapshot_ids:
                        rows_valid = False
                        break
                    if canonical.get("snapshot_id") != str(row["snapshot_id"]):
                        rows_valid = False
                        break
                except (TypeError, ValueError, json.JSONDecodeError):
                    rows_valid = False
                    break

        previous = _GENESIS_HASH
        chain_valid = True
        for row in event_rows:
            if str(row["previous_hash"]) != previous:
                chain_valid = False
                break
            try:
                decoded = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                chain_valid = False
                break
            material = {
                "event_kind": str(row["event_kind"]),
                "entity_id": str(row["entity_id"]),
                "payload": decoded,
                "previous_hash": previous,
                "created_at_utc": str(row["created_at_utc"]),
            }
            expected = _hash(material)
            if expected != str(row["event_hash"]):
                chain_valid = False
                break
            previous = expected

        snapshots = len(snapshot_rows)
        outcomes = len(outcome_rows)
        return ConfluenceLedgerVerification(
            snapshots=snapshots,
            bound_snapshots=bound,
            explicit_missing_snapshots=snapshots - bound,
            outcomes=outcomes,
            completed_fraction=(outcomes / snapshots if snapshots else 0.0),
            chain_head=previous,
            chain_valid=chain_valid,
            rows_valid=rows_valid,
        )
