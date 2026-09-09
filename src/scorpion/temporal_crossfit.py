from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TemporalCrossFitStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class TemporalPrediction:
    event_id: str
    event_ts_utc: datetime
    trained_through_ts_utc: datetime
    probability: float
    truth: bool

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id is required")
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError("probability must be in [0,1]")
        for value in (self.event_ts_utc, self.trained_through_ts_utc):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must be timezone-aware")


@dataclass(frozen=True, slots=True)
class TemporalCrossFitPolicy:
    minimum_samples: int = 100
    minimum_positive_samples: int = 20
    folds: int = 4
    minimum_auc: float = 0.60
    maximum_brier: float = 0.20
    maximum_log_loss: float = 0.70
    maximum_expected_calibration_error: float = 0.10
    minimum_positive_fold_ratio: float = 0.75
    calibration_bins: int = 10

    def __post_init__(self) -> None:
        if self.minimum_samples <= 0 or self.minimum_positive_samples <= 0:
            raise ValueError("sample thresholds must be positive")
        if self.folds < 2:
            raise ValueError("folds must be >=2")
        if not 0.5 <= self.minimum_auc <= 1.0:
            raise ValueError("minimum_auc must be in [0.5,1]")
        if not 0 <= self.maximum_brier <= 1:
            raise ValueError("maximum_brier must be in [0,1]")
        if self.maximum_log_loss <= 0:
            raise ValueError("maximum_log_loss must be positive")
        if not 0 <= self.maximum_expected_calibration_error <= 1:
            raise ValueError("maximum_expected_calibration_error must be in [0,1]")
        if not 0 <= self.minimum_positive_fold_ratio <= 1:
            raise ValueError("minimum_positive_fold_ratio must be in [0,1]")
        if self.calibration_bins <= 1:
            raise ValueError("calibration_bins must be >1")


@dataclass(frozen=True, slots=True)
class TemporalFoldMetrics:
    fold: int
    samples: int
    positives: int
    mean_probability: float
    observed_rate: float
    brier: float
    mean_edge: float


@dataclass(frozen=True, slots=True)
class TemporalCrossFitReport:
    samples: int
    positives: int
    temporal_leakage_rows: int
    auc: float
    brier: float
    log_loss: float
    expected_calibration_error: float
    folds: tuple[TemporalFoldMetrics, ...]
    positive_fold_ratio: float
    status: TemporalCrossFitStatus
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.status is TemporalCrossFitStatus.PASS


def _brier(rows: list[TemporalPrediction]) -> float:
    if not rows:
        return 0.0
    return sum((row.probability - float(row.truth)) ** 2 for row in rows) / len(rows)


def _log_loss(rows: list[TemporalPrediction]) -> float:
    if not rows:
        return 0.0
    eps = 1e-12
    total = 0.0
    for row in rows:
        probability = min(1.0 - eps, max(eps, row.probability))
        total -= math.log(probability if row.truth else 1.0 - probability)
    return total / len(rows)


def _auc(rows: list[TemporalPrediction]) -> float:
    positives = [row for row in rows if row.truth]
    negatives = [row for row in rows if not row.truth]
    if not positives or not negatives:
        return 0.5
    wins = 0.0
    comparisons = 0
    for positive in positives:
        for negative in negatives:
            comparisons += 1
            if positive.probability > negative.probability:
                wins += 1.0
            elif positive.probability == negative.probability:
                wins += 0.5
    return wins / comparisons


def _ece(rows: list[TemporalPrediction], bins: int) -> float:
    if not rows:
        return 0.0
    buckets: list[list[TemporalPrediction]] = [[] for _ in range(bins)]
    for row in rows:
        index = min(bins - 1, int(row.probability * bins))
        buckets[index].append(row)
    total = len(rows)
    error = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        confidence = sum(item.probability for item in bucket) / len(bucket)
        observed = sum(item.truth for item in bucket) / len(bucket)
        error += (len(bucket) / total) * abs(confidence - observed)
    return error


