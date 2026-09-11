from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .research_experimentation import (
    ResearchAssignment,
    ResearchExperimentPolicy,
    ResearchTreatmentBundle,
    assign_research_treatment,
    load_research_experiment_records,
)

_GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_randomization_units (
    randomization_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    wave_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    distribution_hash TEXT NOT NULL,
    entropy_hex TEXT NOT NULL,
    entropy_commitment TEXT NOT NULL,
    random_draw REAL NOT NULL,
    created_ts_utc TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE,
    UNIQUE(experiment_id,wave_id,cluster_id)
);
CREATE TABLE IF NOT EXISTS research_assignment_attestations (
    assignment_id TEXT PRIMARY KEY,
    randomization_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    wave_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    chosen_treatment_key TEXT NOT NULL,
    attested_ts_utc TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS research_randomization_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    previous_event_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_ts_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_randomization_integrity_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    event_count INTEGER NOT NULL,
    head_event_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS research_randomization_units_no_update
BEFORE UPDATE ON research_randomization_units
BEGIN SELECT RAISE(ABORT,'research_randomization_units_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_randomization_units_no_delete
BEFORE DELETE ON research_randomization_units
BEGIN SELECT RAISE(ABORT,'research_randomization_units_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_assignment_attestations_no_update
BEFORE UPDATE ON research_assignment_attestations
BEGIN SELECT RAISE(ABORT,'research_assignment_attestations_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_assignment_attestations_no_delete
BEFORE DELETE ON research_assignment_attestations
BEGIN SELECT RAISE(ABORT,'research_assignment_attestations_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_randomization_events_no_update
BEFORE UPDATE ON research_randomization_events
BEGIN SELECT RAISE(ABORT,'research_randomization_events_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_randomization_events_no_delete
BEFORE DELETE ON research_randomization_events
BEGIN SELECT RAISE(ABORT,'research_randomization_events_append_only'); END;
"""


@dataclass(frozen=True, slots=True)
class AttestedRandomizationUnit:
    randomization_id: str
    experiment_id: str
    wave_id: str
    cluster_id: str
    distribution_hash: str
    entropy_hex: str
    entropy_commitment: str
    random_draw: float
    created_ts_utc: datetime
    record_hash: str


@dataclass(frozen=True, slots=True)
class AssignmentRandomizationAttestation:
    assignment_id: str
    randomization_id: str
    experiment_id: str
    wave_id: str
    cluster_id: str
    chosen_treatment_key: str
    attested_ts_utc: datetime
    record_hash: str


@dataclass(frozen=True, slots=True)
class RandomizationIntegrityReport:
    units: int
    assignment_attestations: int
    events: int
    head_event_hash: str
    chain_hash: str
    valid: bool
    failures: tuple[str, ...]


def _json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(payload: object) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _bundle_payload(bundle: ResearchTreatmentBundle) -> dict[str, object]:
    return {
        "interventions": [item.value for item in bundle.interventions],
        "cost_units": bundle.cost_units,
    }


def _distribution_hash(
    distribution: tuple[tuple[ResearchTreatmentBundle, float], ...],
) -> str:
    material = [
        {
            "bundle": _bundle_payload(bundle),
            "probability": round(probability, 12),
        }
        for bundle, probability in sorted(distribution, key=lambda item: item[0].treatment_key)
    ]
    return _hash({"version": "research-randomization-distribution-v1", "distribution": material})


def _derive_draw(
    *,
    entropy_hex: str,
    experiment_id: str,
    wave_id: str,
    cluster_id: str,
    distribution_hash: str,
) -> float:
    seed_material = {
        "version": "research-randomization-draw-v1",
        "entropy_hex": entropy_hex,
        "experiment_id": experiment_id,
        "wave_id": wave_id,
        "cluster_id": cluster_id,
        "distribution_hash": distribution_hash,
    }
    digest = hashlib.sha256(_json(seed_material).encode()).digest()
    numerator = int.from_bytes(digest[:8], "big")
    return numerator / float(1 << 64)


def _unit_payload(unit: AttestedRandomizationUnit) -> dict[str, object]:
    return {
        "version": "attested-randomization-unit-v1",
        "experiment_id": unit.experiment_id,
        "wave_id": unit.wave_id,
        "cluster_id": unit.cluster_id,
        "distribution_hash": unit.distribution_hash,
        "entropy_hex": unit.entropy_hex,
        "entropy_commitment": unit.entropy_commitment,
        "random_draw": round(unit.random_draw, 15),
        "created_ts_utc": unit.created_ts_utc.astimezone(UTC).isoformat(),
    }


def _attestation_payload(attestation: AssignmentRandomizationAttestation) -> dict[str, object]:
    return {
        "version": "assignment-randomization-attestation-v1",
        "assignment_id": attestation.assignment_id,
        "randomization_id": attestation.randomization_id,
        "experiment_id": attestation.experiment_id,
        "wave_id": attestation.wave_id,
        "cluster_id": attestation.cluster_id,
        "chosen_treatment_key": attestation.chosen_treatment_key,
        "attested_ts_utc": attestation.attested_ts_utc.astimezone(UTC).isoformat(),
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
        "SELECT sequence,event_hash FROM research_randomization_events "
        "ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(previous["sequence"]) + 1 if previous is not None else 1
    previous_hash = str(previous["event_hash"]) if previous is not None else _GENESIS
    event_hash = _hash(
        {
            "version": "research-randomization-event-v1",
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
        INSERT INTO research_randomization_events
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
        "SELECT event_count,chain_hash FROM research_randomization_integrity_state "
        "WHERE singleton_id=1"
    ).fetchone()
    count = int(checkpoint["event_count"]) if checkpoint is not None else 0
    chain = str(checkpoint["chain_hash"]) if checkpoint is not None else _GENESIS
    chain = hashlib.sha256(f"{chain}|{event_hash}".encode()).hexdigest()
    db.execute(
        """
        INSERT INTO research_randomization_integrity_state
        (singleton_id,event_count,head_event_hash,chain_hash) VALUES (1,?,?,?)
        ON CONFLICT(singleton_id) DO UPDATE SET
            event_count=excluded.event_count,
            head_event_hash=excluded.head_event_hash,
            chain_hash=excluded.chain_hash
        """,
        (count + 1, event_hash, chain),
    )


def _load_unit(row: sqlite3.Row) -> AttestedRandomizationUnit:
    return AttestedRandomizationUnit(
        randomization_id=str(row["randomization_id"]),
        experiment_id=str(row["experiment_id"]),
        wave_id=str(row["wave_id"]),
        cluster_id=str(row["cluster_id"]),
        distribution_hash=str(row["distribution_hash"]),
        entropy_hex=str(row["entropy_hex"]),
        entropy_commitment=str(row["entropy_commitment"]),
        random_draw=float(row["random_draw"]),
        created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC),
        record_hash=str(row["record_hash"]),
    )


def get_or_create_randomization_unit(
    path: str | Path,
    *,
    experiment_id: str,
    wave_id: str,
    cluster_id: str,
    distribution: tuple[tuple[ResearchTreatmentBundle, float], ...],
    created_ts_utc: datetime | None = None,
) -> AttestedRandomizationUnit:
    for name, value in (
        ("experiment_id", experiment_id),
        ("wave_id", wave_id),
        ("cluster_id", cluster_id),
    ):
        if not value.strip():
            raise ValueError(f"{name} is required")
    created = (created_ts_utc or datetime.now(UTC)).astimezone(UTC)
    distribution_hash = _distribution_hash(distribution)
    db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            """
            SELECT * FROM research_randomization_units
            WHERE experiment_id=? AND wave_id=? AND cluster_id=?
            """,
            (experiment_id, wave_id, cluster_id),
        ).fetchone()
        if existing is not None:
            unit = _load_unit(existing)
            if unit.distribution_hash != distribution_hash:
                raise ValueError("cluster randomization distribution changed within wave")
            db.execute("COMMIT")
            return unit
        entropy_hex = secrets.token_hex(32)
        entropy_commitment = hashlib.sha256(bytes.fromhex(entropy_hex)).hexdigest()
        random_draw = _derive_draw(
            entropy_hex=entropy_hex,
            experiment_id=experiment_id,
            wave_id=wave_id,
            cluster_id=cluster_id,
            distribution_hash=distribution_hash,
        )
        provisional = AttestedRandomizationUnit(
            randomization_id="",
            experiment_id=experiment_id,
            wave_id=wave_id,
            cluster_id=cluster_id,
            distribution_hash=distribution_hash,
            entropy_hex=entropy_hex,
            entropy_commitment=entropy_commitment,
            random_draw=random_draw,
            created_ts_utc=created,
            record_hash="",
        )
        payload = _unit_payload(provisional)
        randomization_id = _hash({"version": "attested-randomization-id-v1", "payload": payload})
        record_hash = _hash(payload)
        db.execute(
            """
            INSERT INTO research_randomization_units
            (randomization_id,experiment_id,wave_id,cluster_id,distribution_hash,entropy_hex,
             entropy_commitment,random_draw,created_ts_utc,record_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                randomization_id,
                experiment_id,
                wave_id,
                cluster_id,
                distribution_hash,
                entropy_hex,
                entropy_commitment,
                random_draw,
                created.isoformat(),
                record_hash,
            ),
        )
        _append_event(
            db,
            event_kind="RANDOMIZATION_UNIT",
            record_id=randomization_id,
            record_hash=record_hash,
            created_ts_utc=created,
        )
        db.execute("COMMIT")
        return AttestedRandomizationUnit(
            randomization_id=randomization_id,
            experiment_id=experiment_id,
            wave_id=wave_id,
            cluster_id=cluster_id,
            distribution_hash=distribution_hash,
            entropy_hex=entropy_hex,
            entropy_commitment=entropy_commitment,
            random_draw=random_draw,
            created_ts_utc=created,
            record_hash=record_hash,
        )
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def assign_attested_research_treatment(
    path: str | Path,
    *,
    experiment_id: str,
    wave_id: str,
    cluster_id: str,
    episode_id: str,
    event_id: str,
    step_index: int,
    regime_key: str,
    context_fingerprint: str,
    allocation_hash: str,
    reward_contract_hash: str,
    distribution: tuple[tuple[ResearchTreatmentBundle, float], ...],
    maturity_ts_utc: datetime,
    expected_previous_assignment_id: str = "",
    assigned_ts_utc: datetime | None = None,
    policy: ResearchExperimentPolicy | None = None,
) -> ResearchAssignment:
    assigned = (assigned_ts_utc or datetime.now(UTC)).astimezone(UTC)
    unit = get_or_create_randomization_unit(
        path,
        experiment_id=experiment_id,
        wave_id=wave_id,
        cluster_id=cluster_id,
        distribution=distribution,
        created_ts_utc=assigned,
    )
    assignment = assign_research_treatment(
        path,
        episode_id=episode_id,
        event_id=event_id,
        step_index=step_index,
        regime_key=regime_key,
        context_fingerprint=context_fingerprint,
        allocation_hash=allocation_hash,
        reward_contract_hash=reward_contract_hash,
        distribution=distribution,
        random_draw=unit.random_draw,
        maturity_ts_utc=maturity_ts_utc,
        expected_previous_assignment_id=expected_previous_assignment_id,
        assigned_ts_utc=assigned,
        policy=policy,
    )
    attested = datetime.now(UTC)
    provisional = AssignmentRandomizationAttestation(
        assignment_id=assignment.assignment_id,
        randomization_id=unit.randomization_id,
        experiment_id=experiment_id,
        wave_id=wave_id,
        cluster_id=cluster_id,
        chosen_treatment_key=assignment.chosen_treatment.treatment_key,
        attested_ts_utc=attested,
        record_hash="",
    )
    payload = _attestation_payload(provisional)
    record_hash = _hash(payload)
    db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            INSERT INTO research_assignment_attestations
            (assignment_id,randomization_id,experiment_id,wave_id,cluster_id,
             chosen_treatment_key,attested_ts_utc,record_hash)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                assignment.assignment_id,
                unit.randomization_id,
                experiment_id,
                wave_id,
                cluster_id,
                assignment.chosen_treatment.treatment_key,
                attested.isoformat(),
                record_hash,
            ),
        )
        _append_event(
            db,
            event_kind="ASSIGNMENT_ATTESTATION",
            record_id=assignment.assignment_id,
            record_hash=record_hash,
            created_ts_utc=attested,
        )
        db.execute("COMMIT")
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return assignment


def load_assignment_attestations(
    path: str | Path,
) -> dict[str, AssignmentRandomizationAttestation]:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        rows = db.execute("SELECT * FROM research_assignment_attestations").fetchall()
    return {
        str(row["assignment_id"]): AssignmentRandomizationAttestation(
            assignment_id=str(row["assignment_id"]),
            randomization_id=str(row["randomization_id"]),
            experiment_id=str(row["experiment_id"]),
            wave_id=str(row["wave_id"]),
            cluster_id=str(row["cluster_id"]),
            chosen_treatment_key=str(row["chosen_treatment_key"]),
            attested_ts_utc=datetime.fromisoformat(str(row["attested_ts_utc"])).astimezone(UTC),
            record_hash=str(row["record_hash"]),
        )
        for row in rows
    }


def verify_randomization_integrity(path: str | Path) -> RandomizationIntegrityReport:
    assignments, _ = load_research_experiment_records(path)
    assignment_by_id = {item.assignment_id: item for item in assignments}
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        unit_rows = db.execute("SELECT * FROM research_randomization_units").fetchall()
        attestation_rows = db.execute("SELECT * FROM research_assignment_attestations").fetchall()
        events = db.execute("SELECT * FROM research_randomization_events ORDER BY sequence").fetchall()
        checkpoint = db.execute(
            "SELECT * FROM research_randomization_integrity_state WHERE singleton_id=1"
        ).fetchone()
    failures: list[str] = []
    units: dict[str, AttestedRandomizationUnit] = {}
    records: dict[str, str] = {}
    for row in unit_rows:
        unit = _load_unit(row)
        expected_commitment = hashlib.sha256(bytes.fromhex(unit.entropy_hex)).hexdigest()
        if unit.entropy_commitment != expected_commitment:
            failures.append("randomization_entropy_commitment_mismatch")
        expected_draw = _derive_draw(
            entropy_hex=unit.entropy_hex,
            experiment_id=unit.experiment_id,
            wave_id=unit.wave_id,
            cluster_id=unit.cluster_id,
            distribution_hash=unit.distribution_hash,
        )
        if not math.isclose(unit.random_draw, expected_draw, rel_tol=0.0, abs_tol=1e-15):
            failures.append("randomization_draw_mismatch")
        payload = _unit_payload(unit)
        if unit.record_hash != _hash(payload):
            failures.append("randomization_unit_record_hash_mismatch")
        expected_id = _hash({"version": "attested-randomization-id-v1", "payload": payload})
        if unit.randomization_id != expected_id:
            failures.append("randomization_unit_id_mismatch")
        units[unit.randomization_id] = unit
        records[unit.randomization_id] = unit.record_hash
    for row in attestation_rows:
        attestation = AssignmentRandomizationAttestation(
            assignment_id=str(row["assignment_id"]),
            randomization_id=str(row["randomization_id"]),
            experiment_id=str(row["experiment_id"]),
            wave_id=str(row["wave_id"]),
            cluster_id=str(row["cluster_id"]),
            chosen_treatment_key=str(row["chosen_treatment_key"]),
            attested_ts_utc=datetime.fromisoformat(str(row["attested_ts_utc"])).astimezone(UTC),
            record_hash=str(row["record_hash"]),
        )
        unit = units.get(attestation.randomization_id)
        assignment = assignment_by_id.get(attestation.assignment_id)
        if unit is None or assignment is None:
            failures.append("randomization_attestation_parent_missing")
            continue
        if (
            unit.experiment_id != attestation.experiment_id
            or unit.wave_id != attestation.wave_id
            or unit.cluster_id != attestation.cluster_id
        ):
            failures.append("randomization_attestation_unit_mismatch")
        if not math.isclose(assignment.random_draw, unit.random_draw, rel_tol=0.0, abs_tol=1e-15):
            failures.append("randomization_assignment_draw_mismatch")
        if assignment.chosen_treatment.treatment_key != attestation.chosen_treatment_key:
            failures.append("randomization_assignment_treatment_mismatch")
        expected_hash = _hash(_attestation_payload(attestation))
        if attestation.record_hash != expected_hash:
            failures.append("randomization_attestation_record_hash_mismatch")
        records[attestation.assignment_id] = attestation.record_hash
    previous = _GENESIS
    chain = _GENESIS
    covered: set[str] = set()
    for expected_sequence, row in enumerate(events, start=1):
        sequence = int(row["sequence"])
        if sequence != expected_sequence:
            failures.append("randomization_event_sequence_gap")
        if str(row["previous_event_hash"]) != previous:
            failures.append("randomization_event_parent_mismatch")
        record_id = str(row["record_id"])
        record_hash = str(row["record_hash"])
        if records.get(record_id) != record_hash:
            failures.append("randomization_event_record_mismatch")
        created = datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC)
        expected_event_hash = _hash(
            {
                "version": "research-randomization-event-v1",
                "sequence": sequence,
                "event_kind": str(row["event_kind"]),
                "record_id": record_id,
                "record_hash": record_hash,
                "previous_event_hash": str(row["previous_event_hash"]),
                "created_ts_utc": created.isoformat(),
            }
        )
        if str(row["event_hash"]) != expected_event_hash:
            failures.append("randomization_event_hash_mismatch")
        covered.add(record_id)
        previous = str(row["event_hash"])
        chain = hashlib.sha256(f"{chain}|{row['event_hash']}".encode()).hexdigest()
    if covered != set(records):
        failures.append("randomization_event_coverage_mismatch")
    checkpoint_valid = checkpoint is not None and (
        int(checkpoint["event_count"]) == len(events)
        and str(checkpoint["head_event_hash"]) == previous
        and str(checkpoint["chain_hash"]) == chain
    )
    if not checkpoint_valid:
        failures.append("randomization_integrity_checkpoint_mismatch")
    unique = tuple(dict.fromkeys(failures))
    return RandomizationIntegrityReport(
        units=len(units),
        assignment_attestations=len(attestation_rows),
        events=len(events),
        head_event_hash=previous,
        chain_hash=chain,
        valid=not unique,
        failures=unique,
    )
