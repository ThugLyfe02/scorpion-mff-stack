from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class ReadinessCapabilityEventKind(StrEnum):
    ISSUED = "ISSUED"
    CONSUMED = "CONSUMED"
    ACTIVATED = "ACTIVATED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class ReadinessCapabilityJournalReport:
    certificate_id: str
    events: int
    latest_kind: str
    latest_rollout_id: str
    contiguous_sequence: bool
    event_hashes_valid: bool
    event_ids_valid: bool
    chain_valid: bool
    checkpoint_matches_history: bool
    transition_sequence_valid: bool
    deployment_cross_bindings_valid: bool
    materialized_consumption_matches: bool
    head_event_hash: str
    chain_hash: str
    valid: bool
    failures: tuple[str, ...]


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _tables_present(db: sqlite3.Connection) -> bool:
    required = {"readiness_capability_events", "readiness_capability_integrity_state"}
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
        tuple(sorted(required)),
    ).fetchall()
    return {str(row[0]) for row in rows} == required


def capability_payload_sha256(
    *,
    certificate_id: str,
    component: str,
    candidate_release_id: str,
    expires_ts_utc: datetime,
) -> str:
    return _hash_payload(
        {
            "version": "readiness-capability-payload-v1",
            "certificate_id": certificate_id,
            "component": component,
            "candidate_release_id": candidate_release_id,
            "expires_ts_utc": expires_ts_utc.astimezone(UTC).isoformat(),
        }
    )


def _event_hash(
    *,
    certificate_id: str,
    seq: int,
    event_kind: str,
    component: str,
    candidate_release_id: str,
    rollout_id: str,
    actor: str,
    reason: str,
    payload_sha256: str,
    deployment_event_hash: str,
    created_ts_utc: datetime,
    previous_event_hash: str,
) -> str:
    return _hash_payload(
        {
            "version": "readiness-capability-event-v1",
            "certificate_id": certificate_id,
            "seq": seq,
            "event_kind": event_kind,
            "component": component,
            "candidate_release_id": candidate_release_id,
            "rollout_id": rollout_id,
            "actor": actor,
            "reason": reason,
            "payload_sha256": payload_sha256,
            "deployment_event_hash": deployment_event_hash,
            "created_ts_utc": created_ts_utc.astimezone(UTC).isoformat(),
            "previous_event_hash": previous_event_hash,
        }
    )


def _allowed_transition(previous: str, current: str) -> bool:
    if not previous:
        return current == ReadinessCapabilityEventKind.ISSUED.value
    allowed = {
        ReadinessCapabilityEventKind.ISSUED.value: {
            ReadinessCapabilityEventKind.CONSUMED.value,
            ReadinessCapabilityEventKind.INVALIDATED.value,
            ReadinessCapabilityEventKind.EXPIRED.value,
        },
        ReadinessCapabilityEventKind.CONSUMED.value: {
            ReadinessCapabilityEventKind.ACTIVATED.value,
            ReadinessCapabilityEventKind.INVALIDATED.value,
            ReadinessCapabilityEventKind.EXPIRED.value,
        },
        ReadinessCapabilityEventKind.ACTIVATED.value: set(),
        ReadinessCapabilityEventKind.INVALIDATED.value: set(),
        ReadinessCapabilityEventKind.EXPIRED.value: set(),
    }
    return current in allowed.get(previous, set())


