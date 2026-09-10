from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, TypeAlias, runtime_checkable

ScalarValue: TypeAlias = str | int | float | bool
TargetValue: TypeAlias = str | int | float | bool
FeatureVector: TypeAlias = tuple[tuple[str, ScalarValue], ...]


class TaskKind(StrEnum):
    BINARY_CLASSIFICATION = "BINARY_CLASSIFICATION"
    MULTICLASS_CLASSIFICATION = "MULTICLASS_CLASSIFICATION"
    REGRESSION = "REGRESSION"
    RANKING = "RANKING"


class FeatureDType(StrEnum):
    FLOAT = "FLOAT"
    INT = "INT"
    BOOL = "BOOL"
    CATEGORY = "CATEGORY"
    TEXT = "TEXT"


class DeterminismLevel(StrEnum):
    EXACT = "EXACT"
    SEEDED_BEST_EFFORT = "SEEDED_BEST_EFFORT"


@dataclass(frozen=True, slots=True)
class FeatureField:
    name: str
    dtype: FeatureDType

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("feature name is required")


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    version: str
    fields: tuple[FeatureField, ...]

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("feature schema version is required")
        if not self.fields:
            raise ValueError("feature schema requires at least one field")
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("feature schema field names must be unique")

    @property
    def fingerprint(self) -> str:
        payload = {
            "version": self.version,
            "fields": [[field.name, field.dtype.value] for field in self.fields],
        }
        return _hash_json(payload)

    def normalize(self, values: Mapping[str, ScalarValue]) -> FeatureVector:
        expected = {field.name for field in self.fields}
        supplied = set(values)
        missing = expected - supplied
        extra = supplied - expected
        if missing:
            raise ValueError(f"missing model features: {sorted(missing)}")
        if extra:
            raise ValueError(f"unexpected model features: {sorted(extra)}")
        normalized: list[tuple[str, ScalarValue]] = []
        for field in self.fields:
            value = values[field.name]
            _validate_feature_value(field, value)
            normalized.append((field.name, value))
        return tuple(normalized)


@dataclass(frozen=True, slots=True)
class TrainingExample:
    sample_id: str
    observed_ts_utc: datetime
    features: FeatureVector
    target: TargetValue
    weight: float = 1.0
    group_id: str = ""

    def __post_init__(self) -> None:
        if not self.sample_id.strip():
            raise ValueError("sample_id is required")
        if self.observed_ts_utc.tzinfo is None or self.observed_ts_utc.utcoffset() is None:
            raise ValueError("observed_ts_utc must be timezone-aware")
        if not math.isfinite(self.weight) or self.weight <= 0:
            raise ValueError("weight must be finite and positive")
        names = [name for name, _ in self.features]
        if len(names) != len(set(names)):
            raise ValueError("example feature names must be unique")
        _validate_scalar(self.target, "target")

    @property
    def feature_map(self) -> dict[str, ScalarValue]:
        return dict(self.features)


@dataclass(frozen=True, slots=True)
class ModelPrediction:
    value: TargetValue
    confidence: float | None = None

    def __post_init__(self) -> None:
        _validate_scalar(self.value, "prediction")
        if self.confidence is not None:
            if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
                raise ValueError("confidence must be finite and in [0,1]")


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    model_id: str
    model_version: str
    loader_key: str
    media_type: str
    payload: bytes

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.model_id, self.model_version, self.loader_key, self.media_type)
        ):
            raise ValueError("artifact identity fields are required")
        if not self.payload:
            raise ValueError("artifact payload cannot be empty")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@runtime_checkable
class FittedModel(Protocol):
    @property
    def model_id(self) -> str: ...

    def predict_one(self, features: FeatureVector) -> ModelPrediction: ...

    def export_artifact(self) -> ModelArtifact: ...


@runtime_checkable
class TrainableModel(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def trainer_version(self) -> str: ...

    @property
    def task(self) -> TaskKind: ...

    @property
    def feature_schema(self) -> FeatureSchema: ...

    @property
    def determinism(self) -> DeterminismLevel: ...

    def fit(self, examples: Sequence[TrainingExample], *, seed: int) -> FittedModel: ...


FitCallable: TypeAlias = Callable[[Sequence[TrainingExample], int], FittedModel]


@dataclass(frozen=True, slots=True)
class CallableTrainableModel:
    """Adapter that makes an arbitrary fitting function obey Scorpion's training contract."""

    model_id: str
    trainer_version: str
    task: TaskKind
    feature_schema: FeatureSchema
    fit_callable: FitCallable
    determinism: DeterminismLevel = DeterminismLevel.SEEDED_BEST_EFFORT

    def __post_init__(self) -> None:
        if not self.model_id.strip() or not self.trainer_version.strip():
            raise ValueError("model_id and trainer_version are required")

    def fit(self, examples: Sequence[TrainingExample], *, seed: int) -> FittedModel:
        return self.fit_callable(examples, seed)


def validate_examples(
    examples: Sequence[TrainingExample],
    schema: FeatureSchema,
) -> tuple[TrainingExample, ...]:
    rows = tuple(examples)
    ids = [item.sample_id for item in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("training sample ids must be unique")
    for item in rows:
        normalized = schema.normalize(item.feature_map)
        if normalized != item.features:
            raise ValueError(
                "feature vectors must be stored in exact schema order; normalize at ingestion"
            )
    return rows


def dataset_fingerprint(
    examples: Sequence[TrainingExample],
    schema: FeatureSchema,
) -> str:
    rows = validate_examples(examples, schema)
    payload = {
        "version": "trainable-dataset-v1",
        "schema": schema.fingerprint,
        "samples": [
            {
                "sample_id": item.sample_id,
                "observed_ts_utc": item.observed_ts_utc.isoformat(),
                "features": [[name, _canonical_scalar(value)] for name, value in item.features],
                "target": _canonical_scalar(item.target),
                "weight": round(item.weight, 12),
                "group_id": item.group_id,
            }
            for item in sorted(rows, key=lambda row: row.sample_id)
        ],
    }
    return _hash_json(payload)


def _validate_feature_value(field: FeatureField, value: ScalarValue) -> None:
    _validate_scalar(value, field.name)
    if field.dtype is FeatureDType.FLOAT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"feature {field.name} requires FLOAT")
    elif field.dtype is FeatureDType.INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"feature {field.name} requires INT")
    elif field.dtype is FeatureDType.BOOL:
        if not isinstance(value, bool):
            raise TypeError(f"feature {field.name} requires BOOL")
    elif field.dtype in {FeatureDType.CATEGORY, FeatureDType.TEXT} and not isinstance(value, str):
        raise TypeError(f"feature {field.name} requires string data")


def _validate_scalar(value: TargetValue, name: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _canonical_scalar(value: TargetValue) -> TargetValue:
    _validate_scalar(value, "scalar")
    return value


def _hash_json(payload: object) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
