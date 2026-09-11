from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

_GENESIS = "0" * 64


class ResearchIntervention(StrEnum):
    CONTROL = "CONTROL"
    LIGHT_SHADOW = "LIGHT_SHADOW"
    DEEP_SHADOW = "DEEP_SHADOW"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    HOTSPOT_TARGETING = "HOTSPOT_TARGETING"
    CURRICULUM_INCLUSION = "CURRICULUM_INCLUSION"
    CONTEXTUAL_SPECIALIZATION = "CONTEXTUAL_SPECIALIZATION"


@dataclass(frozen=True, slots=True)
class ResearchTreatmentBundle:
    interventions: tuple[ResearchIntervention, ...]
    cost_units: int

    def __post_init__(self) -> None:
        if not self.interventions:
            raise ValueError("research treatment bundle cannot be empty")
        if len(self.interventions) != len(set(self.interventions)):
            raise ValueError("research treatment bundle interventions must be unique")
        normalized = tuple(sorted(self.interventions, key=lambda item: item.value))
        object.__setattr__(self, "interventions", normalized)
        if ResearchIntervention.CONTROL in normalized and len(normalized) != 1:
            raise ValueError("CONTROL cannot be combined with another intervention")
        if self.cost_units < 0:
            raise ValueError("research treatment cost cannot be negative")
        if normalized == (ResearchIntervention.CONTROL,) and self.cost_units != 0:
            raise ValueError("CONTROL research treatment must have zero cost")
        if normalized != (ResearchIntervention.CONTROL,) and self.cost_units <= 0:
            raise ValueError("non-control research treatments require positive cost")

    @property
    def treatment_key(self) -> str:
        names = "+".join(item.value for item in self.interventions)
        return f"{names}@{self.cost_units}"


@dataclass(frozen=True, slots=True)
class ResearchExperimentPolicy:
    minimum_propensity: float = 0.05
    maximum_bundle_size: int = 3
    maximum_maturity_delay: timedelta = timedelta(days=45)
    require_counterfactual_support: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.minimum_propensity) or not 0 < self.minimum_propensity <= 0.5:
            raise ValueError("minimum_propensity must be finite and in (0,0.5]")
        if self.maximum_bundle_size <= 0:
            raise ValueError("maximum_bundle_size must be positive")
        if self.maximum_maturity_delay <= timedelta(0):
            raise ValueError("maximum_maturity_delay must be positive")


@dataclass(frozen=True, slots=True)
class ResearchAssignment:
    assignment_id: str
    episode_id: str
    event_id: str
    step_index: int
    regime_key: str
    context_fingerprint: str
    allocation_hash: str
    reward_contract_hash: str
    distribution: tuple[tuple[ResearchTreatmentBundle, float], ...]
    chosen_treatment: ResearchTreatmentBundle
    chosen_propensity: float
    random_draw: float
    previous_assignment_id: str
    assigned_ts_utc: datetime
    maturity_ts_utc: datetime
    record_hash: str


@dataclass(frozen=True, slots=True)
class ResearchExperimentOutcome:
    outcome_id: str
    assignment_id: str
    reward_contract_hash: str
    realized_value: float
    evidence_hash: str
    realized_ts_utc: datetime
    record_hash: str


