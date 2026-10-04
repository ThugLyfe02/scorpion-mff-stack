from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .adaptive_ensemble import (
    AdaptiveEnsemblePolicy,
    AdaptiveEnsembleSnapshot,
    fingerprint_adaptive_snapshot,
    update_adaptive_ensemble,
)
from .domain import EventKind

_GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS adaptive_learning_journal (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    ensemble_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    truth TEXT NOT NULL,
    drift_active INTEGER NOT NULL,
    probabilities_json TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    before_snapshot_hash TEXT NOT NULL,
    after_snapshot_hash TEXT NOT NULL,
    previous_record_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE,
    created_ts_utc TEXT NOT NULL,
    UNIQUE(ensemble_id,event_id)
);
CREATE INDEX IF NOT EXISTS idx_adaptive_learning_ensemble_sequence
ON adaptive_learning_journal(ensemble_id,sequence);
"""


@dataclass(frozen=True, slots=True)
class LearningJournalVerification:
    ensemble_id: str
    records: int
    chain_valid: bool
    transition_valid: bool
    replay_valid: bool
    final_snapshot_hash: str | None
    failures: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return self.chain_valid and self.transition_valid and self.replay_valid


def _probabilities_payload(
    model_probabilities: Mapping[str, Mapping[EventKind, float]],
) -> dict[str, dict[str, float]]:
    payload: dict[str, dict[str, float]] = {}
    for model_id in sorted(model_probabilities):
        payload[model_id] = {
            label.value: model_probabilities[model_id][label]
            for label in sorted(model_probabilities[model_id], key=lambda item: item.value)
        }
    return payload


def _canonical_record_payload(
    *,
    ensemble_id: str,
    event_id: str,
    truth: EventKind,
    drift_active: bool,
    probabilities: dict[str, dict[str, float]],
    policy: AdaptiveEnsemblePolicy,
    before_snapshot_hash: str,
    after_snapshot_hash: str,
    previous_record_hash: str,
) -> dict[str, object]:
    return {
        "ensemble_id": ensemble_id,
        "event_id": event_id,
        "truth": truth.value,
        "drift_active": drift_active,
        "probabilities": probabilities,
        "policy": asdict(policy),
        "before_snapshot_hash": before_snapshot_hash,
        "after_snapshot_hash": after_snapshot_hash,
        "previous_record_hash": previous_record_hash,
    }


def _hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def apply_and_record_adaptive_update(
    path: str | Path,
    *,
    ensemble_id: str,
    event_id: str,
    previous: AdaptiveEnsembleSnapshot | None,
    model_probabilities: Mapping[str, Mapping[EventKind, float]],
    truth: EventKind,
    drift_active: bool = False,
    policy: AdaptiveEnsemblePolicy | None = None,
) -> AdaptiveEnsembleSnapshot:
    """Apply one shadow-learning update and append its exact evidence atomically to the journal."""
    if not ensemble_id.strip() or not event_id.strip():
        raise ValueError("ensemble_id and event_id are required")
    policy = policy or AdaptiveEnsemblePolicy()
    current = update_adaptive_ensemble(
        previous,
        model_probabilities,
        truth,
        drift_active=drift_active,
        policy=policy,
    )
    before_hash = fingerprint_adaptive_snapshot(previous) if previous is not None else _GENESIS
    after_hash = fingerprint_adaptive_snapshot(current)
    probabilities = _probabilities_payload(model_probabilities)
    with sqlite3.connect(str(path)) as db:
        db.executescript(_SCHEMA)
        duplicate = db.execute(
            "SELECT 1 FROM adaptive_learning_journal WHERE ensemble_id=? AND event_id=?",
            (ensemble_id, event_id),
        ).fetchone()
        if duplicate is not None:
            raise ValueError("adaptive learning event already journaled")
        previous_row = db.execute(
            """
            SELECT record_hash,after_snapshot_hash
            FROM adaptive_learning_journal
            WHERE ensemble_id=? ORDER BY sequence DESC LIMIT 1
            """,
            (ensemble_id,),
        ).fetchone()
        previous_record_hash = str(previous_row[0]) if previous_row is not None else _GENESIS
        if previous_row is not None and str(previous_row[1]) != before_hash:
            raise ValueError("adaptive learning before-state does not match journal head")
        if previous_row is None and previous is not None:
            raise ValueError("non-genesis adaptive state cannot start a new learning journal")
        payload = _canonical_record_payload(
            ensemble_id=ensemble_id,
            event_id=event_id,
            truth=truth,
            drift_active=drift_active,
            probabilities=probabilities,
            policy=policy,
            before_snapshot_hash=before_hash,
            after_snapshot_hash=after_hash,
            previous_record_hash=previous_record_hash,
        )
        record_hash = _hash(payload)
        db.execute(
            """
            INSERT INTO adaptive_learning_journal
            (ensemble_id,event_id,truth,drift_active,probabilities_json,policy_json,
             before_snapshot_hash,after_snapshot_hash,previous_record_hash,record_hash,
             created_ts_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ensemble_id,
                event_id,
                truth.value,
                int(drift_active),
                json.dumps(probabilities, sort_keys=True, separators=(",", ":")),
                json.dumps(asdict(policy), sort_keys=True, separators=(",", ":")),
                before_hash,
                after_hash,
                previous_record_hash,
                record_hash,
                datetime.now(UTC).isoformat(),
            ),
        )
    return current


