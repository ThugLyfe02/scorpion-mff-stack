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


class HypothesisStatus(StrEnum):
    PROPOSED = "PROPOSED"
    TESTING = "TESTING"
    SUPPORTED = "SUPPORTED"
    FALSIFIED = "FALSIFIED"
    INCONCLUSIVE = "INCONCLUSIVE"
    BREAKTHROUGH_CANDIDATE = "BREAKTHROUGH_CANDIDATE"
    RETIRED = "RETIRED"


class HypothesisReuseAction(StrEnum):
    NEW = "NEW"
    DO_NOT_REPEAT = "DO_NOT_REPEAT"
    REOPEN_CONTEXT_SHIFT = "REOPEN_CONTEXT_SHIFT"
    REOPEN_TEMPORAL_REVALIDATION = "REOPEN_TEMPORAL_REVALIDATION"


@dataclass(frozen=True, slots=True)
class ResearchHypothesis:
    hypothesis_id: str
    family_id: str
    parent_hypothesis_id: str
    generation: int
    canonical_claim: str
    claim_hash: str
    mechanism_scope: tuple[str, ...]
    intervention_scope: tuple[str, ...]
    created_ts_utc: datetime
    record_hash: str


@dataclass(frozen=True, slots=True)
class HypothesisResolution:
    resolution_id: str
    hypothesis_id: str
    status: HypothesisStatus
    evidence_ids: tuple[str, ...]
    dataset_fingerprints: tuple[str, ...]
    regime_keys: tuple[str, ...]
    time_blocks: tuple[str, ...]
    selection_family_id: str
    familywise_alpha_spent: float
    reason: str
    resolved_ts_utc: datetime
    record_hash: str


@dataclass(frozen=True, slots=True)
class HypothesisReuseDecision:
    action: HypothesisReuseAction
    hypothesis_id: str
    reason: str
    covered_regimes: tuple[str, ...]
    covered_datasets: tuple[str, ...]
    last_status: HypothesisStatus | None
    last_resolved_ts_utc: datetime | None


@dataclass(frozen=True, slots=True)
class HypothesisMemoryPolicy:
    familywise_alpha_budget: float = 0.05
    temporal_revalidation_after: timedelta = timedelta(days=90)
    allow_temporal_revalidation: bool = True

    def __post_init__(self) -> None:
        if not 0 < self.familywise_alpha_budget <= 1:
            raise ValueError("familywise_alpha_budget must be in (0,1]")
        if self.temporal_revalidation_after <= timedelta(0):
            raise ValueError("temporal_revalidation_after must be positive")