@dataclass(frozen=True, slots=True)
class ResearchExperimentVerification:
    assignments: int
    outcomes: int
    events: int
    head_event_hash: str
    chain_hash: str
    valid: bool
    failures: tuple[str, ...]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_assignments (
    assignment_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    regime_key TEXT NOT NULL,
    context_fingerprint TEXT NOT NULL,
    allocation_hash TEXT NOT NULL,
    reward_contract_hash TEXT NOT NULL,
    distribution_json TEXT NOT NULL,
    chosen_treatment_json TEXT NOT NULL,
    chosen_propensity REAL NOT NULL,
    random_draw REAL NOT NULL,
    previous_assignment_id TEXT NOT NULL,
    assigned_ts_utc TEXT NOT NULL,
    maturity_ts_utc TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE,
    UNIQUE(episode_id,step_index)
);
CREATE TABLE IF NOT EXISTS research_experiment_outcomes (
    outcome_id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL UNIQUE,
    reward_contract_hash TEXT NOT NULL,
    realized_value REAL NOT NULL,
    evidence_hash TEXT NOT NULL,
    realized_ts_utc TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS research_experiment_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    previous_event_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_ts_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_experiment_integrity_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    event_count INTEGER NOT NULL,
    head_event_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS research_assignments_no_update
BEFORE UPDATE ON research_assignments
BEGIN SELECT RAISE(ABORT,'research_assignments_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_assignments_no_delete
BEFORE DELETE ON research_assignments
BEGIN SELECT RAISE(ABORT,'research_assignments_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_experiment_outcomes_no_update
BEFORE UPDATE ON research_experiment_outcomes
BEGIN SELECT RAISE(ABORT,'research_experiment_outcomes_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_experiment_outcomes_no_delete
BEFORE DELETE ON research_experiment_outcomes
BEGIN SELECT RAISE(ABORT,'research_experiment_outcomes_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_experiment_events_no_update
BEFORE UPDATE ON research_experiment_events
BEGIN SELECT RAISE(ABORT,'research_experiment_events_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_experiment_events_no_delete
BEFORE DELETE ON research_experiment_events
BEGIN SELECT RAISE(ABORT,'research_experiment_events_append_only'); END;
"""


def _json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(payload: object) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _bundle_payload(bundle: ResearchTreatmentBundle) -> dict[str, object]:
    return {
        "interventions": [item.value for item in bundle.interventions],
        "cost_units": bundle.cost_units,
    }


def _decode_bundle(raw: object) -> ResearchTreatmentBundle:
    if not isinstance(raw, dict):
        raise ValueError("research treatment bundle must be an object")
    interventions_raw = raw.get("interventions")
    cost_raw = raw.get("cost_units")
    if not isinstance(interventions_raw, list) or not isinstance(cost_raw, int):
        raise ValueError("research treatment bundle payload is malformed")
    return ResearchTreatmentBundle(
        tuple(ResearchIntervention(str(item)) for item in interventions_raw),
        cost_raw,
    )


def _normalize_distribution(
    distribution: tuple[tuple[ResearchTreatmentBundle, float], ...],
    policy: ResearchExperimentPolicy,
) -> tuple[tuple[ResearchTreatmentBundle, float], ...]:
    if not distribution:
        raise ValueError("research assignment distribution cannot be empty")
    normalized = tuple(sorted(distribution, key=lambda item: item[0].treatment_key))
    keys = [bundle.treatment_key for bundle, _ in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError("research assignment distribution contains duplicate treatments")
    if policy.require_counterfactual_support and len(normalized) < 2:
        raise ValueError("counterfactual assignment requires at least two treatments")
    total = 0.0
    for bundle, probability in normalized:
        if len(bundle.interventions) > policy.maximum_bundle_size:
            raise ValueError("research treatment exceeds maximum bundle size")
        if not math.isfinite(probability) or probability < policy.minimum_propensity:
            raise ValueError("research treatment probability violates exploration floor")
        total += probability
    if abs(total - 1.0) > 1e-9:
        raise ValueError("research assignment probabilities must sum to one")
    return normalized


def _distribution_payload(
    distribution: tuple[tuple[ResearchTreatmentBundle, float], ...],
) -> list[dict[str, object]]:
    return [
        {"bundle": _bundle_payload(bundle), "probability": round(probability, 12)}
        for bundle, probability in distribution
    ]


def _assignment_payload(assignment: ResearchAssignment) -> dict[str, object]:
    return {
        "version": "research-assignment-v1",
        "episode_id": assignment.episode_id,
        "event_id": assignment.event_id,
        "step_index": assignment.step_index,
        "regime_key": assignment.regime_key,
        "context_fingerprint": assignment.context_fingerprint,
        "allocation_hash": assignment.allocation_hash,
        "reward_contract_hash": assignment.reward_contract_hash,
        "distribution": _distribution_payload(assignment.distribution),
        "chosen_treatment": _bundle_payload(assignment.chosen_treatment),
        "chosen_propensity": round(assignment.chosen_propensity, 12),
        "random_draw": round(assignment.random_draw, 12),
        "previous_assignment_id": assignment.previous_assignment_id,
        "assigned_ts_utc": assignment.assigned_ts_utc.astimezone(UTC).isoformat(),
        "maturity_ts_utc": assignment.maturity_ts_utc.astimezone(UTC).isoformat(),
    }


def _outcome_payload(outcome: ResearchExperimentOutcome) -> dict[str, object]:
    return {
        "version": "research-experiment-outcome-v1",
        "assignment_id": outcome.assignment_id,
        "reward_contract_hash": outcome.reward_contract_hash,
        "realized_value": round(outcome.realized_value, 12),
        "evidence_hash": outcome.evidence_hash,
        "realized_ts_utc": outcome.realized_ts_utc.astimezone(UTC).isoformat(),
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
        "SELECT sequence,event_hash FROM research_experiment_events "
        "ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(previous["sequence"]) + 1 if previous is not None else 1
    previous_hash = str(previous["event_hash"]) if previous is not None else _GENESIS
    event_hash = _hash(
        {
            "version": "research-experiment-event-v1",
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
        INSERT INTO research_experiment_events
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
        "SELECT event_count,chain_hash FROM research_experiment_integrity_state "
        "WHERE singleton_id=1"
    ).fetchone()
    count = int(checkpoint["event_count"]) if checkpoint is not None else 0
    chain = str(checkpoint["chain_hash"]) if checkpoint is not None else _GENESIS
    chain = hashlib.sha256(f"{chain}|{event_hash}".encode()).hexdigest()
    db.execute(
        """
        INSERT INTO research_experiment_integrity_state
        (singleton_id,event_count,head_event_hash,chain_hash) VALUES (1,?,?,?)
        ON CONFLICT(singleton_id) DO UPDATE SET
            event_count=excluded.event_count,
            head_event_hash=excluded.head_event_hash,
            chain_hash=excluded.chain_hash
        """,
        (count + 1, event_hash, chain),
    )


def assign_research_treatment(
    path: str | Path,
    *,
    episode_id: str,
    event_id: str,
    step_index: int,
    regime_key: str,
    context_fingerprint: str,
    allocation_hash: str,
    reward_contract_hash: str,
    distribution: tuple[tuple[ResearchTreatmentBundle, float], ...],
    random_draw: float,
    maturity_ts_utc: datetime,
    expected_previous_assignment_id: str = "",
    assigned_ts_utc: datetime | None = None,
    policy: ResearchExperimentPolicy | None = None,
) -> ResearchAssignment:
    policy = policy or ResearchExperimentPolicy()
    for name, value in (
        ("episode_id", episode_id),
        ("event_id", event_id),
        ("regime_key", regime_key),
        ("context_fingerprint", context_fingerprint),
        ("allocation_hash", allocation_hash),
        ("reward_contract_hash", reward_contract_hash),
    ):
        if not value.strip():
            raise ValueError(f"{name} is required")
    if step_index < 0:
        raise ValueError("step_index cannot be negative")
    if not math.isfinite(random_draw) or not 0 <= random_draw < 1:
        raise ValueError("random_draw must be finite and in [0,1)")
    assigned = assigned_ts_utc or datetime.now(UTC)
    if assigned.tzinfo is None or assigned.utcoffset() is None:
        raise ValueError("assigned_ts_utc must be timezone-aware")
    if maturity_ts_utc.tzinfo is None or maturity_ts_utc.utcoffset() is None:
        raise ValueError("maturity_ts_utc must be timezone-aware")
    assigned = assigned.astimezone(UTC)
    maturity = maturity_ts_utc.astimezone(UTC)
    if maturity <= assigned or maturity - assigned > policy.maximum_maturity_delay:
        raise ValueError("research outcome maturity is outside configured bounds")
    normalized = _normalize_distribution(distribution, policy)
    cumulative = 0.0
    chosen, propensity = normalized[-1]
    for bundle, probability in normalized:
        cumulative += probability
        if random_draw < cumulative:
            chosen, propensity = bundle, probability
            break

    db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        previous_assignment_id = ""
        if step_index == 0:
            if expected_previous_assignment_id:
                raise ValueError("first research step cannot declare a predecessor")
        else:
            previous = db.execute(
                "SELECT assignment_id FROM research_assignments "
                "WHERE episode_id=? AND step_index=?",
                (episode_id, step_index - 1),
            ).fetchone()
            if previous is None:
                raise ValueError("research sequence predecessor is missing")
            previous_assignment_id = str(previous["assignment_id"])
            if previous_assignment_id != expected_previous_assignment_id:
                raise ValueError("research sequence predecessor changed")
        provisional = ResearchAssignment(
            assignment_id="",
            episode_id=episode_id,
            event_id=event_id,
            step_index=step_index,
            regime_key=regime_key,
            context_fingerprint=context_fingerprint,
            allocation_hash=allocation_hash,
            reward_contract_hash=reward_contract_hash,
            distribution=normalized,
            chosen_treatment=chosen,
            chosen_propensity=propensity,
            random_draw=random_draw,
            previous_assignment_id=previous_assignment_id,
            assigned_ts_utc=assigned,
            maturity_ts_utc=maturity,
            record_hash="",
        )
        payload = _assignment_payload(provisional)
        assignment_id = _hash({"version": "research-assignment-id-v1", "payload": payload})
        record_hash = _hash(payload)
        db.execute(
            """
            INSERT INTO research_assignments
            (assignment_id,episode_id,event_id,step_index,regime_key,context_fingerprint,
             allocation_hash,reward_contract_hash,distribution_json,chosen_treatment_json,
             chosen_propensity,random_draw,previous_assignment_id,assigned_ts_utc,maturity_ts_utc,
             record_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                assignment_id,
                episode_id,
                event_id,
                step_index,
                regime_key,
                context_fingerprint,
                allocation_hash,
                reward_contract_hash,
                _json(_distribution_payload(normalized)),
                _json(_bundle_payload(chosen)),
                propensity,
                random_draw,
                previous_assignment_id,
                assigned.isoformat(),
                maturity.isoformat(),
                record_hash,
            ),
        )
        _append_event(
            db,
            event_kind="ASSIGNMENT",
            record_id=assignment_id,
            record_hash=record_hash,
            created_ts_utc=assigned,
        )
        db.execute("COMMIT")
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return ResearchAssignment(
        assignment_id=assignment_id,
        episode_id=episode_id,
        event_id=event_id,
        step_index=step_index,
        regime_key=regime_key,
        context_fingerprint=context_fingerprint,
        allocation_hash=allocation_hash,
        reward_contract_hash=reward_contract_hash,
        distribution=normalized,
        chosen_treatment=chosen,
        chosen_propensity=propensity,
        random_draw=random_draw,
        previous_assignment_id=previous_assignment_id,
        assigned_ts_utc=assigned,
        maturity_ts_utc=maturity,
        record_hash=record_hash,
    )


def record_research_experiment_outcome(
    path: str | Path,
    *,
    assignment_id: str,
    realized_value: float,
    evidence_hash: str,
    realized_ts_utc: datetime | None = None,
) -> ResearchExperimentOutcome:
    if not assignment_id.strip() or not evidence_hash.strip():
        raise ValueError("assignment_id and evidence_hash are required")
    if not math.isfinite(realized_value) or not -2 <= realized_value <= 2:
        raise ValueError("realized_value must be finite and in [-2,2]")
    realized = realized_ts_utc or datetime.now(UTC)
    if realized.tzinfo is None or realized.utcoffset() is None:
        raise ValueError("realized_ts_utc must be timezone-aware")
    realized = realized.astimezone(UTC)
    db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT reward_contract_hash,maturity_ts_utc FROM research_assignments "
            "WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        if row is None:
            raise KeyError(assignment_id)
        maturity = datetime.fromisoformat(str(row["maturity_ts_utc"])).astimezone(UTC)
        if realized < maturity:
            raise ValueError("research outcome cannot be recorded before maturity")
        reward_contract_hash = str(row["reward_contract_hash"])
        provisional = ResearchExperimentOutcome(
            outcome_id="",
            assignment_id=assignment_id,
            reward_contract_hash=reward_contract_hash,
            realized_value=realized_value,
            evidence_hash=evidence_hash,
            realized_ts_utc=realized,
            record_hash="",
        )
        payload = _outcome_payload(provisional)
        outcome_id = _hash({"version": "research-outcome-id-v1", "payload": payload})
        record_hash = _hash(payload)
        db.execute(
            """
            INSERT INTO research_experiment_outcomes
            (outcome_id,assignment_id,reward_contract_hash,realized_value,evidence_hash,
             realized_ts_utc,record_hash) VALUES (?,?,?,?,?,?,?)
            """,
            (
                outcome_id,
                assignment_id,
                reward_contract_hash,
                realized_value,
                evidence_hash,
                realized.isoformat(),
                record_hash,
            ),
        )
        _append_event(
            db,
            event_kind="OUTCOME",
            record_id=outcome_id,
            record_hash=record_hash,
            created_ts_utc=realized,
        )
        db.execute("COMMIT")
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return ResearchExperimentOutcome(
        outcome_id=outcome_id,
        assignment_id=assignment_id,
        reward_contract_hash=reward_contract_hash,
        realized_value=realized_value,
        evidence_hash=evidence_hash,
        realized_ts_utc=realized,
        record_hash=record_hash,
    )


def _decode_distribution(raw: str) -> tuple[tuple[ResearchTreatmentBundle, float], ...]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("research distribution must decode to a list")
    output: list[tuple[ResearchTreatmentBundle, float]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("research distribution entry must be an object")
        probability = item.get("probability")
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ValueError("research distribution probability must be numeric")
        output.append((_decode_bundle(item.get("bundle")), float(probability)))
    return tuple(output)


def _decode_assignment(row: sqlite3.Row) -> ResearchAssignment:
    return ResearchAssignment(
        assignment_id=str(row["assignment_id"]),
        episode_id=str(row["episode_id"]),
        event_id=str(row["event_id"]),
        step_index=int(row["step_index"]),
        regime_key=str(row["regime_key"]),
        context_fingerprint=str(row["context_fingerprint"]),
        allocation_hash=str(row["allocation_hash"]),
        reward_contract_hash=str(row["reward_contract_hash"]),
        distribution=_decode_distribution(str(row["distribution_json"])),
        chosen_treatment=_decode_bundle(json.loads(str(row["chosen_treatment_json"]))),
        chosen_propensity=float(row["chosen_propensity"]),
        random_draw=float(row["random_draw"]),
        previous_assignment_id=str(row["previous_assignment_id"]),
        assigned_ts_utc=datetime.fromisoformat(str(row["assigned_ts_utc"])).astimezone(UTC),
        maturity_ts_utc=datetime.fromisoformat(str(row["maturity_ts_utc"])).astimezone(UTC),
        record_hash=str(row["record_hash"]),
    )


def _decode_outcome(row: sqlite3.Row) -> ResearchExperimentOutcome:
    return ResearchExperimentOutcome(
        outcome_id=str(row["outcome_id"]),
        assignment_id=str(row["assignment_id"]),
        reward_contract_hash=str(row["reward_contract_hash"]),
        realized_value=float(row["realized_value"]),
        evidence_hash=str(row["evidence_hash"]),
        realized_ts_utc=datetime.fromisoformat(str(row["realized_ts_utc"])).astimezone(UTC),
        record_hash=str(row["record_hash"]),
    )


def load_research_experiment_records(
    path: str | Path,
) -> tuple[tuple[ResearchAssignment, ...], tuple[ResearchExperimentOutcome, ...]]:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        assignments = tuple(
            _decode_assignment(row)
            for row in db.execute(
                "SELECT * FROM research_assignments ORDER BY assigned_ts_utc,assignment_id"
            )
        )
        outcomes = tuple(
            _decode_outcome(row)
            for row in db.execute(
                "SELECT * FROM research_experiment_outcomes ORDER BY realized_ts_utc,outcome_id"
            )
        )
    return assignments, outcomes


def verify_research_experiment_ledger(
    path: str | Path,
) -> ResearchExperimentVerification:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        assignment_rows = db.execute("SELECT * FROM research_assignments").fetchall()
        outcome_rows = db.execute("SELECT * FROM research_experiment_outcomes").fetchall()
        events = db.execute(
            "SELECT * FROM research_experiment_events ORDER BY sequence"
        ).fetchall()
        checkpoint = db.execute(
            "SELECT * FROM research_experiment_integrity_state WHERE singleton_id=1"
        ).fetchone()
    failures: list[str] = []
    assignments: dict[str, ResearchAssignment] = {}
    for row in assignment_rows:
        try:
            assignment_item = _decode_assignment(row)
            assignment_payload = _assignment_payload(assignment_item)
        except (ValueError, TypeError, json.JSONDecodeError):
            failures.append("research_assignment_decode_failure")
            continue
        if assignment_item.record_hash != _hash(assignment_payload):
            failures.append("research_assignment_record_hash_mismatch")
        expected_assignment_id = _hash(
            {"version": "research-assignment-id-v1", "payload": assignment_payload}
        )
        if assignment_item.assignment_id != expected_assignment_id:
            failures.append("research_assignment_id_mismatch")
        assignments[assignment_item.assignment_id] = assignment_item
    by_episode: dict[str, list[ResearchAssignment]] = {}
    for assignment_item in assignments.values():
        by_episode.setdefault(assignment_item.episode_id, []).append(assignment_item)
    for episode_rows in by_episode.values():
        ordered = sorted(episode_rows, key=lambda value: value.step_index)
        for expected_step, assignment_item in enumerate(ordered):
            if assignment_item.step_index != expected_step:
                failures.append("research_assignment_step_gap")
            expected_previous = ordered[expected_step - 1].assignment_id if expected_step else ""
            if assignment_item.previous_assignment_id != expected_previous:
                failures.append("research_assignment_previous_mismatch")
    outcomes: dict[str, ResearchExperimentOutcome] = {}
    for row in outcome_rows:
        try:
            outcome_item = _decode_outcome(row)
            outcome_payload = _outcome_payload(outcome_item)
        except (ValueError, TypeError):
            failures.append("research_outcome_decode_failure")
            continue
        assignment_item = assignments.get(outcome_item.assignment_id)
        if assignment_item is None:
            failures.append("research_outcome_assignment_missing")
            continue
        if outcome_item.reward_contract_hash != assignment_item.reward_contract_hash:
            failures.append("research_outcome_reward_contract_mismatch")
        if outcome_item.realized_ts_utc < assignment_item.maturity_ts_utc:
            failures.append("research_outcome_before_maturity")
        if outcome_item.record_hash != _hash(outcome_payload):
            failures.append("research_outcome_record_hash_mismatch")
        expected_outcome_id = _hash(
            {"version": "research-outcome-id-v1", "payload": outcome_payload}
        )
        if outcome_item.outcome_id != expected_outcome_id:
            failures.append("research_outcome_id_mismatch")
        outcomes[outcome_item.outcome_id] = outcome_item
    records = {
        **{
            assignment_item.assignment_id: assignment_item.record_hash
            for assignment_item in assignments.values()
        },
        **{
            outcome_item.outcome_id: outcome_item.record_hash
            for outcome_item in outcomes.values()
        },
    }
    previous = _GENESIS
    chain = _GENESIS
    covered: set[str] = set()
    for expected_sequence, row in enumerate(events, start=1):
        sequence = int(row["sequence"])
        created = datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC)
        if sequence != expected_sequence:
            failures.append("research_experiment_event_sequence_gap")
        if str(row["previous_event_hash"]) != previous:
            failures.append("research_experiment_event_parent_mismatch")
        record_id = str(row["record_id"])
        record_hash = str(row["record_hash"])
        if records.get(record_id) != record_hash:
            failures.append("research_experiment_event_record_mismatch")
        expected_hash = _hash(
            {
                "version": "research-experiment-event-v1",
                "sequence": sequence,
                "event_kind": str(row["event_kind"]),
                "record_id": record_id,
                "record_hash": record_hash,
                "previous_event_hash": str(row["previous_event_hash"]),
                "created_ts_utc": created.isoformat(),
            }
        )
        if str(row["event_hash"]) != expected_hash:
            failures.append("research_experiment_event_hash_mismatch")
        covered.add(record_id)
        previous = str(row["event_hash"])
        chain = hashlib.sha256(f"{chain}|{row['event_hash']}".encode()).hexdigest()
    if covered != set(records):
        failures.append("research_experiment_event_coverage_mismatch")
    checkpoint_valid = checkpoint is not None and (
        int(checkpoint["event_count"]) == len(events)
        and str(checkpoint["head_event_hash"]) == previous
        and str(checkpoint["chain_hash"]) == chain
    )
    if not checkpoint_valid:
        failures.append("research_experiment_integrity_checkpoint_mismatch")
    unique = tuple(dict.fromkeys(failures))
    return ResearchExperimentVerification(
        assignments=len(assignments),
        outcomes=len(outcomes),
        events=len(events),
        head_event_hash=previous,
        chain_hash=chain,
        valid=not unique,
        failures=unique,
    )