def _decode_probabilities(raw: str) -> dict[str, dict[EventKind, float]]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("learning journal probabilities payload must be an object")
    output: dict[str, dict[EventKind, float]] = {}
    for model_id, distribution in payload.items():
        if not isinstance(distribution, dict):
            raise ValueError("learning journal model distribution must be an object")
        output[str(model_id)] = {
            EventKind(str(label)): float(value) for label, value in distribution.items()
        }
    return output


def verify_learning_journal(path: str | Path, ensemble_id: str) -> LearningJournalVerification:
    if not ensemble_id.strip():
        raise ValueError("ensemble_id is required")
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        rows = db.execute(
            """
            SELECT * FROM adaptive_learning_journal
            WHERE ensemble_id=? ORDER BY sequence
            """,
            (ensemble_id,),
        ).fetchall()
    failures: list[str] = []
    previous_record_hash = _GENESIS
    expected_before_hash = _GENESIS
    replay: AdaptiveEnsembleSnapshot | None = None
    chain_valid = True
    transition_valid = True
    replay_valid = True
    for index, row in enumerate(rows):
        try:
            truth = EventKind(str(row["truth"]))
            probabilities = _decode_probabilities(str(row["probabilities_json"]))
            policy_payload = json.loads(str(row["policy_json"]))
            if not isinstance(policy_payload, dict):
                raise ValueError("policy payload must be an object")
            policy = AdaptiveEnsemblePolicy(**policy_payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"decode_failure:{index}:{type(exc).__name__}")
            chain_valid = transition_valid = replay_valid = False
            continue
        before_hash = str(row["before_snapshot_hash"])
        after_hash = str(row["after_snapshot_hash"])
        if before_hash != expected_before_hash:
            transition_valid = False
            failures.append(f"before_snapshot_mismatch:{index}")
        payload = _canonical_record_payload(
            ensemble_id=ensemble_id,
            event_id=str(row["event_id"]),
            truth=truth,
            drift_active=bool(row["drift_active"]),
            probabilities=_probabilities_payload(probabilities),
            policy=policy,
            before_snapshot_hash=before_hash,
            after_snapshot_hash=after_hash,
            previous_record_hash=str(row["previous_record_hash"]),
        )
        if str(row["previous_record_hash"]) != previous_record_hash:
            chain_valid = False
            failures.append(f"record_parent_mismatch:{index}")
        expected_record_hash = _hash(payload)
        if str(row["record_hash"]) != expected_record_hash:
            chain_valid = False
            failures.append(f"record_hash_mismatch:{index}")
        replay = update_adaptive_ensemble(
            replay,
            probabilities,
            truth,
            drift_active=bool(row["drift_active"]),
            policy=policy,
        )
        replay_hash = fingerprint_adaptive_snapshot(replay)
        if replay_hash != after_hash:
            replay_valid = False
            failures.append(f"replay_snapshot_mismatch:{index}")
        previous_record_hash = str(row["record_hash"])
        expected_before_hash = after_hash
    final_hash = fingerprint_adaptive_snapshot(replay) if replay is not None else None
    return LearningJournalVerification(
        ensemble_id=ensemble_id,
        records=len(rows),
        chain_valid=chain_valid,
        transition_valid=transition_valid,
        replay_valid=replay_valid,
        final_snapshot_hash=final_hash,
        failures=tuple(failures),
    )
