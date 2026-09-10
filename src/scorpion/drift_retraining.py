from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .bayesian_changepoint import BayesianChangePointReport
from .online_drift import DriftSignal


class RetrainingPlanStatus(StrEnum):
    NO_ACTION = "NO_ACTION"
    ACCUMULATING_EVIDENCE = "ACCUMULATING_EVIDENCE"
    COOLDOWN = "COOLDOWN"
    READY_RESEARCH_CHALLENGER = "READY_RESEARCH_CHALLENGER"


@dataclass(frozen=True, slots=True)
class DriftRetrainingPolicy:
    minimum_confirmations: int = 2
    minimum_post_drift_samples: int = 120
    minimum_post_drift_training_samples: int = 80
    minimum_validation_samples: int = 30
    validation_fraction: float = 0.25
    maximum_post_drift_training_samples: int = 2000
    maximum_pre_drift_anchor_samples: int = 500
    changepoint_probability_threshold: float = 0.90
    severe_changepoint_probability: float = 0.995
    cooldown: timedelta = timedelta(hours=6)
    storm_window: timedelta = timedelta(hours=24)
    maximum_plans_per_storm_window: int = 2
    purge_embargo: timedelta = timedelta(minutes=30)

    def __post_init__(self) -> None:
        if self.minimum_confirmations <= 0:
            raise ValueError("minimum_confirmations must be positive")
        if self.minimum_post_drift_samples <= 0:
            raise ValueError("minimum_post_drift_samples must be positive")
        if self.minimum_post_drift_training_samples <= 0:
            raise ValueError("minimum_post_drift_training_samples must be positive")
        if self.minimum_validation_samples <= 0:
            raise ValueError("minimum_validation_samples must be positive")
        if not 0 < self.validation_fraction < 1:
            raise ValueError("validation_fraction must be in (0,1)")
        if self.maximum_post_drift_training_samples < self.minimum_post_drift_training_samples:
            raise ValueError("maximum_post_drift_training_samples is too small")
        if self.maximum_pre_drift_anchor_samples < 0:
            raise ValueError("maximum_pre_drift_anchor_samples cannot be negative")
        if not 0 < self.changepoint_probability_threshold < 1:
            raise ValueError("changepoint_probability_threshold must be in (0,1)")
        if not self.changepoint_probability_threshold <= self.severe_changepoint_probability < 1:
            raise ValueError("severe_changepoint_probability must be >= threshold and <1")
        if self.cooldown < timedelta(0) or self.storm_window <= timedelta(0):
            raise ValueError("retraining time windows are invalid")
        if self.maximum_plans_per_storm_window <= 0:
            raise ValueError("maximum_plans_per_storm_window must be positive")
        if self.purge_embargo < timedelta(0):
            raise ValueError("purge_embargo cannot be negative")


