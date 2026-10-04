from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .domain import BookState
from .execution_state import replay_admitted_events
from .invariants import assert_valid_book
from .policy_bundle import RuntimePolicyBundle
from .processing_order import load_signals_in_processing_order
from .replay import ReplayOrder, replay, state_fingerprint
from .store import Store

Scalar = str | int | float | bool | None
_GENESIS = "0" * 64
_INTEGRITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS integrity_ledger (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL UNIQUE,
    previous_hash TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    record_hash TEXT NOT NULL UNIQUE,
    created_ts_utc TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class IntegrityRecord:
    sequence: int
    record_id: str
    previous_hash: str
    payload_sha256: str
    record_hash: str


@dataclass(frozen=True, slots=True)
class IntegrityVerification:
    ok: bool
    checked: int
    failure_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class DatabaseEvidenceVerification:
    ok: bool
    checked: int
    legacy_uncovered_signals: int
    failures: tuple[str, ...]


def canonical_payload(payload: Mapping[str, Scalar]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _payload_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def effect_payload_digest(effects: Sequence[Mapping[str, object]]) -> str:
    """Hash the complete ordered effect payload used by the execution-review boundary."""
    payload = json.dumps(
        [dict(effect) for effect in effects],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_record_hash(
    sequence: int,
    record_id: str,
    previous_hash: str,
    payload_sha256: str,
) -> str:
    material = f"{sequence}|{record_id}|{previous_hash}|{payload_sha256}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_chain(records: Sequence[tuple[str, Mapping[str, Scalar]]]) -> tuple[IntegrityRecord, ...]:
    chain: list[IntegrityRecord] = []
    previous = _GENESIS
    for sequence, (record_id, payload) in enumerate(records, start=1):
        payload_sha256 = _payload_hash(canonical_payload(payload))
        record_hash = compute_record_hash(sequence, record_id, previous, payload_sha256)
        chain.append(
            IntegrityRecord(sequence, record_id, previous, payload_sha256, record_hash)
        )
        previous = record_hash
    return tuple(chain)


def verify_chain(records: Sequence[IntegrityRecord]) -> IntegrityVerification:
    previous = _GENESIS
    for expected_sequence, record in enumerate(records, start=1):
        expected_hash = compute_record_hash(
            expected_sequence,
            record.record_id,
            previous,
            record.payload_sha256,
        )
        if (
            record.sequence != expected_sequence
            or record.previous_hash != previous
            or record.record_hash != expected_hash
        ):
            return IntegrityVerification(False, expected_sequence - 1, expected_sequence)
        previous = record.record_hash
    return IntegrityVerification(True, len(records))


def ensure_integrity_schema(db: sqlite3.Connection) -> None:
    db.execute(_INTEGRITY_SCHEMA)
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(integrity_ledger)")}
    if "payload_json" not in columns:
        db.execute(
            "ALTER TABLE integrity_ledger "
            "ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'"
        )


def append_integrity_record(
    db: sqlite3.Connection,
    record_id: str,
    payload: Mapping[str, Scalar],
    *,
    created_ts_utc: str | None = None,
) -> IntegrityRecord:
    ensure_integrity_schema(db)
    payload_json = canonical_payload(payload)
    payload_sha256 = _payload_hash(payload_json)
    row = db.execute(
        "SELECT sequence,record_hash FROM integrity_ledger ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(row["sequence"]) + 1 if row else 1
    previous_hash = str(row["record_hash"]) if row else _GENESIS
    record_hash = compute_record_hash(sequence, record_id, previous_hash, payload_sha256)
    db.execute(
        """
        INSERT INTO integrity_ledger
        (sequence,record_id,previous_hash,payload_sha256,payload_json,record_hash,created_ts_utc)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            sequence,
            record_id,
            previous_hash,
            payload_sha256,
            payload_json,
            record_hash,
            created_ts_utc or datetime.now(UTC).isoformat(),
        ),
    )
    return IntegrityRecord(sequence, record_id, previous_hash, payload_sha256, record_hash)


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _packet_bound_fields(
    packet_payload_json: str,
    ledger_payload: dict[str, object],
    *,
    event_id: str,
    failures: list[str],
) -> dict[str, Scalar] | None:
    bound_names = {"strategy_bucket", "eligibility_reason", "policy_fingerprint"}
    if not bound_names.intersection(ledger_payload):
        return {}
    try:
        packet_payload = json.loads(packet_payload_json)
    except json.JSONDecodeError:
        failures.append(f"decision_packet_json_invalid:{event_id}")
        return None
    if not isinstance(packet_payload, dict):
        failures.append(f"decision_packet_payload_not_object:{event_id}")
        return None
    result: dict[str, Scalar] = {}
    if "strategy_bucket" in ledger_payload:
        result["strategy_bucket"] = str(packet_payload.get("strategy_bucket", "UNKNOWN"))
    if "eligibility_reason" in ledger_payload:
        result["eligibility_reason"] = str(packet_payload.get("eligibility_reason", ""))
    if "policy_fingerprint" in ledger_payload:
        result["policy_fingerprint"] = str(packet_payload.get("policy_fingerprint", ""))
    return result


def _process_seq_bound_field(
    db: sqlite3.Connection,
    payload: dict[str, object],
    *,
    event_id: str,
    failures: list[str],
) -> dict[str, Scalar] | None:
    if "process_seq" not in payload:
        return {}
    if not _table_exists(db, "event_processing_order"):
        failures.append(f"missing_processing_order_table:{event_id}")
        return None
    row = db.execute(
        "SELECT process_seq FROM event_processing_order WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if row is None:
        failures.append(f"missing_processing_order:{event_id}")
        return None
    return {"process_seq": int(row["process_seq"])}


def _effect_rows_digest(
    effects: Sequence[sqlite3.Row],
    *,
    event_id: str,
    failures: list[str],
) -> str | None:
    payloads: list[dict[str, object]] = []
    for effect in effects:
        try:
            metadata = json.loads(str(effect["metadata_json"]))
        except json.JSONDecodeError:
            failures.append(f"effect_metadata_json_invalid:{event_id}")
            return None
        if not isinstance(metadata, dict):
            failures.append(f"effect_metadata_not_object:{event_id}")
            return None
        payloads.append(
            {
                "kind": str(effect["kind"]),
                "contract_key": str(effect["contract_key"]) if effect["contract_key"] else None,
                "generation": int(effect["generation"]),
                "reason": str(effect["reason"]),
                "quantity_hint": (
                    int(effect["quantity_hint"]) if effect["quantity_hint"] is not None else None
                ),
                "metadata": metadata,
                "status": str(effect["status"]),
            }
        )
    return effect_payload_digest(payloads)


def _extended_expected_payload(
    db: sqlite3.Connection,
    *,
    event_id: str,
    payload: dict[str, object],
    base: dict[str, Scalar],
    effects: Sequence[sqlite3.Row],
    failures: list[str],
) -> dict[str, Scalar] | None:
    process_fields = _process_seq_bound_field(
        db,
        payload,
        event_id=event_id,
        failures=failures,
    )
    if process_fields is None:
        return None
    base = {**base, **process_fields}
    if "effects_sha256" in payload:
        effects_sha256 = _effect_rows_digest(
            effects,
            event_id=event_id,
            failures=failures,
        )
        if effects_sha256 is None:
            return None
        base = {**base, "effects_sha256": effects_sha256}
    if "decision_packet_id" not in payload:
        return base
    if not _table_exists(db, "operator_decision_packets"):
        failures.append(f"missing_decision_packet_table:{event_id}")
        return None
    packet = db.execute(
        """
        SELECT packet_id,disposition,system_mode,payload_json
        FROM operator_decision_packets WHERE event_id=?
        """,
        (event_id,),
    ).fetchone()
    if packet is None:
        failures.append(f"missing_decision_packet:{event_id}")
        return None
    statuses = {str(effect["status"]) for effect in effects}
    effect_status = next(iter(statuses)) if len(statuses) == 1 else ""
    if len(statuses) > 1:
        failures.append(f"mixed_effect_status:{event_id}")
    bound_fields = _packet_bound_fields(
        str(packet["payload_json"]),
        payload,
        event_id=event_id,
        failures=failures,
    )
    if bound_fields is None:
        return None
    return {
        **base,
        "effect_status": effect_status,
        "decision_packet_id": str(packet["packet_id"]),
        "decision_disposition": str(packet["disposition"]),
        "operational_mode": str(packet["system_mode"]),
        **bound_fields,
    }


def verify_database_evidence(path: str | Path) -> DatabaseEvidenceVerification:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    failures: list[str] = []
    try:
        ensure_integrity_schema(db)
        rows = db.execute(
            "SELECT sequence,record_id,previous_hash,payload_sha256,payload_json,record_hash "
            "FROM integrity_ledger ORDER BY sequence"
        ).fetchall()
        records = tuple(
            IntegrityRecord(
                int(row["sequence"]),
                str(row["record_id"]),
                str(row["previous_hash"]),
                str(row["payload_sha256"]),
                str(row["record_hash"]),
            )
            for row in rows
        )
        chain = verify_chain(records)
        if not chain.ok:
            failures.append(f"chain_failure:{chain.failure_sequence}")

        for row in rows:
            event_id = str(row["record_id"])
            payload_json = str(row["payload_json"])
            if _payload_hash(payload_json) != str(row["payload_sha256"]):
                failures.append(f"payload_hash_mismatch:{event_id}")
                continue
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                failures.append(f"payload_json_invalid:{event_id}")
                continue
            if not isinstance(payload, dict):
                failures.append(f"payload_not_object:{event_id}")
                continue

            signal = db.execute(
                "SELECT message_id,kind,contract_key FROM signal_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            audit = db.execute(
                "SELECT parser_rule,parser_confidence,association_method "
                "FROM decision_audit WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if signal is None or audit is None:
                failures.append(f"missing_source_evidence:{event_id}")
                continue

            effects = db.execute(
                """
                SELECT kind,status,contract_key,generation,reason,quantity_hint,metadata_json
                FROM proposed_effects
                WHERE source_event_id=? ORDER BY effect_id
                """,
                (event_id,),
            ).fetchall()
            raw_revision_id = str(payload.get("raw_revision_id", ""))
            raw = db.execute(
                """
                SELECT r.message_id,p.status
                FROM raw_discord_events r
                JOIN raw_processing p ON p.raw_event_id=r.raw_event_id
                WHERE r.raw_event_id=?
                """,
                (raw_revision_id,),
            ).fetchone()
            if raw is None:
                failures.append(f"missing_raw_revision:{event_id}")
                continue
            if raw["status"] != "DONE" or raw["message_id"] != signal["message_id"]:
                failures.append(f"raw_link_mismatch:{event_id}")

            base_payload: dict[str, Scalar] = {
                "raw_revision_id": raw_revision_id,
                "message_id": str(signal["message_id"]),
                "kind": str(signal["kind"]),
                "contract_key": str(signal["contract_key"] or ""),
                "parser_rule": str(audit["parser_rule"]),
                "parser_confidence": float(audit["parser_confidence"]),
                "association_method": str(audit["association_method"]),
                "effect_count": len(effects),
                "effect_kinds": ",".join(str(effect["kind"]) for effect in effects),
            }
            expected_payload = _extended_expected_payload(
                db,
                event_id=event_id,
                payload=payload,
                base=base_payload,
                effects=effects,
                failures=failures,
            )
            if expected_payload is None:
                continue
            expected_hash = _payload_hash(canonical_payload(expected_payload))
            if expected_hash != str(row["payload_sha256"]):
                failures.append(f"source_evidence_mismatch:{event_id}")

        uncovered = int(
            db.execute(
                """
                SELECT COUNT(*)
                FROM signal_events s
                LEFT JOIN integrity_ledger i ON i.record_id=s.event_id
                WHERE i.record_id IS NULL
                """
            ).fetchone()[0]
        )
    finally:
        db.close()
    return DatabaseEvidenceVerification(
        ok=not failures,
        checked=len(rows),
        legacy_uncovered_signals=uncovered,
        failures=tuple(failures),
    )


class IntegrityLedger:
    """Append-only SHA-256 hash chain for forensic evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        db = self._connect()
        try:
            ensure_integrity_schema(db)
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def append(self, record_id: str, payload: Mapping[str, Scalar]) -> IntegrityRecord:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            record = append_integrity_record(db, record_id, payload)
            db.execute("COMMIT")
            return record
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def records(self) -> tuple[IntegrityRecord, ...]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT sequence,record_id,previous_hash,payload_sha256,record_hash "
                "FROM integrity_ledger ORDER BY sequence"
            ).fetchall()
        finally:
            db.close()
        return tuple(
            IntegrityRecord(
                int(row["sequence"]),
                str(row["record_id"]),
                str(row["previous_hash"]),
                str(row["payload_sha256"]),
                str(row["record_hash"]),
            )
            for row in rows
        )

    def verify(self) -> IntegrityVerification:
        return verify_chain(self.records())

    def verify_database(self) -> DatabaseEvidenceVerification:
        return verify_database_evidence(self.path)

    def head_hash(self) -> str:
        records = self.records()
        return records[-1].record_hash if records else _GENESIS


@dataclass(frozen=True, slots=True)
class IntegrityCheckResult:
    ok: bool
    live_fingerprint: str
    durable_fingerprint: str
    durable_event_count: int
    observed_live_fingerprint: str | None = None
    observed_durable_fingerprint: str | None = None


@dataclass(slots=True)
class ReplayIntegritySentinel:
    """Periodically prove that in-memory state equals replay of the durable event log.

    The synchronous check runs only at its configured commit cadence. A mismatch latches
    the runtime halt before telemetry; it never rewrites events or manufactures state.
    """

    every_n_commits: int = 64
    _successful_commits: int = 0

    def __post_init__(self) -> None:
        if self.every_n_commits <= 0:
            raise ValueError("every_n_commits must be positive")

    def after_commit(
        self,
        store: Store,
        live_state: BookState,
        *,
        force: bool = False,
        observed_state: BookState | None = None,
        runtime_policy: RuntimePolicyBundle | None = None,
    ) -> IntegrityCheckResult | None:
        self._successful_commits += 1
        if not force and self._successful_commits % self.every_n_commits:
            return None

        policy = runtime_policy or RuntimePolicyBundle()
        events = load_signals_in_processing_order(store.path)
        # Pipeline's admitted intent book excludes durable BLOCKED dispositions.
        # Recorded fills have a separate reconstruction path and must not be
        # compared with a book of normalized source intent.
        durable_state, _ = replay_admitted_events(
            store.path,
            events,
            policy=policy.base,
            order=ReplayOrder.INPUT,
        )
        assert_valid_book(durable_state, max_open_positions=policy.base.max_open_positions)
        live_fingerprint = state_fingerprint(live_state)
        durable_fingerprint = state_fingerprint(durable_state)
        ok = live_fingerprint == durable_fingerprint
        observed_live_fingerprint = None
        observed_durable_fingerprint = None
        if observed_state is not None:
            durable_observed_state, _ = replay(events, policy.base, order=ReplayOrder.INPUT)
            assert_valid_book(
                durable_observed_state,
                max_open_positions=policy.base.max_open_positions,
            )
            observed_live_fingerprint = state_fingerprint(observed_state)
            observed_durable_fingerprint = state_fingerprint(durable_observed_state)
            ok = ok and observed_live_fingerprint == observed_durable_fingerprint

        # Durably revoke admission before any diagnostic write can fail or the
        # process can exit. Telemetry must not be a prerequisite for the latch.
        if not ok:
            store.set_halt(True, "replay_integrity_divergence")

        store.heartbeat(
            "replay-integrity",
            status="ok" if ok else "diverged",
            live_fingerprint=live_fingerprint,
            durable_fingerprint=durable_fingerprint,
            durable_event_count=len(events),
            cadence_commits=self.every_n_commits,
            observed_live_fingerprint=observed_live_fingerprint,
            observed_durable_fingerprint=observed_durable_fingerprint,
            policy_fingerprint=policy.fingerprint,
            replay_order="durable_process_seq",
        )
        return IntegrityCheckResult(
            ok=ok,
            live_fingerprint=live_fingerprint,
            durable_fingerprint=durable_fingerprint,
            durable_event_count=len(events),
            observed_live_fingerprint=observed_live_fingerprint,
            observed_durable_fingerprint=observed_durable_fingerprint,
        )
