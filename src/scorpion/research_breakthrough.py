from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class BreakthroughStatus(StrEnum):
    INSUFFICIENT = "INSUFFICIENT"
    PROMISING = "PROMISING"
    BREAKTHROUGH_CANDIDATE = "BREAKTHROUGH_CANDIDATE"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class BreakthroughEvidence:
    hypothesis_id: str
    experiment_id: str
    dataset_fingerprint: str
    truth_snapshot_hash: str
    counterfactual_report_hash: str
    time_block: str
    regime_key: str
    effect_size: float
    simultaneous_lower_bound: float
    selection_adjusted: bool
    safety_regression: bool
    observed_ts_utc: datetime

    def __post_init__(self) -> None:
        for name in (
            "hypothesis_id",
            "experiment_id",
            "dataset_fingerprint",
            "truth_snapshot_hash",
            "counterfactual_report_hash",
            "time_block",
            "regime_key",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if not math.isfinite(self.effect_size) or not math.isfinite(self.simultaneous_lower_bound):
            raise ValueError("breakthrough effect evidence must be finite")
        if self.observed_ts_utc.tzinfo is None or self.observed_ts_utc.utcoffset() is None:
            raise ValueError("observed_ts_utc must be timezone-aware")


@dataclass(frozen=True, slots=True)
class BreakthroughPolicy:
    minimum_replications: int = 3
    minimum_independent_datasets: int = 2
    minimum_time_blocks: int = 2
    minimum_regimes: int = 2
    minimum_median_effect_size: float = 0.02
    minimum_replication_lower_bound: float = 0.0
    require_selection_adjustment: bool = True
    maximum_safety_regressions: int = 0

    def __post_init__(self) -> None:
        if min(
            self.minimum_replications,
            self.minimum_independent_datasets,
            self.minimum_time_blocks,
            self.minimum_regimes,
        ) <= 0:
            raise ValueError("breakthrough replication thresholds must be positive")
        if not math.isfinite(self.minimum_median_effect_size):
            raise ValueError("minimum_median_effect_size must be finite")
        if not math.isfinite(self.minimum_replication_lower_bound):
            raise ValueError("minimum_replication_lower_bound must be finite")
        if self.maximum_safety_regressions < 0:
            raise ValueError("maximum_safety_regressions cannot be negative")


@dataclass(frozen=True, slots=True)
class BreakthroughReport:
    hypothesis_id: str
    status: BreakthroughStatus
    replications: int
    independent_datasets: int
    time_blocks: int
    regimes: int
    median_effect_size: float
    worst_simultaneous_lower_bound: float
    evidence_ids: tuple[str, ...]
    report_hash: str
    failures: tuple[str, ...]

    @property
    def breakthrough_candidate(self) -> bool:
        return self.status is BreakthroughStatus.BREAKTHROUGH_CANDIDATE


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_breakthrough_candidates (
    report_hash TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL,
    status TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS research_breakthrough_candidates_no_update
BEFORE UPDATE ON research_breakthrough_candidates
BEGIN SELECT RAISE(ABORT,'research_breakthrough_candidates_append_only'); END;
CREATE TRIGGER IF NOT EXISTS research_breakthrough_candidates_no_delete
BEFORE DELETE ON research_breakthrough_candidates
BEGIN SELECT RAISE(ABORT,'research_breakthrough_candidates_append_only'); END;
"""


def _hash(payload: object) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode()).hexdigest()


def _evidence_id(item: BreakthroughEvidence) -> str:
    return _hash(
        {
            "version": "breakthrough-evidence-v1",
            "hypothesis_id": item.hypothesis_id,
            "experiment_id": item.experiment_id,
            "dataset_fingerprint": item.dataset_fingerprint,
            "truth_snapshot_hash": item.truth_snapshot_hash,
            "counterfactual_report_hash": item.counterfactual_report_hash,
            "time_block": item.time_block,
            "regime_key": item.regime_key,
            "effect_size": round(item.effect_size, 12),
            "simultaneous_lower_bound": round(item.simultaneous_lower_bound, 12),
            "selection_adjusted": item.selection_adjusted,
            "safety_regression": item.safety_regression,
            "observed_ts_utc": item.observed_ts_utc.astimezone(UTC).isoformat(),
        }
    )


def evaluate_breakthrough_candidate(
    evidence: tuple[BreakthroughEvidence, ...],
    *,
    policy: BreakthroughPolicy | None = None,
) -> BreakthroughReport:
    """Require independent, selection-adjusted replication before emitting a research breakthrough."""
    policy = policy or BreakthroughPolicy()
    if not evidence:
        return _report("", BreakthroughStatus.INSUFFICIENT, (), policy, ("no_evidence",))
    hypothesis_ids = {item.hypothesis_id for item in evidence}
    if len(hypothesis_ids) != 1:
        raise ValueError("breakthrough evidence must target one hypothesis")
    if len({item.experiment_id for item in evidence}) != len(evidence):
        raise ValueError("breakthrough experiment ids must be unique")
    hypothesis_id = next(iter(hypothesis_ids))
    failures: list[str] = []
    safety_regressions = sum(item.safety_regression for item in evidence)
    if safety_regressions > policy.maximum_safety_regressions:
        failures.append("breakthrough_safety_regression")
    if policy.require_selection_adjustment and any(not item.selection_adjusted for item in evidence):
        failures.append("breakthrough_unadjusted_selection_evidence")
    if failures:
        return _report(hypothesis_id, BreakthroughStatus.BLOCKED, evidence, policy, tuple(failures))

    replications = len(evidence)
    datasets = len({item.dataset_fingerprint for item in evidence})
    time_blocks = len({item.time_block for item in evidence})
    regimes = len({item.regime_key for item in evidence})
    median_effect = statistics.median(item.effect_size for item in evidence)
    worst_lower = min(item.simultaneous_lower_bound for item in evidence)
    if replications < policy.minimum_replications:
        failures.append("insufficient_breakthrough_replications")
    if datasets < policy.minimum_independent_datasets:
        failures.append("insufficient_breakthrough_dataset_independence")
    if time_blocks < policy.minimum_time_blocks:
        failures.append("insufficient_breakthrough_temporal_replication")
    if regimes < policy.minimum_regimes:
        failures.append("insufficient_breakthrough_regime_breadth")
    if median_effect < policy.minimum_median_effect_size:
        failures.append("breakthrough_effect_below_floor")
    if worst_lower <= policy.minimum_replication_lower_bound:
        failures.append("breakthrough_replication_lower_bound_not_positive")
    if not failures:
        status = BreakthroughStatus.BREAKTHROUGH_CANDIDATE
    elif median_effect >= policy.minimum_median_effect_size and worst_lower > 0:
        status = BreakthroughStatus.PROMISING
    else:
        status = BreakthroughStatus.INSUFFICIENT
    return _report(hypothesis_id, status, evidence, policy, tuple(failures))


def _report(
    hypothesis_id: str,
    status: BreakthroughStatus,
    evidence: tuple[BreakthroughEvidence, ...],
    policy: BreakthroughPolicy,
    failures: tuple[str, ...],
) -> BreakthroughReport:
    evidence_ids = tuple(sorted(_evidence_id(item) for item in evidence))
    replications = len(evidence)
    datasets = len({item.dataset_fingerprint for item in evidence})
    time_blocks = len({item.time_block for item in evidence})
    regimes = len({item.regime_key for item in evidence})
    median_effect = statistics.median(item.effect_size for item in evidence) if evidence else 0.0
    worst_lower = min((item.simultaneous_lower_bound for item in evidence), default=0.0)
    material = {
        "version": "breakthrough-report-v1",
        "hypothesis_id": hypothesis_id,
        "status": status.value,
        "replications": replications,
        "independent_datasets": datasets,
        "time_blocks": time_blocks,
        "regimes": regimes,
        "median_effect_size": round(median_effect, 12),
        "worst_simultaneous_lower_bound": round(worst_lower, 12),
        "evidence_ids": evidence_ids,
        "policy": asdict(policy),
        "failures": failures,
    }
    return BreakthroughReport(
        hypothesis_id=hypothesis_id,
        status=status,
        replications=replications,
        independent_datasets=datasets,
        time_blocks=time_blocks,
        regimes=regimes,
        median_effect_size=median_effect,
        worst_simultaneous_lower_bound=worst_lower,
        evidence_ids=evidence_ids,
        report_hash=_hash(material),
        failures=failures,
    )


def persist_breakthrough_candidate(path: str | Path, report: BreakthroughReport) -> bool:
    if report.status is not BreakthroughStatus.BREAKTHROUGH_CANDIDATE:
        raise ValueError("only replicated breakthrough candidates may be persisted")
    encoded = json.dumps(asdict(report), sort_keys=True, separators=(",", ":"), default=str)
    with sqlite3.connect(str(path)) as db:
        db.executescript(_SCHEMA)
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO research_breakthrough_candidates
            (report_hash,hypothesis_id,status,report_json,created_ts_utc)
            VALUES (?,?,?,?,?)
            """,
            (
                report.report_hash,
                report.hypothesis_id,
                report.status.value,
                encoded,
                datetime.now(UTC).isoformat(),
            ),
        )
        return cursor.rowcount == 1