@dataclass(frozen=True, slots=True)
class HypothesisMemoryVerification:
    hypotheses: int
    resolutions: int
    events: int
    head_event_hash: str
    chain_hash: str
    valid: bool
    failures: tuple[str, ...]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    parent_hypothesis_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    canonical_claim TEXT NOT NULL,
    claim_hash TEXT NOT NULL,
    mechanism_scope_json TEXT NOT NULL,
    intervention_scope_json TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE,
    UNIQUE(family_id,claim_hash)
);
CREATE TABLE IF NOT EXISTS research_hypothesis_resolutions (
    resolution_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    dataset_fingerprints_json TEXT NOT NULL,
    regime_keys_json TEXT NOT NULL,
    time_blocks_json TEXT NOT NULL,
    selection_family_id TEXT NOT NULL,
    familywise_alpha_spent REAL NOT NULL,
    reason TEXT NOT NULL,
    resolved_ts_utc TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS research_hypothesis_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    previous_event_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_ts_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_hypothesis_integrity_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    event_count INTEGER NOT NULL,
    head_event_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS research_hypotheses_no_update
BEFORE UPDATE ON research_hypotheses
BEGIN SELECT RAISE(ABORT,'research_hypotheses_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_hypotheses_no_delete
BEFORE DELETE ON research_hypotheses
BEGIN SELECT RAISE(ABORT,'research_hypotheses_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_hypothesis_resolutions_no_update
BEFORE UPDATE ON research_hypothesis_resolutions
BEGIN SELECT RAISE(ABORT,'research_hypothesis_resolutions_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_hypothesis_resolutions_no_delete
BEFORE DELETE ON research_hypothesis_resolutions
BEGIN SELECT RAISE(ABORT,'research_hypothesis_resolutions_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_hypothesis_events_no_update
BEFORE UPDATE ON research_hypothesis_events
BEGIN SELECT RAISE(ABORT,'research_hypothesis_events_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_hypothesis_events_no_delete
BEFORE DELETE ON research_hypothesis_events
BEGIN SELECT RAISE(ABORT,'research_hypothesis_events_append_only'); END;
"""


def _json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(payload: object) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _normalize_claim(claim: str) -> str:
    normalized = " ".join(claim.strip().lower().split())
    if not normalized:
        raise ValueError("canonical_claim is required")
    return normalized


def _normalize_strings(
    values: tuple[str, ...],
    *,
    name: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    normalized = tuple(sorted({item.strip() for item in values if item.strip()}))
    if not allow_empty and not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


def _hypothesis_payload(item: ResearchHypothesis) -> dict[str, object]:
    return {
        "version": "research-hypothesis-v1",
        "family_id": item.family_id,
        "parent_hypothesis_id": item.parent_hypothesis_id,
        "generation": item.generation,
        "canonical_claim": item.canonical_claim,
        "claim_hash": item.claim_hash,
        "mechanism_scope": list(item.mechanism_scope),
        "intervention_scope": list(item.intervention_scope),
        "created_ts_utc": item.created_ts_utc.astimezone(UTC).isoformat(),
    }


def _resolution_payload(item: HypothesisResolution) -> dict[str, object]:
    return {
        "version": "hypothesis-resolution-v1",
        "hypothesis_id": item.hypothesis_id,
        "status": item.status.value,
        "evidence_ids": list(item.evidence_ids),
        "dataset_fingerprints": list(item.dataset_fingerprints),
        "regime_keys": list(item.regime_keys),
        "time_blocks": list(item.time_blocks),
        "selection_family_id": item.selection_family_id,
        "familywise_alpha_spent": round(item.familywise_alpha_spent, 12),
        "reason": item.reason,
        "resolved_ts_utc": item.resolved_ts_utc.astimezone(UTC).isoformat(),
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
        "SELECT sequence,event_hash FROM research_hypothesis_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(previous["sequence"]) + 1 if previous is not None else 1
    previous_hash = str(previous["event_hash"]) if previous is not None else _GENESIS
    event_hash = _hash(
        {
            "version": "research-hypothesis-event-v1",
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
        INSERT INTO research_hypothesis_events
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
        "SELECT event_count,chain_hash FROM research_hypothesis_integrity_state WHERE singleton_id=1"
    ).fetchone()
    count = int(checkpoint["event_count"]) if checkpoint is not None else 0
    chain = str(checkpoint["chain_hash"]) if checkpoint is not None else _GENESIS
    chain = hashlib.sha256(f"{chain}|{event_hash}".encode()).hexdigest()
    db.execute(
        """
        INSERT INTO research_hypothesis_integrity_state
        (singleton_id,event_count,head_event_hash,chain_hash) VALUES (1,?,?,?)
        ON CONFLICT(singleton_id) DO UPDATE SET
            event_count=excluded.event_count,
            head_event_hash=excluded.head_event_hash,
            chain_hash=excluded.chain_hash
        """,
        (count + 1, event_hash, chain),
    )


def register_research_hypothesis(
    path: str | Path,
    *,
    family_id: str,
    canonical_claim: str,
    mechanism_scope: tuple[str, ...],
    intervention_scope: tuple[str, ...],
    parent_hypothesis_id: str = "",
    created_ts_utc: datetime | None = None,
) -> ResearchHypothesis:
    if not family_id.strip():
        raise ValueError("family_id is required")
    claim = _normalize_claim(canonical_claim)
    mechanisms = _normalize_strings(mechanism_scope, name="mechanism_scope")
    interventions = _normalize_strings(intervention_scope, name="intervention_scope")
    created = (created_ts_utc or datetime.now(UTC)).astimezone(UTC)
    claim_hash = _hash({"version": "hypothesis-claim-v1", "canonical_claim": claim})
    db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        duplicate = db.execute(
            "SELECT hypothesis_id FROM research_hypotheses WHERE family_id=? AND claim_hash=?",
            (family_id, claim_hash),
        ).fetchone()
        if duplicate is not None:
            raise ValueError("hypothesis claim already registered in family")
        generation = 0
        if parent_hypothesis_id:
            parent = db.execute(
                "SELECT family_id,generation FROM research_hypotheses WHERE hypothesis_id=?",
                (parent_hypothesis_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("parent hypothesis does not exist")
            if str(parent["family_id"]) != family_id:
                raise ValueError("parent hypothesis belongs to a different family")
            generation = int(parent["generation"]) + 1
        provisional = ResearchHypothesis(
            hypothesis_id="",
            family_id=family_id,
            parent_hypothesis_id=parent_hypothesis_id,
            generation=generation,
            canonical_claim=claim,
            claim_hash=claim_hash,
            mechanism_scope=mechanisms,
            intervention_scope=interventions,
            created_ts_utc=created,
            record_hash="",
        )
        payload = _hypothesis_payload(provisional)
        hypothesis_id = _hash({"version": "research-hypothesis-id-v1", "payload": payload})
        record_hash = _hash(payload)
        db.execute(
            """
            INSERT INTO research_hypotheses
            (hypothesis_id,family_id,parent_hypothesis_id,generation,canonical_claim,claim_hash,
             mechanism_scope_json,intervention_scope_json,created_ts_utc,record_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                hypothesis_id,
                family_id,
                parent_hypothesis_id,
                generation,
                claim,
                claim_hash,
                _json(list(mechanisms)),
                _json(list(interventions)),
                created.isoformat(),
                record_hash,
            ),
        )
        _append_event(
            db,
            event_kind="HYPOTHESIS",
            record_id=hypothesis_id,
            record_hash=record_hash,
            created_ts_utc=created,
        )
        db.execute("COMMIT")
        return ResearchHypothesis(
            hypothesis_id=hypothesis_id,
            family_id=family_id,
            parent_hypothesis_id=parent_hypothesis_id,
            generation=generation,
            canonical_claim=claim,
            claim_hash=claim_hash,
            mechanism_scope=mechanisms,
            intervention_scope=interventions,
            created_ts_utc=created,
            record_hash=record_hash,
        )
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def resolve_research_hypothesis(
    path: str | Path,
    *,
    hypothesis_id: str,
    status: HypothesisStatus,
    evidence_ids: tuple[str, ...],
    dataset_fingerprints: tuple[str, ...],
    regime_keys: tuple[str, ...],
    time_blocks: tuple[str, ...],
    selection_family_id: str,
    familywise_alpha_spent: float,
    reason: str,
    resolved_ts_utc: datetime | None = None,
    policy: HypothesisMemoryPolicy | None = None,
) -> HypothesisResolution:
    policy = policy or HypothesisMemoryPolicy()
    if status in {HypothesisStatus.PROPOSED, HypothesisStatus.TESTING}:
        raise ValueError("resolution status must be terminal or evidential")
    if not hypothesis_id.strip() or not selection_family_id.strip() or not reason.strip():
        raise ValueError("hypothesis_id, selection_family_id, and reason are required")
    if not math.isfinite(familywise_alpha_spent) or familywise_alpha_spent < 0:
        raise ValueError("familywise_alpha_spent must be finite and non-negative")
    evidence = _normalize_strings(evidence_ids, name="evidence_ids")
    datasets = _normalize_strings(dataset_fingerprints, name="dataset_fingerprints")
    regimes = _normalize_strings(regime_keys, name="regime_keys")
    blocks = _normalize_strings(time_blocks, name="time_blocks")
    resolved = (resolved_ts_utc or datetime.now(UTC)).astimezone(UTC)
    db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        hypothesis = db.execute(
            "SELECT hypothesis_id FROM research_hypotheses WHERE hypothesis_id=?",
            (hypothesis_id,),
        ).fetchone()
        if hypothesis is None:
            raise ValueError("hypothesis does not exist")
        alpha_row = db.execute(
            """
            SELECT COALESCE(SUM(familywise_alpha_spent),0) AS alpha
            FROM research_hypothesis_resolutions WHERE selection_family_id=?
            """,
            (selection_family_id,),
        ).fetchone()
        alpha_used = float(alpha_row["alpha"])
        if alpha_used + familywise_alpha_spent > policy.familywise_alpha_budget + 1e-12:
            raise ValueError("hypothesis familywise alpha budget exhausted")
        provisional = HypothesisResolution(
            resolution_id="",
            hypothesis_id=hypothesis_id,
            status=status,
            evidence_ids=evidence,
            dataset_fingerprints=datasets,
            regime_keys=regimes,
            time_blocks=blocks,
            selection_family_id=selection_family_id,
            familywise_alpha_spent=familywise_alpha_spent,
            reason=reason,
            resolved_ts_utc=resolved,
            record_hash="",
        )
        payload = _resolution_payload(provisional)
        resolution_id = _hash({"version": "hypothesis-resolution-id-v1", "payload": payload})
        record_hash = _hash(payload)
        db.execute(
            """
            INSERT INTO research_hypothesis_resolutions
            (resolution_id,hypothesis_id,status,evidence_ids_json,dataset_fingerprints_json,
             regime_keys_json,time_blocks_json,selection_family_id,familywise_alpha_spent,
             reason,resolved_ts_utc,record_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                resolution_id,
                hypothesis_id,
                status.value,
                _json(list(evidence)),
                _json(list(datasets)),
                _json(list(regimes)),
                _json(list(blocks)),
                selection_family_id,
                familywise_alpha_spent,
                reason,
                resolved.isoformat(),
                record_hash,
            ),
        )
        _append_event(
            db,
            event_kind="RESOLUTION",
            record_id=resolution_id,
            record_hash=record_hash,
            created_ts_utc=resolved,
        )
        db.execute("COMMIT")
        return HypothesisResolution(
            resolution_id=resolution_id,
            hypothesis_id=hypothesis_id,
            status=status,
            evidence_ids=evidence,
            dataset_fingerprints=datasets,
            regime_keys=regimes,
            time_blocks=blocks,
            selection_family_id=selection_family_id,
            familywise_alpha_spent=familywise_alpha_spent,
            reason=reason,
            resolved_ts_utc=resolved,
            record_hash=record_hash,
        )
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def evaluate_hypothesis_reuse(
    path: str | Path,
    *,
    family_id: str,
    canonical_claim: str,
    current_regime: str,
    current_dataset_fingerprint: str,
    as_of_ts_utc: datetime,
    policy: HypothesisMemoryPolicy | None = None,
) -> HypothesisReuseDecision:
    policy = policy or HypothesisMemoryPolicy()
    claim_hash = _hash(
        {"version": "hypothesis-claim-v1", "canonical_claim": _normalize_claim(canonical_claim)}
    )
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        hypothesis = db.execute(
            "SELECT hypothesis_id FROM research_hypotheses WHERE family_id=? AND claim_hash=?",
            (family_id, claim_hash),
        ).fetchone()
        if hypothesis is None:
            return HypothesisReuseDecision(
                action=HypothesisReuseAction.NEW,
                hypothesis_id="",
                reason="hypothesis_claim_not_seen",
                covered_regimes=(),
                covered_datasets=(),
                last_status=None,
                last_resolved_ts_utc=None,
            )
        hypothesis_id = str(hypothesis["hypothesis_id"])
        resolutions = db.execute(
            """
            SELECT * FROM research_hypothesis_resolutions
            WHERE hypothesis_id=? ORDER BY resolved_ts_utc,resolution_id
            """,
            (hypothesis_id,),
        ).fetchall()
    covered_regimes: set[str] = set()
    covered_datasets: set[str] = set()
    for row in resolutions:
        covered_regimes.update(str(item) for item in json.loads(str(row["regime_keys_json"])))
        covered_datasets.update(
            str(item) for item in json.loads(str(row["dataset_fingerprints_json"]))
        )
    if not resolutions:
        return HypothesisReuseDecision(
            action=HypothesisReuseAction.DO_NOT_REPEAT,
            hypothesis_id=hypothesis_id,
            reason="hypothesis_already_registered_without_resolution",
            covered_regimes=tuple(sorted(covered_regimes)),
            covered_datasets=tuple(sorted(covered_datasets)),
            last_status=None,
            last_resolved_ts_utc=None,
        )
    last = resolutions[-1]
    last_status = HypothesisStatus(str(last["status"]))
    last_ts = datetime.fromisoformat(str(last["resolved_ts_utc"])).astimezone(UTC)
    if current_regime not in covered_regimes or current_dataset_fingerprint not in covered_datasets:
        return HypothesisReuseDecision(
            action=HypothesisReuseAction.REOPEN_CONTEXT_SHIFT,
            hypothesis_id=hypothesis_id,
            reason="material_context_not_previously_tested",
            covered_regimes=tuple(sorted(covered_regimes)),
            covered_datasets=tuple(sorted(covered_datasets)),
            last_status=last_status,
            last_resolved_ts_utc=last_ts,
        )
    if (
        policy.allow_temporal_revalidation
        and as_of_ts_utc.astimezone(UTC) >= last_ts + policy.temporal_revalidation_after
    ):
        return HypothesisReuseDecision(
            action=HypothesisReuseAction.REOPEN_TEMPORAL_REVALIDATION,
            hypothesis_id=hypothesis_id,
            reason="evidence_stale_enough_for_temporal_revalidation",
            covered_regimes=tuple(sorted(covered_regimes)),
            covered_datasets=tuple(sorted(covered_datasets)),
            last_status=last_status,
            last_resolved_ts_utc=last_ts,
        )
    return HypothesisReuseDecision(
        action=HypothesisReuseAction.DO_NOT_REPEAT,
        hypothesis_id=hypothesis_id,
        reason="claim_already_tested_in_current_context",
        covered_regimes=tuple(sorted(covered_regimes)),
        covered_datasets=tuple(sorted(covered_datasets)),
        last_status=last_status,
        last_resolved_ts_utc=last_ts,
    )


def verify_hypothesis_memory(path: str | Path) -> HypothesisMemoryVerification:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        hypotheses = db.execute("SELECT * FROM research_hypotheses").fetchall()
        resolutions = db.execute("SELECT * FROM research_hypothesis_resolutions").fetchall()
        events = db.execute("SELECT * FROM research_hypothesis_events ORDER BY sequence").fetchall()
        checkpoint = db.execute(
            "SELECT * FROM research_hypothesis_integrity_state WHERE singleton_id=1"
        ).fetchone()
    failures: list[str] = []
    records: dict[str, str] = {}
    for row in hypotheses:
        mechanism_scope = tuple(
            str(value) for value in json.loads(str(row["mechanism_scope_json"]))
        )
        intervention_scope = tuple(
            str(value) for value in json.loads(str(row["intervention_scope_json"]))
        )
        hypothesis_item = ResearchHypothesis(
            hypothesis_id=str(row["hypothesis_id"]),
            family_id=str(row["family_id"]),
            parent_hypothesis_id=str(row["parent_hypothesis_id"]),
            generation=int(row["generation"]),
            canonical_claim=str(row["canonical_claim"]),
            claim_hash=str(row["claim_hash"]),
            mechanism_scope=mechanism_scope,
            intervention_scope=intervention_scope,
            created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC),
            record_hash=str(row["record_hash"]),
        )
        expected_claim = _hash(
            {
                "version": "hypothesis-claim-v1",
                "canonical_claim": hypothesis_item.canonical_claim,
            }
        )
        if hypothesis_item.claim_hash != expected_claim:
            failures.append("hypothesis_claim_hash_mismatch")
        hypothesis_payload = _hypothesis_payload(hypothesis_item)
        if hypothesis_item.record_hash != _hash(hypothesis_payload):
            failures.append("hypothesis_record_hash_mismatch")
        expected_hypothesis_id = _hash(
            {"version": "research-hypothesis-id-v1", "payload": hypothesis_payload}
        )
        if hypothesis_item.hypothesis_id != expected_hypothesis_id:
            failures.append("hypothesis_id_mismatch")
        records[hypothesis_item.hypothesis_id] = hypothesis_item.record_hash
    hypothesis_ids = set(records)
    for row in resolutions:
        resolution_item = HypothesisResolution(
            resolution_id=str(row["resolution_id"]),
            hypothesis_id=str(row["hypothesis_id"]),
            status=HypothesisStatus(str(row["status"])),
            evidence_ids=tuple(
                str(value) for value in json.loads(str(row["evidence_ids_json"]))
            ),
            dataset_fingerprints=tuple(
                str(value) for value in json.loads(str(row["dataset_fingerprints_json"]))
            ),
            regime_keys=tuple(
                str(value) for value in json.loads(str(row["regime_keys_json"]))
            ),
            time_blocks=tuple(
                str(value) for value in json.loads(str(row["time_blocks_json"]))
            ),
            selection_family_id=str(row["selection_family_id"]),
            familywise_alpha_spent=float(row["familywise_alpha_spent"]),
            reason=str(row["reason"]),
            resolved_ts_utc=datetime.fromisoformat(str(row["resolved_ts_utc"])).astimezone(UTC),
            record_hash=str(row["record_hash"]),
        )
        if resolution_item.hypothesis_id not in hypothesis_ids:
            failures.append("hypothesis_resolution_parent_missing")
        resolution_payload = _resolution_payload(resolution_item)
        if resolution_item.record_hash != _hash(resolution_payload):
            failures.append("hypothesis_resolution_record_hash_mismatch")
        expected_resolution_id = _hash(
            {"version": "hypothesis-resolution-id-v1", "payload": resolution_payload}
        )
        if resolution_item.resolution_id != expected_resolution_id:
            failures.append("hypothesis_resolution_id_mismatch")
        records[resolution_item.resolution_id] = resolution_item.record_hash
    previous = _GENESIS
    chain = _GENESIS
    covered: set[str] = set()
    for expected_sequence, row in enumerate(events, start=1):
        sequence = int(row["sequence"])
        if sequence != expected_sequence:
            failures.append("hypothesis_event_sequence_gap")
        if str(row["previous_event_hash"]) != previous:
            failures.append("hypothesis_event_parent_mismatch")
        record_id = str(row["record_id"])
        record_hash = str(row["record_hash"])
        if records.get(record_id) != record_hash:
            failures.append("hypothesis_event_record_mismatch")
        created = datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC)
        expected_hash = _hash(
            {
                "version": "research-hypothesis-event-v1",
                "sequence": sequence,
                "event_kind": str(row["event_kind"]),
                "record_id": record_id,
                "record_hash": record_hash,
                "previous_event_hash": str(row["previous_event_hash"]),
                "created_ts_utc": created.isoformat(),
            }
        )
        if str(row["event_hash"]) != expected_hash:
            failures.append("hypothesis_event_hash_mismatch")
        covered.add(record_id)
        previous = str(row["event_hash"])
        chain = hashlib.sha256(f"{chain}|{row['event_hash']}".encode()).hexdigest()
    if covered != set(records):
        failures.append("hypothesis_event_coverage_mismatch")
    checkpoint_valid = checkpoint is not None and (
        int(checkpoint["event_count"]) == len(events)
        and str(checkpoint["head_event_hash"]) == previous
        and str(checkpoint["chain_hash"]) == chain
    )
    if not checkpoint_valid:
        failures.append("hypothesis_integrity_checkpoint_mismatch")
    unique = tuple(dict.fromkeys(failures))
    return HypothesisMemoryVerification(
        hypotheses=len(hypotheses),
        resolutions=len(resolutions),
        events=len(events),
        head_event_hash=previous,
        chain_hash=chain,
        valid=not unique,
        failures=unique,
    )
