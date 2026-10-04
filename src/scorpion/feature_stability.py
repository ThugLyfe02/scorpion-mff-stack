from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class FeatureStabilityStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class FeatureStabilityPolicy:
    minimum_folds: int = 4
    minimum_fold_samples: int = 30
    minimum_total_samples: int = 150
    minimum_direction_agreement: float = 0.75
    minimum_median_absolute_effect: float = 0.10
    maximum_single_fold_effect_share: float = 0.70

    def __post_init__(self) -> None:
        if self.minimum_folds < 2:
            raise ValueError("minimum_folds must be >=2")
        if self.minimum_fold_samples <= 0 or self.minimum_total_samples <= 0:
            raise ValueError("sample thresholds must be positive")
        if not 0.5 <= self.minimum_direction_agreement <= 1:
            raise ValueError("minimum_direction_agreement must be in [0.5,1]")
        if self.minimum_median_absolute_effect < 0:
            raise ValueError("minimum_median_absolute_effect cannot be negative")
        if not 0 < self.maximum_single_fold_effect_share <= 1:
            raise ValueError("maximum_single_fold_effect_share must be in (0,1]")


@dataclass(frozen=True, slots=True)
class FeatureStabilityExample:
    event_id: str
    fold: int
    features: Mapping[str, float]
    target: bool


@dataclass(frozen=True, slots=True)
class FoldFeatureEffect:
    fold: int
    samples: int
    positives: int
    negatives: int
    standardized_effect: float


@dataclass(frozen=True, slots=True)
class FeatureStabilityMetric:
    feature: str
    folds: tuple[FoldFeatureEffect, ...]
    direction: int
    direction_agreement: float
    median_absolute_effect: float
    maximum_effect_share: float
    status: FeatureStabilityStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is FeatureStabilityStatus.QUALIFIED


@dataclass(frozen=True, slots=True)
class FeatureStabilityReport:
    samples: int
    folds: int
    features: tuple[FeatureStabilityMetric, ...]
    qualified_features: tuple[str, ...]
    rejected_features: tuple[str, ...]
    status: FeatureStabilityStatus
    failures: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return self.status is FeatureStabilityStatus.QUALIFIED


def _standardized_effect(rows: Sequence[FeatureStabilityExample], feature: str) -> float:
    positives = [row.features[feature] for row in rows if row.target and feature in row.features]
    negatives = [row.features[feature] for row in rows if not row.target and feature in row.features]
    if len(positives) < 2 or len(negatives) < 2:
        return 0.0
    mean_positive = statistics.fmean(positives)
    mean_negative = statistics.fmean(negatives)
    var_positive = statistics.variance(positives)
    var_negative = statistics.variance(negatives)
    pooled = math.sqrt(max((var_positive + var_negative) / 2.0, 1e-12))
    return (mean_positive - mean_negative) / pooled


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def evaluate_feature_stability(
    examples: Sequence[FeatureStabilityExample],
    *,
    policy: FeatureStabilityPolicy | None = None,
) -> FeatureStabilityReport:
    policy = policy or FeatureStabilityPolicy()
    rows = list(examples)
    folds = sorted({row.fold for row in rows})
    global_failures: list[str] = []
    if len(rows) < policy.minimum_total_samples:
        global_failures.append(
            f"insufficient_total_samples:{len(rows)}<{policy.minimum_total_samples}"
        )
    if len(folds) < policy.minimum_folds:
        global_failures.append(f"insufficient_folds:{len(folds)}<{policy.minimum_folds}")

    feature_names = tuple(sorted({name for row in rows for name in row.features}))
    metrics: list[FeatureStabilityMetric] = []
    for feature in feature_names:
        fold_effects: list[FoldFeatureEffect] = []
        for fold in folds:
            fold_rows = [row for row in rows if row.fold == fold and feature in row.features]
            positives = sum(row.target for row in fold_rows)
            negatives = len(fold_rows) - positives
            if len(fold_rows) < policy.minimum_fold_samples or positives < 2 or negatives < 2:
                continue
            fold_effects.append(
                FoldFeatureEffect(
                    fold=fold,
                    samples=len(fold_rows),
                    positives=positives,
                    negatives=negatives,
                    standardized_effect=_standardized_effect(fold_rows, feature),
                )
            )
        failures: list[str] = []
        if len(fold_effects) < policy.minimum_folds:
            failures.append(
                f"insufficient_qualified_folds:{len(fold_effects)}<{policy.minimum_folds}"
            )
            direction = 0
            direction_agreement = 0.0
            median_effect = 0.0
            max_share = 1.0
        else:
            effects = [item.standardized_effect for item in fold_effects]
            median_signed = statistics.median(effects)
            direction = _sign(median_signed)
            directional = [effect for effect in effects if _sign(effect) != 0]
            direction_agreement = (
                sum(_sign(effect) == direction for effect in directional) / len(directional)
                if directional and direction != 0
                else 0.0
            )
            absolute = [abs(effect) for effect in effects]
            median_effect = statistics.median(absolute)
            total_effect = sum(absolute)
            max_share = max(absolute, default=0.0) / total_effect if total_effect > 0 else 1.0
            if direction_agreement < policy.minimum_direction_agreement:
                failures.append(
                    "direction_agreement_below_threshold:"
                    f"{direction_agreement:.6f}<{policy.minimum_direction_agreement:.6f}"
                )
            if median_effect < policy.minimum_median_absolute_effect:
                failures.append(
                    "median_absolute_effect_below_threshold:"
                    f"{median_effect:.6f}<{policy.minimum_median_absolute_effect:.6f}"
                )
            if max_share > policy.maximum_single_fold_effect_share:
                failures.append(
                    "single_fold_effect_concentration:"
                    f"{max_share:.6f}>{policy.maximum_single_fold_effect_share:.6f}"
                )
        if any(item.startswith("insufficient_") for item in failures):
            status = FeatureStabilityStatus.INSUFFICIENT
        elif failures:
            status = FeatureStabilityStatus.FAILED
        else:
            status = FeatureStabilityStatus.QUALIFIED
        metrics.append(
            FeatureStabilityMetric(
                feature=feature,
                folds=tuple(fold_effects),
                direction=direction,
                direction_agreement=direction_agreement,
                median_absolute_effect=median_effect,
                maximum_effect_share=max_share,
                status=status,
                failures=tuple(failures),
            )
        )

    qualified = tuple(item.feature for item in metrics if item.qualified)
    rejected = tuple(item.feature for item in metrics if not item.qualified)
    if global_failures:
        status = FeatureStabilityStatus.INSUFFICIENT
    elif not qualified:
        status = FeatureStabilityStatus.FAILED
        global_failures.append("no_temporally_stable_features")
    else:
        status = FeatureStabilityStatus.QUALIFIED
    return FeatureStabilityReport(
        samples=len(rows),
        folds=len(folds),
        features=tuple(metrics),
        qualified_features=qualified,
        rejected_features=rejected,
        status=status,
        failures=tuple(global_failures),
    )