def _folds(
    rows: list[TemporalPrediction],
    folds: int,
) -> tuple[TemporalFoldMetrics, ...]:
    effective = min(folds, len(rows))
    result: list[TemporalFoldMetrics] = []
    for index in range(effective):
        start = index * len(rows) // effective
        end = (index + 1) * len(rows) // effective
        fold_rows = rows[start:end]
        if not fold_rows:
            continue
        observed = sum(row.truth for row in fold_rows) / len(fold_rows)
        predicted = sum(row.probability for row in fold_rows) / len(fold_rows)
        result.append(
            TemporalFoldMetrics(
                fold=index,
                samples=len(fold_rows),
                positives=sum(row.truth for row in fold_rows),
                mean_probability=predicted,
                observed_rate=observed,
                brier=_brier(fold_rows),
                mean_edge=observed - 0.5,
            )
        )
    return tuple(result)


def evaluate_temporal_crossfit(
    predictions: tuple[TemporalPrediction, ...],
    *,
    policy: TemporalCrossFitPolicy | None = None,
) -> TemporalCrossFitReport:
    """Evaluate already-generated out-of-fold probabilities under strict temporal causality.

    This function intentionally does not train a model. Each prediction must carry the latest
    timestamp used to train that model instance. If ``trained_through >= event_ts`` the row is
    marked leaked and the entire evaluation fails. Random cross-validation therefore cannot be
    passed off as a valid result for time-ordered trading research.
    """

    policy = policy or TemporalCrossFitPolicy()
    ordered = sorted(predictions, key=lambda row: (row.event_ts_utc, row.event_id))
    leakage = sum(
        row.trained_through_ts_utc >= row.event_ts_utc
        for row in ordered
    )
    positives = sum(row.truth for row in ordered)
    auc = _auc(ordered)
    brier = _brier(ordered)
    log_loss = _log_loss(ordered)
    ece = _ece(ordered, policy.calibration_bins)
    folds = _folds(ordered, policy.folds)
    positive_fold_ratio = (
        sum(fold.mean_edge > 0 for fold in folds) / len(folds) if folds else 0.0
    )
    failures: list[str] = []
    if len(ordered) < policy.minimum_samples:
        failures.append(f"insufficient_samples:{len(ordered)}<{policy.minimum_samples}")
    if positives < policy.minimum_positive_samples:
        failures.append(
            f"insufficient_positive_samples:{positives}<{policy.minimum_positive_samples}"
        )
    if leakage:
        failures.append(f"temporal_leakage_rows:{leakage}")
    if len(ordered) >= policy.minimum_samples and auc < policy.minimum_auc:
        failures.append(f"auc_below_threshold:{auc:.6f}<{policy.minimum_auc:.6f}")
    if brier > policy.maximum_brier:
        failures.append(f"brier_above_threshold:{brier:.6f}>{policy.maximum_brier:.6f}")
    if log_loss > policy.maximum_log_loss:
        failures.append(
            f"log_loss_above_threshold:{log_loss:.6f}>{policy.maximum_log_loss:.6f}"
        )
    if ece > policy.maximum_expected_calibration_error:
        failures.append(
            "calibration_error_above_threshold:"
            f"{ece:.6f}>{policy.maximum_expected_calibration_error:.6f}"
        )
    if folds and positive_fold_ratio < policy.minimum_positive_fold_ratio:
        failures.append(
            "positive_fold_ratio_below_threshold:"
            f"{positive_fold_ratio:.6f}<{policy.minimum_positive_fold_ratio:.6f}"
        )

    if any(item.startswith("insufficient_") for item in failures):
        status = TemporalCrossFitStatus.INSUFFICIENT
    elif failures:
        status = TemporalCrossFitStatus.FAIL
    else:
        status = TemporalCrossFitStatus.PASS
    return TemporalCrossFitReport(
        samples=len(ordered),
        positives=positives,
        temporal_leakage_rows=leakage,
        auc=auc,
        brier=brier,
        log_loss=log_loss,
        expected_calibration_error=ece,
        folds=folds,
        positive_fold_ratio=positive_fold_ratio,
        status=status,
        failures=tuple(failures),
    )