@dataclass(frozen=True, slots=True)
class DriftRetrainingObservation:
    observed_ts_utc: datetime
    drift_started_ts_utc: datetime
    dataset_samples: int
    samples_since_drift: int
    page_hinkley: DriftSignal | None = None
    bayesian_changepoint: BayesianChangePointReport | None = None
    fill_calibration_degraded: bool = False
    robust_residual_hotspots: int = 0

    def __post_init__(self) -> None:
        for name in ("observed_ts_utc", "drift_started_ts_utc"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.drift_started_ts_utc > self.observed_ts_utc:
            raise ValueError("drift_started_ts_utc cannot be in the future")
        if self.dataset_samples < 0 or self.samples_since_drift < 0:
            raise ValueError("sample counts cannot be negative")
        if self.samples_since_drift > self.dataset_samples:
            raise ValueError("samples_since_drift cannot exceed dataset_samples")
        if self.robust_residual_hotspots < 0:
            raise ValueError("robust_residual_hotspots cannot be negative")


@dataclass(frozen=True, slots=True)
class ChallengerRetrainingPlan:
    plan_id: str
    parent_release_id: str
    dataset_fingerprint: str
    status: RetrainingPlanStatus
    confirmations: tuple[str, ...]
    samples_since_drift: int
    post_drift_training_samples: int
    validation_samples: int
    pre_drift_anchor_samples: int
    purge_embargo_seconds: float
    severe_drift: bool
    reasons: tuple[str, ...]
    created_ts_utc: datetime

    @property
    def ready(self) -> bool:
        return self.status is RetrainingPlanStatus.READY_RESEARCH_CHALLENGER


_SCHEMA = """
CREATE TABLE IF NOT EXISTS challenger_retraining_plans (
    plan_id TEXT PRIMARY KEY,
    parent_release_id TEXT NOT NULL,
    dataset_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_challenger_retraining_parent_created
ON challenger_retraining_plans(parent_release_id, created_ts_utc, plan_id);
"""


def _policy_material(policy: DriftRetrainingPolicy) -> dict[str, object]:
    return {
        "minimum_confirmations": policy.minimum_confirmations,
        "minimum_post_drift_samples": policy.minimum_post_drift_samples,
        "minimum_post_drift_training_samples": policy.minimum_post_drift_training_samples,
        "minimum_validation_samples": policy.minimum_validation_samples,
        "validation_fraction": round(policy.validation_fraction, 12),
        "maximum_post_drift_training_samples": policy.maximum_post_drift_training_samples,
        "maximum_pre_drift_anchor_samples": policy.maximum_pre_drift_anchor_samples,
        "changepoint_probability_threshold": round(
            policy.changepoint_probability_threshold, 12
        ),
        "severe_changepoint_probability": round(policy.severe_changepoint_probability, 12),
        "cooldown_seconds": policy.cooldown.total_seconds(),
        "storm_window_seconds": policy.storm_window.total_seconds(),
        "maximum_plans_per_storm_window": policy.maximum_plans_per_storm_window,
        "purge_embargo_seconds": policy.purge_embargo.total_seconds(),
    }


def _confirmations(
    observation: DriftRetrainingObservation,
    policy: DriftRetrainingPolicy,
) -> tuple[tuple[str, ...], bool]:
    confirmations: list[str] = []
    if observation.page_hinkley is not None and observation.page_hinkley.drifted:
        confirmations.append("page_hinkley")
    changepoint = observation.bayesian_changepoint
    severe = False
    if (
        changepoint is not None
        and changepoint.degradation
        and changepoint.posterior_changepoint_probability
        >= policy.changepoint_probability_threshold
    ):
        confirmations.append("bayesian_changepoint")
        severe = (
            changepoint.posterior_changepoint_probability
            >= policy.severe_changepoint_probability
        )
    if observation.fill_calibration_degraded:
        confirmations.append("fill_calibration_decay")
    if observation.robust_residual_hotspots > 0:
        confirmations.append("robust_residual_hotspots")
    return tuple(confirmations), severe


def _canonical_plan_material(
    *,
    parent_release_id: str,
    dataset_fingerprint: str,
    observation: DriftRetrainingObservation,
    policy: DriftRetrainingPolicy,
    status: RetrainingPlanStatus,
    confirmations: tuple[str, ...],
    post_drift_training_samples: int,
    validation_samples: int,
    pre_drift_anchor_samples: int,
    severe_drift: bool,
    reasons: tuple[str, ...],
) -> dict[str, object]:
    return {
        "version": "drift-retraining-v1",
        "parent_release_id": parent_release_id,
        "dataset_fingerprint": dataset_fingerprint,
        "drift_started_ts_utc": observation.drift_started_ts_utc.astimezone(UTC).isoformat(),
        "dataset_samples": observation.dataset_samples,
        "samples_since_drift": observation.samples_since_drift,
        "status": status.value,
        "confirmations": list(confirmations),
        "post_drift_training_samples": post_drift_training_samples,
        "validation_samples": validation_samples,
        "pre_drift_anchor_samples": pre_drift_anchor_samples,
        "severe_drift": severe_drift,
        "reasons": list(reasons),
        "policy": _policy_material(policy),
    }


def _hash_payload(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evaluate_drift_retraining(
    *,
    parent_release_id: str,
    dataset_fingerprint: str,
    observation: DriftRetrainingObservation,
    recent_plan_timestamps: tuple[datetime, ...] = (),
    policy: DriftRetrainingPolicy | None = None,
) -> ChallengerRetrainingPlan:
    """Build a leakage-aware, research-only retraining decision from corroborated drift.

    The function reserves the newest post-drift observations for untouched validation, keeps a
    bounded pre-drift anchor to reduce catastrophic forgetting, and emits a purge embargo that
    the trainer must apply at the train/validation boundary. It never trains, promotes, sizes,
    or deploys a model by itself.
    """
    policy = policy or DriftRetrainingPolicy()
    if not parent_release_id.strip() or not dataset_fingerprint.strip():
        raise ValueError("parent_release_id and dataset_fingerprint are required")
    normalized_recent = tuple(item.astimezone(UTC) for item in recent_plan_timestamps)
    if any(item.tzinfo is None or item.utcoffset() is None for item in recent_plan_timestamps):
        raise ValueError("recent plan timestamps must be timezone-aware")

    confirmations, severe = _confirmations(observation, policy)
    reasons: list[str] = []
    status = RetrainingPlanStatus.NO_ACTION
    post_train = 0
    validation = 0
    anchor = 0

    if not confirmations:
        reasons.append("no_corroborated_degradation_signal")
    elif len(confirmations) < policy.minimum_confirmations and not severe:
        status = RetrainingPlanStatus.ACCUMULATING_EVIDENCE
        reasons.append(
            f"drift_confirmations:{len(confirmations)}<{policy.minimum_confirmations}"
        )
    elif observation.samples_since_drift < policy.minimum_post_drift_samples:
        status = RetrainingPlanStatus.ACCUMULATING_EVIDENCE
        reasons.append(
            "post_drift_samples:"
            f"{observation.samples_since_drift}<{policy.minimum_post_drift_samples}"
        )
    else:
        now = observation.observed_ts_utc.astimezone(UTC)
        storm_cutoff = now - policy.storm_window
        recent = tuple(item for item in normalized_recent if storm_cutoff <= item <= now)
        last = max(recent, default=None)
        if len(recent) >= policy.maximum_plans_per_storm_window:
            status = RetrainingPlanStatus.COOLDOWN
            reasons.append(
                "retraining_storm_guard:"
                f"{len(recent)}>={policy.maximum_plans_per_storm_window}"
            )
        elif last is not None and now < last + policy.cooldown and not severe:
            status = RetrainingPlanStatus.COOLDOWN
            reasons.append("retraining_cooldown_active")
        else:
            validation = max(
                policy.minimum_validation_samples,
                math.ceil(observation.samples_since_drift * policy.validation_fraction),
            )
            validation = min(validation, observation.samples_since_drift - 1)
            available_train = observation.samples_since_drift - validation
            post_train = min(available_train, policy.maximum_post_drift_training_samples)
            if post_train < policy.minimum_post_drift_training_samples:
                status = RetrainingPlanStatus.ACCUMULATING_EVIDENCE
                reasons.append(
                    "post_drift_training_samples:"
                    f"{post_train}<{policy.minimum_post_drift_training_samples}"
                )
            else:
                pre_drift_available = observation.dataset_samples - observation.samples_since_drift
                anchor = min(
                    pre_drift_available,
                    policy.maximum_pre_drift_anchor_samples,
                )
                status = RetrainingPlanStatus.READY_RESEARCH_CHALLENGER
                reasons.extend(
                    (
                        "corroborated_drift_retraining_ready",
                        "chronological_post_drift_holdout_reserved",
                        "train_validation_purge_required",
                    )
                )

    reason_tuple = tuple(reasons)
    material = _canonical_plan_material(
        parent_release_id=parent_release_id,
        dataset_fingerprint=dataset_fingerprint,
        observation=observation,
        policy=policy,
        status=status,
        confirmations=confirmations,
        post_drift_training_samples=post_train,
        validation_samples=validation,
        pre_drift_anchor_samples=anchor,
        severe_drift=severe,
        reasons=reason_tuple,
    )
    return ChallengerRetrainingPlan(
        plan_id=_hash_payload(material),
        parent_release_id=parent_release_id,
        dataset_fingerprint=dataset_fingerprint,
        status=status,
        confirmations=confirmations,
        samples_since_drift=observation.samples_since_drift,
        post_drift_training_samples=post_train,
        validation_samples=validation,
        pre_drift_anchor_samples=anchor,
        purge_embargo_seconds=policy.purge_embargo.total_seconds(),
        severe_drift=severe,
        reasons=reason_tuple,
        created_ts_utc=observation.observed_ts_utc.astimezone(UTC),
    )


def _ensure_schema(path: str | Path) -> None:
    with sqlite3.connect(str(path)) as db:
        db.executescript(_SCHEMA)


def _recent_plan_timestamps(
    path: str | Path,
    *,
    parent_release_id: str,
) -> tuple[datetime, ...]:
    _ensure_schema(path)
    with sqlite3.connect(str(path)) as db:
        rows = db.execute(
            """
            SELECT created_ts_utc
            FROM challenger_retraining_plans
            WHERE parent_release_id=? AND status=?
            ORDER BY created_ts_utc
            """,
            (parent_release_id, RetrainingPlanStatus.READY_RESEARCH_CHALLENGER.value),
        ).fetchall()
    return tuple(datetime.fromisoformat(str(row[0])).astimezone(UTC) for row in rows)


def persist_challenger_retraining_plan(
    path: str | Path,
    plan: ChallengerRetrainingPlan,
) -> bool:
    if not plan.ready:
        raise ValueError("only ready research challenger plans may be persisted")
    payload = {
        "version": "drift-retraining-persisted-v1",
        "plan_id": plan.plan_id,
        "parent_release_id": plan.parent_release_id,
        "dataset_fingerprint": plan.dataset_fingerprint,
        "status": plan.status.value,
        "confirmations": list(plan.confirmations),
        "samples_since_drift": plan.samples_since_drift,
        "post_drift_training_samples": plan.post_drift_training_samples,
        "validation_samples": plan.validation_samples,
        "pre_drift_anchor_samples": plan.pre_drift_anchor_samples,
        "purge_embargo_seconds": plan.purge_embargo_seconds,
        "severe_drift": plan.severe_drift,
        "reasons": list(plan.reasons),
        "created_ts_utc": plan.created_ts_utc.isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    _ensure_schema(path)
    with sqlite3.connect(str(path)) as db:
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO challenger_retraining_plans
            (plan_id,parent_release_id,dataset_fingerprint,status,plan_json,plan_sha256,
             created_ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                plan.plan_id,
                plan.parent_release_id,
                plan.dataset_fingerprint,
                plan.status.value,
                encoded,
                digest,
                plan.created_ts_utc.isoformat(),
            ),
        )
        return cursor.rowcount == 1


def maybe_create_drift_retraining_plan(
    path: str | Path,
    *,
    parent_release_id: str,
    dataset_fingerprint: str,
    observation: DriftRetrainingObservation,
    policy: DriftRetrainingPolicy | None = None,
) -> tuple[ChallengerRetrainingPlan, bool]:
    policy = policy or DriftRetrainingPolicy()
    recent = _recent_plan_timestamps(path, parent_release_id=parent_release_id)
    plan = evaluate_drift_retraining(
        parent_release_id=parent_release_id,
        dataset_fingerprint=dataset_fingerprint,
        observation=observation,
        recent_plan_timestamps=recent,
        policy=policy,
    )
    inserted = persist_challenger_retraining_plan(path, plan) if plan.ready else False
    return plan, inserted


def list_challenger_retraining_plans(path: str | Path) -> tuple[dict[str, object], ...]:
    _ensure_schema(path)
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT plan_id,parent_release_id,dataset_fingerprint,status,plan_json,
                   plan_sha256,created_ts_utc
            FROM challenger_retraining_plans
            ORDER BY created_ts_utc,plan_id
            """
        ).fetchall()
    return tuple(
        {
            "plan_id": str(row["plan_id"]),
            "parent_release_id": str(row["parent_release_id"]),
            "dataset_fingerprint": str(row["dataset_fingerprint"]),
            "status": str(row["status"]),
            "plan": json.loads(str(row["plan_json"])),
            "plan_sha256": str(row["plan_sha256"]),
            "created_ts_utc": str(row["created_ts_utc"]),
        }
        for row in rows
    )