def append_readiness_capability_event(
    db: sqlite3.Connection,
    *,
    certificate_id: str,
    event_kind: ReadinessCapabilityEventKind,
    component: str,
    candidate_release_id: str,
    rollout_id: str,
    actor: str,
    reason: str,
    payload_sha256: str,
    deployment_event_hash: str = "",
    now: datetime,
) -> str:
    if not _tables_present(db):
        raise RuntimeError("readiness capability journal schema is missing")
    if not certificate_id.strip() or not component.strip() or not candidate_release_id.strip():
        raise ValueError("certificate, component and candidate identities are required")
    if not actor.strip() or not reason.strip() or not payload_sha256.strip():
        raise ValueError("actor, reason and payload hash are required")
    db.row_factory = sqlite3.Row
    last = db.execute(
        "SELECT * FROM readiness_capability_events WHERE certificate_id=? ORDER BY seq DESC LIMIT 1",
        (certificate_id,),
    ).fetchone()
    seq = int(last["seq"]) + 1 if last is not None else 1
    previous_kind = str(last["event_kind"]) if last is not None else ""
    previous_hash = str(last["event_hash"]) if last is not None else ""
    if not _allowed_transition(previous_kind, event_kind.value):
        raise ValueError(
            f"invalid readiness capability transition:{previous_kind or 'NONE'}->{event_kind.value}"
        )
    if last is not None:
        if str(last["component"]) != component:
            raise ValueError("readiness capability component changed across journal")
        if str(last["candidate_release_id"]) != candidate_release_id:
            raise ValueError("readiness capability candidate changed across journal")
        if str(last["payload_sha256"]) != payload_sha256:
            raise ValueError("readiness capability payload hash changed across journal")
    digest = _event_hash(
        certificate_id=certificate_id,
        seq=seq,
        event_kind=event_kind.value,
        component=component,
        candidate_release_id=candidate_release_id,
        rollout_id=rollout_id,
        actor=actor,
        reason=reason[:1000],
        payload_sha256=payload_sha256,
        deployment_event_hash=deployment_event_hash,
        created_ts_utc=now,
        previous_event_hash=previous_hash,
    )
    event_id = _hash_payload(
        {
            "version": "readiness-capability-event-id-v1",
            "certificate_id": certificate_id,
            "seq": seq,
            "event_hash": digest,
        }
    )
    db.execute(
        """
        INSERT INTO readiness_capability_events
        (event_id,certificate_id,seq,event_kind,component,candidate_release_id,rollout_id,
         actor,reason,payload_sha256,deployment_event_hash,created_ts_utc,
         previous_event_hash,event_hash)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            certificate_id,
            seq,
            event_kind.value,
            component,
            candidate_release_id,
            rollout_id,
            actor,
            reason[:1000],
            payload_sha256,
            deployment_event_hash,
            now.astimezone(UTC).isoformat(),
            previous_hash,
            digest,
        ),
    )
    checkpoint = db.execute(
        "SELECT event_count,chain_hash FROM readiness_capability_integrity_state "
        "WHERE certificate_id=?",
        (certificate_id,),
    ).fetchone()
    prior_count = int(checkpoint["event_count"]) if checkpoint is not None else 0
    prior_chain = str(checkpoint["chain_hash"]) if checkpoint is not None else ""
    chain_hash = hashlib.sha256(f"{prior_chain}|{event_id}".encode()).hexdigest()
    db.execute(
        """
        INSERT INTO readiness_capability_integrity_state
        (certificate_id,event_count,head_event_hash,chain_hash) VALUES (?,?,?,?)
        ON CONFLICT(certificate_id) DO UPDATE SET
            event_count=excluded.event_count,
            head_event_hash=excluded.head_event_hash,
            chain_hash=excluded.chain_hash
        """,
        (certificate_id, prior_count + 1, digest, chain_hash),
    )
    return digest


def ensure_readiness_capability_issued(
    db: sqlite3.Connection,
    *,
    certificate_id: str,
    component: str,
    candidate_release_id: str,
    expires_ts_utc: datetime,
    actor: str,
    issued_ts_utc: datetime,
) -> str:
    payload = capability_payload_sha256(
        certificate_id=certificate_id,
        component=component,
        candidate_release_id=candidate_release_id,
        expires_ts_utc=expires_ts_utc,
    )
    row = db.execute(
        "SELECT * FROM readiness_capability_events WHERE certificate_id=? ORDER BY seq LIMIT 1",
        (certificate_id,),
    ).fetchone()
    if row is not None:
        if str(row["event_kind"]) != ReadinessCapabilityEventKind.ISSUED.value:
            raise ValueError("readiness capability journal does not begin with ISSUED")
        if str(row["payload_sha256"]) != payload:
            raise ValueError("readiness capability issued payload does not match certificate")
        return str(row["event_hash"])
    return append_readiness_capability_event(
        db,
        certificate_id=certificate_id,
        event_kind=ReadinessCapabilityEventKind.ISSUED,
        component=component,
        candidate_release_id=candidate_release_id,
        rollout_id="",
        actor=actor,
        reason="readiness capability entered authoritative deployment lifecycle",
        payload_sha256=payload,
        now=issued_ts_utc,
    )


def _deployment_binding_valid(
    db: sqlite3.Connection,
    *,
    rollout_id: str,
    deployment_event_hash: str,
    event_kind: str,
) -> bool:
    if not deployment_event_hash:
        return event_kind == ReadinessCapabilityEventKind.ISSUED.value
    if not rollout_id:
        return False
    row = db.execute(
        "SELECT to_state FROM deployment_rollout_events WHERE rollout_id=? AND event_hash=?",
        (rollout_id, deployment_event_hash),
    ).fetchone()
    if row is None:
        return False
    expected = {
        ReadinessCapabilityEventKind.CONSUMED.value: "PREPARED",
        ReadinessCapabilityEventKind.ACTIVATED.value: "ACTIVE_GUARDED",
        ReadinessCapabilityEventKind.INVALIDATED.value: "CANCELLED",
        ReadinessCapabilityEventKind.EXPIRED.value: "EXPIRED",
    }
    return str(row[0]) == expected.get(event_kind, "")


def verify_readiness_capability_journal_connection(
    db: sqlite3.Connection,
    *,
    certificate_id: str,
    expected_latest_kind: ReadinessCapabilityEventKind | None = None,
) -> ReadinessCapabilityJournalReport:
    db.row_factory = sqlite3.Row
    if not _tables_present(db):
        return ReadinessCapabilityJournalReport(
            certificate_id=certificate_id,
            events=0,
            latest_kind="",
            latest_rollout_id="",
            contiguous_sequence=False,
            event_hashes_valid=False,
            event_ids_valid=False,
            chain_valid=False,
            checkpoint_matches_history=False,
            transition_sequence_valid=False,
            deployment_cross_bindings_valid=False,
            materialized_consumption_matches=False,
            head_event_hash="",
            chain_hash="",
            valid=False,
            failures=("readiness_capability_journal_missing",),
        )
    rows = db.execute(
        "SELECT * FROM readiness_capability_events WHERE certificate_id=? ORDER BY seq",
        (certificate_id,),
    ).fetchall()
    failures: list[str] = []
    contiguous = [int(row["seq"]) for row in rows] == list(range(1, len(rows) + 1))
    if not contiguous:
        failures.append("readiness_capability_sequence_gap")
    hashes_valid = True
    ids_valid = True
    transitions_valid = True
    deployment_valid = True
    previous_hash = ""
    previous_kind = ""
    chain_hash = ""
    for row in rows:
        kind = str(row["event_kind"])
        created = datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC)
        expected_hash = _event_hash(
            certificate_id=certificate_id,
            seq=int(row["seq"]),
            event_kind=kind,
            component=str(row["component"]),
            candidate_release_id=str(row["candidate_release_id"]),
            rollout_id=str(row["rollout_id"]),
            actor=str(row["actor"]),
            reason=str(row["reason"]),
            payload_sha256=str(row["payload_sha256"]),
            deployment_event_hash=str(row["deployment_event_hash"]),
            created_ts_utc=created,
            previous_event_hash=str(row["previous_event_hash"]),
        )
        if (
            str(row["previous_event_hash"]) != previous_hash
            or expected_hash != str(row["event_hash"])
        ):
            hashes_valid = False
        expected_id = _hash_payload(
            {
                "version": "readiness-capability-event-id-v1",
                "certificate_id": certificate_id,
                "seq": int(row["seq"]),
                "event_hash": str(row["event_hash"]),
            }
        )
        if expected_id != str(row["event_id"]):
            ids_valid = False
        if not _allowed_transition(previous_kind, kind):
            transitions_valid = False
        if not _deployment_binding_valid(
            db,
            rollout_id=str(row["rollout_id"]),
            deployment_event_hash=str(row["deployment_event_hash"]),
            event_kind=kind,
        ):
            deployment_valid = False
        chain_hash = hashlib.sha256(f"{chain_hash}|{row['event_id']}".encode()).hexdigest()
        previous_hash = str(row["event_hash"])
        previous_kind = kind
    if not rows:
        failures.append("readiness_capability_journal_empty")
    if not hashes_valid:
        failures.append("readiness_capability_event_hash_mismatch")
    if not ids_valid:
        failures.append("readiness_capability_event_id_mismatch")
    if not transitions_valid:
        failures.append("readiness_capability_transition_invalid")
    if not deployment_valid:
        failures.append("readiness_capability_deployment_cross_binding_invalid")
    checkpoint = db.execute(
        "SELECT * FROM readiness_capability_integrity_state WHERE certificate_id=?",
        (certificate_id,),
    ).fetchone()
    checkpoint_matches = checkpoint is not None and (
        int(checkpoint["event_count"]) == len(rows)
        and str(checkpoint["head_event_hash"]) == previous_hash
        and str(checkpoint["chain_hash"]) == chain_hash
    )
    if not checkpoint_matches:
        failures.append("readiness_capability_integrity_checkpoint_mismatch")
    latest_kind = str(rows[-1]["event_kind"]) if rows else ""
    latest_rollout_id = str(rows[-1]["rollout_id"]) if rows else ""
    if expected_latest_kind is not None and latest_kind != expected_latest_kind.value:
        failures.append(
            f"readiness_capability_latest_kind_mismatch:{latest_kind or 'NONE'}"
        )
    materialized = db.execute(
        "SELECT consumed_rollout_id FROM production_readiness_consumptions WHERE certificate_id=?",
        (certificate_id,),
    ).fetchone()
    materialized_rollout = str(materialized[0]) if materialized is not None else ""
    expects_consumed = latest_kind in {
        ReadinessCapabilityEventKind.CONSUMED.value,
        ReadinessCapabilityEventKind.ACTIVATED.value,
        ReadinessCapabilityEventKind.INVALIDATED.value,
        ReadinessCapabilityEventKind.EXPIRED.value,
    }
    materialized_matches = (
        (not expects_consumed and not materialized_rollout)
        or (
            expects_consumed
            and bool(latest_rollout_id)
            and materialized_rollout == latest_rollout_id
        )
    )
    if not materialized_matches:
        failures.append("readiness_capability_materialized_consumption_mismatch")
    return ReadinessCapabilityJournalReport(
        certificate_id=certificate_id,
        events=len(rows),
        latest_kind=latest_kind,
        latest_rollout_id=latest_rollout_id,
        contiguous_sequence=contiguous,
        event_hashes_valid=hashes_valid,
        event_ids_valid=ids_valid,
        chain_valid=hashes_valid,
        checkpoint_matches_history=checkpoint_matches,
        transition_sequence_valid=transitions_valid,
        deployment_cross_bindings_valid=deployment_valid,
        materialized_consumption_matches=materialized_matches,
        head_event_hash=previous_hash,
        chain_hash=chain_hash,
        valid=not failures,
        failures=tuple(failures),
    )


def verify_readiness_capability_journal(
    path: str | Path,
    *,
    certificate_id: str,
    expected_latest_kind: ReadinessCapabilityEventKind | None = None,
) -> ReadinessCapabilityJournalReport:
    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(database)
    with sqlite3.connect(str(database)) as db:
        db.row_factory = sqlite3.Row
        return verify_readiness_capability_journal_connection(
            db,
            certificate_id=certificate_id,
            expected_latest_kind=expected_latest_kind,
        )
