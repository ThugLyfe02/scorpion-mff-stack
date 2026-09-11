from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .drift_retraining import ChallengerRetrainingPlan
from .trainable_model import (
    DeterminismLevel,
    FittedModel,
    ModelArtifact,
    ModelPrediction,
    TargetValue,
    TrainableModel,
    TrainingExample,
    dataset_fingerprint,
    validate_examples,
)


class MetricDirection(StrEnum):
    MINIMIZE = "MINIMIZE"
    MAXIMIZE = "MAXIMIZE"


class TrainingRunStatus(StrEnum):
    REJECTED_DATA = "REJECTED_DATA"
    FIT_FAILED = "FIT_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    SHADOW_READY = "SHADOW_READY"


@dataclass(frozen=True, slots=True)
class PredictionRecord:
    sample_id: str
    target: TargetValue
    prediction: ModelPrediction


class ValidationMetric(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def direction(self) -> MetricDirection: ...

    @property
    def threshold(self) -> float: ...

    def evaluate(self, predictions: Sequence[PredictionRecord]) -> float: ...


type MetricCallable = Callable[[Sequence[PredictionRecord]], float]


@dataclass(frozen=True, slots=True)
class CallableValidationMetric:
    name: str
    direction: MetricDirection
    threshold: float
    evaluator: MetricCallable

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("metric name is required")
        if not math.isfinite(self.threshold):
            raise ValueError("metric threshold must be finite")

    def evaluate(self, predictions: Sequence[PredictionRecord]) -> float:
        return float(self.evaluator(predictions))


@dataclass(frozen=True, slots=True)
class MetricResult:
    name: str
    value: float
    direction: MetricDirection
    threshold: float
    passed: bool


@dataclass(frozen=True, slots=True)
class TrainingSplitManifest:
    plan_id: str
    dataset_fingerprint: str
    feature_schema_hash: str
    train_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    anchor_ids: tuple[str, ...]
    purged_ids: tuple[str, ...]
    unused_post_drift_ids: tuple[str, ...]
    validation_start_ts_utc: datetime
    purge_cutoff_ts_utc: datetime
    split_hash: str


@dataclass(frozen=True, slots=True)
class AutoTrainerPolicy:
    seed: int = 17
    minimum_training_samples: int = 80
    minimum_validation_samples: int = 30
    maximum_artifact_bytes: int = 16 * 1024 * 1024
    verify_exact_determinism: bool = True

    def __post_init__(self) -> None:
        if self.minimum_training_samples <= 0 or self.minimum_validation_samples <= 0:
            raise ValueError("minimum training and validation samples must be positive")
        if self.maximum_artifact_bytes <= 0:
            raise ValueError("maximum_artifact_bytes must be positive")


@dataclass(frozen=True, slots=True)
class TrainingRunReport:
    run_id: str
    model_id: str
    trainer_version: str
    task: str
    dataset_fingerprint: str
    split_hash: str
    feature_schema_hash: str
    training_samples: int
    validation_samples: int
    artifact_sha256: str
    artifact_loader_key: str
    artifact_model_version: str
    metrics: tuple[MetricResult, ...]
    exact_reproducibility_verified: bool
    status: TrainingRunStatus
    failures: tuple[str, ...]
    created_ts_utc: datetime

    @property
    def shadow_ready(self) -> bool:
        return self.status is TrainingRunStatus.SHADOW_READY


@dataclass(frozen=True, slots=True)
class AutoTrainingOutcome:
    report: TrainingRunReport
    artifact: ModelArtifact | None
    split: TrainingSplitManifest


_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_training_runs (
    run_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    trainer_version TEXT NOT NULL,
    dataset_fingerprint TEXT NOT NULL,
    split_hash TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    report_json TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_training_runs_model_created
ON model_training_runs(model_id,created_ts_utc,run_id);
"""


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_training_split(
    examples: Sequence[TrainingExample],
    plan: ChallengerRetrainingPlan,
    model: TrainableModel,
) -> tuple[TrainingSplitManifest, tuple[TrainingExample, ...], tuple[TrainingExample, ...]]:
    """Materialize the drift plan into exact immutable train/validation sample identities."""
    if not plan.ready:
        raise ValueError("retraining plan must be READY_RESEARCH_CHALLENGER")
    rows = tuple(
        sorted(
            validate_examples(examples, model.feature_schema),
            key=lambda item: (item.observed_ts_utc, item.sample_id),
        )
    )
    dataset_hash = dataset_fingerprint(rows, model.feature_schema)
    if dataset_hash != plan.dataset_fingerprint:
        raise ValueError("retraining plan dataset fingerprint does not match training examples")
    if plan.samples_since_drift <= 0 or plan.samples_since_drift > len(rows):
        raise ValueError("plan samples_since_drift is incompatible with dataset")
    if plan.validation_samples <= 0 or plan.validation_samples >= plan.samples_since_drift:
        raise ValueError("plan validation sample count is incompatible with post-drift data")

    post_drift = rows[-plan.samples_since_drift :]
    pre_drift = rows[: -plan.samples_since_drift]
    validation = post_drift[-plan.validation_samples :]
    validation_start = validation[0].observed_ts_utc.astimezone(UTC)
    cutoff = validation_start - timedelta(seconds=plan.purge_embargo_seconds)
    pre_validation_post = post_drift[: -plan.validation_samples]
    purged = tuple(item for item in pre_validation_post if item.observed_ts_utc > cutoff)
    eligible_post = tuple(item for item in pre_validation_post if item.observed_ts_utc <= cutoff)
    post_train = eligible_post[-plan.post_drift_training_samples :]
    used_post_ids = {item.sample_id for item in post_train}
    purged_ids = {item.sample_id for item in purged}
    unused_post = tuple(
        item
        for item in pre_validation_post
        if item.sample_id not in used_post_ids and item.sample_id not in purged_ids
    )
    anchor = (
        pre_drift[-plan.pre_drift_anchor_samples :]
        if plan.pre_drift_anchor_samples
        else ()
    )
    train = tuple(
        sorted(
            (*anchor, *post_train),
            key=lambda item: (item.observed_ts_utc, item.sample_id),
        )
    )

    if train and max(item.observed_ts_utc for item in train) > cutoff:
        raise ValueError("training split violates purge boundary")
    material = {
        "version": "training-split-v2",
        "plan_id": plan.plan_id,
        "dataset_fingerprint": dataset_hash,
        "feature_schema_hash": model.feature_schema.fingerprint,
        "train_ids": [item.sample_id for item in train],
        "validation_ids": [item.sample_id for item in validation],
        "anchor_ids": [item.sample_id for item in anchor],
        "purged_ids": [item.sample_id for item in purged],
        "unused_post_drift_ids": [item.sample_id for item in unused_post],
        "validation_start_ts_utc": validation_start.isoformat(),
        "purge_cutoff_ts_utc": cutoff.isoformat(),
    }
    split_hash = _hash_payload(material)
    manifest = TrainingSplitManifest(
        plan_id=plan.plan_id,
        dataset_fingerprint=dataset_hash,
        feature_schema_hash=model.feature_schema.fingerprint,
        train_ids=tuple(item.sample_id for item in train),
        validation_ids=tuple(item.sample_id for item in validation),
        anchor_ids=tuple(item.sample_id for item in anchor),
        purged_ids=tuple(item.sample_id for item in purged),
        unused_post_drift_ids=tuple(item.sample_id for item in unused_post),
        validation_start_ts_utc=validation_start,
        purge_cutoff_ts_utc=cutoff,
        split_hash=split_hash,
    )
    return manifest, train, tuple(validation)


def _metric_result(metric: ValidationMetric, rows: Sequence[PredictionRecord]) -> MetricResult:
    name = metric.name.strip()
    if not name:
        raise ValueError("validation metric name is blank")
    threshold = float(metric.threshold)
    if not math.isfinite(threshold):
        raise ValueError(f"validation metric {name} has non-finite threshold")
    value = float(metric.evaluate(rows))
    if not math.isfinite(value):
        raise ValueError(f"validation metric {name} produced non-finite value")
    passed = (
        value <= threshold
        if metric.direction is MetricDirection.MINIMIZE
        else value >= threshold
    )
    return MetricResult(name, value, metric.direction, threshold, passed)


def _fit_once(model: TrainableModel, train: Sequence[TrainingExample], seed: int) -> FittedModel:
    fitted = model.fit(train, seed=seed)
    if fitted.model_id != model.model_id:
        raise ValueError("fitted model identity does not match trainable model identity")
    return fitted


def _validate_artifact(
    artifact: ModelArtifact,
    *,
    model_id: str,
    maximum_bytes: int,
) -> None:
    if artifact.model_id != model_id:
        raise ValueError("artifact model identity mismatch")
    if len(artifact.payload) > maximum_bytes:
        raise ValueError("artifact exceeds configured maximum size")


def _predict(
    fitted: FittedModel,
    validation: Sequence[TrainingExample],
) -> tuple[PredictionRecord, ...]:
    return tuple(
        PredictionRecord(
            sample_id=item.sample_id,
            target=item.target,
            prediction=fitted.predict_one(item.features),
        )
        for item in validation
    )


def _report_id(
    *,
    model: TrainableModel,
    split: TrainingSplitManifest,
    code_revision: str,
    policy_fingerprint: str,
    seed: int,
) -> str:
    return _hash_payload(
        {
            "version": "auto-trainer-run-v2",
            "model_id": model.model_id,
            "trainer_version": model.trainer_version,
            "task": model.task.value,
            "determinism": model.determinism.value,
            "feature_schema": model.feature_schema.fingerprint,
            "dataset": split.dataset_fingerprint,
            "split": split.split_hash,
            "code_revision": code_revision,
            "policy_fingerprint": policy_fingerprint,
            "seed": seed,
        }
    )


def _empty_report(
    *,
    run_id: str,
    model: TrainableModel,
    split: TrainingSplitManifest,
    training_samples: int,
    validation_samples: int,
    status: TrainingRunStatus,
    failures: tuple[str, ...],
    created: datetime,
) -> TrainingRunReport:
    return TrainingRunReport(
        run_id=run_id,
        model_id=model.model_id,
        trainer_version=model.trainer_version,
        task=model.task.value,
        dataset_fingerprint=split.dataset_fingerprint,
        split_hash=split.split_hash,
        feature_schema_hash=split.feature_schema_hash,
        training_samples=training_samples,
        validation_samples=validation_samples,
        artifact_sha256="",
        artifact_loader_key="",
        artifact_model_version="",
        metrics=(),
        exact_reproducibility_verified=False,
        status=status,
        failures=failures,
        created_ts_utc=created,
    )


def _validation_failure_report(
    *,
    run_id: str,
    model: TrainableModel,
    split: TrainingSplitManifest,
    artifact: ModelArtifact,
    training_samples: int,
    validation_samples: int,
    exact_verified: bool,
    failures: tuple[str, ...],
    created: datetime,
) -> TrainingRunReport:
    return TrainingRunReport(
        run_id=run_id,
        model_id=model.model_id,
        trainer_version=model.trainer_version,
        task=model.task.value,
        dataset_fingerprint=split.dataset_fingerprint,
        split_hash=split.split_hash,
        feature_schema_hash=split.feature_schema_hash,
        training_samples=training_samples,
        validation_samples=validation_samples,
        artifact_sha256=artifact.sha256,
        artifact_loader_key=artifact.loader_key,
        artifact_model_version=artifact.model_version,
        metrics=(),
        exact_reproducibility_verified=exact_verified,
        status=TrainingRunStatus.VALIDATION_FAILED,
        failures=failures,
        created_ts_utc=created,
    )


def run_auto_training(
    path: str | Path,
    *,
    model: TrainableModel,
    examples: Sequence[TrainingExample],
    plan: ChallengerRetrainingPlan,
    metrics: Sequence[ValidationMetric],
    code_revision: str,
    policy_fingerprint: str,
    policy: AutoTrainerPolicy | None = None,
    now: datetime | None = None,
) -> AutoTrainingOutcome:
    """Fit an arbitrary model behind the universal contract and qualify it for shadow only."""
    policy = policy or AutoTrainerPolicy()
    if not code_revision.strip() or not policy_fingerprint.strip():
        raise ValueError("code_revision and policy_fingerprint are required")
    if not metrics:
        raise ValueError("at least one validation metric is required")
    metric_names = [metric.name.strip() for metric in metrics]
    if any(not name for name in metric_names):
        raise ValueError("validation metric names cannot be blank")
    if len(metric_names) != len(set(metric_names)):
        raise ValueError("validation metric names must be unique")

    created = (now or datetime.now(UTC)).astimezone(UTC)
    split, train, validation = build_training_split(examples, plan, model)
    run_id = _report_id(
        model=model,
        split=split,
        code_revision=code_revision,
        policy_fingerprint=policy_fingerprint,
        seed=policy.seed,
    )
    failures: list[str] = []
    if len(train) < policy.minimum_training_samples:
        failures.append(f"training_samples:{len(train)}<{policy.minimum_training_samples}")
    if len(validation) < policy.minimum_validation_samples:
        failures.append(
            f"validation_samples:{len(validation)}<{policy.minimum_validation_samples}"
        )
    if failures:
        report = _empty_report(
            run_id=run_id,
            model=model,
            split=split,
            training_samples=len(train),
            validation_samples=len(validation),
            status=TrainingRunStatus.REJECTED_DATA,
            failures=tuple(failures),
            created=created,
        )
        _persist_report(path, report)
        return AutoTrainingOutcome(report, None, split)

    try:
        fitted = _fit_once(model, train, policy.seed)
        artifact = fitted.export_artifact()
        _validate_artifact(
            artifact,
            model_id=model.model_id,
            maximum_bytes=policy.maximum_artifact_bytes,
        )
    except Exception as exc:
        failures.append(f"fit_or_artifact_failed:{type(exc).__name__}")
        report = _empty_report(
            run_id=run_id,
            model=model,
            split=split,
            training_samples=len(train),
            validation_samples=len(validation),
            status=TrainingRunStatus.FIT_FAILED,
            failures=tuple(failures),
            created=created,
        )
        _persist_report(path, report)
        return AutoTrainingOutcome(report, None, split)

    exact_verified = False
    if policy.verify_exact_determinism and model.determinism is DeterminismLevel.EXACT:
        try:
            second = _fit_once(model, train, policy.seed).export_artifact()
            _validate_artifact(
                second,
                model_id=model.model_id,
                maximum_bytes=policy.maximum_artifact_bytes,
            )
            exact_verified = second.sha256 == artifact.sha256
            if not exact_verified:
                failures.append("exact_determinism_artifact_mismatch")
        except Exception as exc:
            failures.append(f"determinism_verification_failed:{type(exc).__name__}")

    try:
        predictions = _predict(fitted, validation)
        metric_results = tuple(_metric_result(metric, predictions) for metric in metrics)
    except Exception as exc:
        failures.append(f"validation_evaluation_failed:{type(exc).__name__}")
        report = _validation_failure_report(
            run_id=run_id,
            model=model,
            split=split,
            artifact=artifact,
            training_samples=len(train),
            validation_samples=len(validation),
            exact_verified=exact_verified,
            failures=tuple(failures),
            created=created,
        )
        _persist_report(path, report)
        return AutoTrainingOutcome(report, artifact, split)

    failures.extend(
        f"validation_metric_failed:{item.name}:{item.value:.8f}"
        for item in metric_results
        if not item.passed
    )
    status = (
        TrainingRunStatus.SHADOW_READY
        if not failures
        else TrainingRunStatus.VALIDATION_FAILED
    )
    report = TrainingRunReport(
        run_id=run_id,
        model_id=model.model_id,
        trainer_version=model.trainer_version,
        task=model.task.value,
        dataset_fingerprint=split.dataset_fingerprint,
        split_hash=split.split_hash,
        feature_schema_hash=split.feature_schema_hash,
        training_samples=len(train),
        validation_samples=len(validation),
        artifact_sha256=artifact.sha256,
        artifact_loader_key=artifact.loader_key,
        artifact_model_version=artifact.model_version,
        metrics=metric_results,
        exact_reproducibility_verified=exact_verified,
        status=status,
        failures=tuple(failures),
        created_ts_utc=created,
    )
    _persist_report(path, report)
    return AutoTrainingOutcome(report, artifact, split)


def _persist_report(path: str | Path, report: TrainingRunReport) -> None:
    encoded = json.dumps(asdict(report), sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    with sqlite3.connect(str(path)) as db:
        db.executescript(_SCHEMA)
        db.execute(
            """
            INSERT OR IGNORE INTO model_training_runs
            (run_id,model_id,trainer_version,dataset_fingerprint,split_hash,artifact_sha256,
             status,report_json,report_sha256,created_ts_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                report.run_id,
                report.model_id,
                report.trainer_version,
                report.dataset_fingerprint,
                report.split_hash,
                report.artifact_sha256,
                report.status.value,
                encoded,
                digest,
                report.created_ts_utc.isoformat(),
            ),
        )


def load_training_run(path: str | Path, run_id: str) -> dict[str, object]:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        row = db.execute(
            "SELECT report_json,report_sha256 FROM model_training_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if row is None:
        raise KeyError(run_id)
    encoded = str(row["report_json"])
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if digest != str(row["report_sha256"]):
        raise ValueError("training run ledger integrity mismatch")
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise ValueError("training run payload is not an object")
    return {str(key): value for key, value in payload.items()}
