from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .adaptive_ensemble import AdaptiveEnsembleSnapshot, fingerprint_adaptive_snapshot
from .feature_stability import FeatureStabilityReport
from .regime_mixture import RegimeMixtureReport


class EvolutionTrigger(StrEnum):
    SCHEDULED = "SCHEDULED"
    NEW_DATA = "NEW_DATA"
    DRIFT = "DRIFT"
    CALIBRATION_DECAY = "CALIBRATION_DECAY"
    REGIME_CHANGE = "REGIME_CHANGE"
    MODEL_DEGRADATION = "MODEL_DEGRADATION"


class EvolutionCandidateStatus(StrEnum):
    RESEARCH_CANDIDATE = "RESEARCH_CANDIDATE"
    BLOCKED_EVIDENCE = "BLOCKED_EVIDENCE"


@dataclass(frozen=True, slots=True)
class EvolutionCandidate:
    candidate_id: str
    parent_release_id: str
    trigger: EvolutionTrigger
    code_revision: str
    policy_fingerprint: str
    dataset_fingerprint: str
    feature_set_version: str
    stable_features: tuple[str, ...]
    ensemble_snapshot_hash: str
    model_weights: tuple[tuple[str, float], ...]
    regimes: tuple[str, ...]
    adaptive_updates: int
    status: EvolutionCandidateStatus
    failures: tuple[str, ...]
    created_ts_utc: datetime

    @property
    def ready_for_research(self) -> bool:
        return self.status is EvolutionCandidateStatus.RESEARCH_CANDIDATE


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_candidates (
    candidate_id TEXT PRIMARY KEY,
    parent_release_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    code_revision TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    dataset_fingerprint TEXT NOT NULL,
    feature_set_version TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    candidate_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evolution_candidates_created
ON evolution_candidates(created_ts_utc, candidate_id);
"""


def _canonical_payload(
    *,
    parent_release_id: str,
    trigger: EvolutionTrigger,
    code_revision: str,
    policy_fingerprint: str,
    dataset_fingerprint: str,
    feature_set_version: str,
    stable_features: tuple[str, ...],
    ensemble_snapshot_hash: str,
    model_weights: tuple[tuple[str, float], ...],
    regimes: tuple[str, ...],
    adaptive_updates: int,
    status: EvolutionCandidateStatus,
    failures: tuple[str, ...],
) -> dict[str, object]:
    return {
        "parent_release_id": parent_release_id,
        "trigger": trigger.value,
        "code_revision": code_revision,
        "policy_fingerprint": policy_fingerprint,
        "dataset_fingerprint": dataset_fingerprint,
        "feature_set_version": feature_set_version,
        "stable_features": list(stable_features),
        "ensemble_snapshot_hash": ensemble_snapshot_hash,
        "model_weights": [[model_id, round(weight, 12)] for model_id, weight in model_weights],
        "regimes": list(regimes),
        "adaptive_updates": adaptive_updates,
        "status": status.value,
        "failures": list(failures),
    }


def _hash_payload(payload: dict[str, object]) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_evolution_candidate(
    *,
    parent_release_id: str,
    trigger: EvolutionTrigger,
    code_revision: str,
    policy_fingerprint: str,
    dataset_fingerprint: str,
    feature_set_version: str,
    feature_stability: FeatureStabilityReport,
    adaptive_ensemble: AdaptiveEnsembleSnapshot,
    regime_mixture: RegimeMixtureReport,
) -> EvolutionCandidate:
    """Assemble an immutable *research* challenger from already-validated evidence.

    This function cannot activate or deploy the candidate. A blocked candidate is still useful:
    it records exactly which evidence prevented the current self-evolution cycle from advancing.
    """
    for name, value in (
        ("parent_release_id", parent_release_id),
        ("code_revision", code_revision),
        ("policy_fingerprint", policy_fingerprint),
        ("dataset_fingerprint", dataset_fingerprint),
        ("feature_set_version", feature_set_version),
    ):
        if not value.strip():
            raise ValueError(f"{name} is required")

    failures: list[str] = []
    if not feature_stability.qualified:
        failures.append("feature_stability_not_qualified")
        failures.extend(f"feature:{item}" for item in feature_stability.failures)
    if not feature_stability.qualified_features:
        failures.append("no_qualified_features")
    if not adaptive_ensemble.trusted:
        failures.append("adaptive_ensemble_not_trusted")
        if adaptive_ensemble.drift_active:
            failures.append("adaptive_ensemble_frozen_by_drift")
        failures.extend(f"ensemble:{item}" for item in adaptive_ensemble.failures)
    if not regime_mixture.qualified:
        failures.append("regime_mixture_not_qualified")
        failures.extend(f"regime:{item}" for item in regime_mixture.failures)

    status = (
        EvolutionCandidateStatus.RESEARCH_CANDIDATE
        if not failures
        else EvolutionCandidateStatus.BLOCKED_EVIDENCE
    )
    stable_features = tuple(sorted(feature_stability.qualified_features))
    model_weights = tuple(
        sorted((item.model_id, item.weight) for item in adaptive_ensemble.weights)
    )
    regimes = tuple(sorted(regime_mixture.regimes))
    ensemble_hash = fingerprint_adaptive_snapshot(adaptive_ensemble)
    failure_tuple = tuple(failures)
    payload = _canonical_payload(
        parent_release_id=parent_release_id,
        trigger=trigger,
        code_revision=code_revision,
        policy_fingerprint=policy_fingerprint,
        dataset_fingerprint=dataset_fingerprint,
        feature_set_version=feature_set_version,
        stable_features=stable_features,
        ensemble_snapshot_hash=ensemble_hash,
        model_weights=model_weights,
        regimes=regimes,
        adaptive_updates=adaptive_ensemble.updates,
        status=status,
        failures=failure_tuple,
    )
    candidate_id = _hash_payload(payload)
    return EvolutionCandidate(
        candidate_id=candidate_id,
        parent_release_id=parent_release_id,
        trigger=trigger,
        code_revision=code_revision,
        policy_fingerprint=policy_fingerprint,
        dataset_fingerprint=dataset_fingerprint,
        feature_set_version=feature_set_version,
        stable_features=stable_features,
        ensemble_snapshot_hash=ensemble_hash,
        model_weights=model_weights,
        regimes=regimes,
        adaptive_updates=adaptive_ensemble.updates,
        status=status,
        failures=failure_tuple,
        created_ts_utc=datetime.now(UTC),
    )


def persist_evolution_candidate(path: str | Path, candidate: EvolutionCandidate) -> bool:
    payload = _canonical_payload(
        parent_release_id=candidate.parent_release_id,
        trigger=candidate.trigger,
        code_revision=candidate.code_revision,
        policy_fingerprint=candidate.policy_fingerprint,
        dataset_fingerprint=candidate.dataset_fingerprint,
        feature_set_version=candidate.feature_set_version,
        stable_features=candidate.stable_features,
        ensemble_snapshot_hash=candidate.ensemble_snapshot_hash,
        model_weights=candidate.model_weights,
        regimes=candidate.regimes,
        adaptive_updates=candidate.adaptive_updates,
        status=candidate.status,
        failures=candidate.failures,
    )
    expected = _hash_payload(payload)
    if expected != candidate.candidate_id:
        raise ValueError("evolution candidate identity does not match canonical evidence")
    encoded = json.dumps(asdict(candidate), sort_keys=True, default=str, separators=(",", ":"))
    with sqlite3.connect(str(path)) as db:
        db.executescript(_SCHEMA)
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO evolution_candidates
            (candidate_id,parent_release_id,trigger,code_revision,policy_fingerprint,
             dataset_fingerprint,feature_set_version,candidate_json,candidate_sha256,status,
             created_ts_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                candidate.candidate_id,
                candidate.parent_release_id,
                candidate.trigger.value,
                candidate.code_revision,
                candidate.policy_fingerprint,
                candidate.dataset_fingerprint,
                candidate.feature_set_version,
                encoded,
                expected,
                candidate.status.value,
                candidate.created_ts_utc.isoformat(),
            ),
        )
        return cursor.rowcount == 1


def list_evolution_candidates(path: str | Path) -> tuple[dict[str, object], ...]:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        rows = db.execute(
            """
            SELECT candidate_id,parent_release_id,trigger,status,code_revision,
                   policy_fingerprint,dataset_fingerprint,feature_set_version,created_ts_utc
            FROM evolution_candidates
            ORDER BY created_ts_utc,candidate_id
            """
        ).fetchall()
    return tuple(
        {
            "candidate_id": str(row["candidate_id"]),
            "parent_release_id": str(row["parent_release_id"]),
            "trigger": str(row["trigger"]),
            "status": str(row["status"]),
            "code_revision": str(row["code_revision"]),
            "policy_fingerprint": str(row["policy_fingerprint"]),
            "dataset_fingerprint": str(row["dataset_fingerprint"]),
            "feature_set_version": str(row["feature_set_version"]),
            "created_ts_utc": str(row["created_ts_utc"]),
        }
        for row in rows
    )
