from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from .counterfactual_learning_efficiency import (
    CounterfactualLearningReport,
    CounterfactualLearningStatus,
)
from .information_value import LearningAction


@dataclass(frozen=True, slots=True)
class CounterfactualAllocatorCalibration:
    report_hash: str
    control_value: float
    effect_to_multiplier_scale: float
    minimum_multiplier: float
    maximum_multiplier: float
    multipliers: tuple[tuple[str, float], ...]
    calibration_hash: str

    def multiplier_for(self, action: str, segment: str = "") -> float:
        del segment
        return dict(self.multipliers).get(action, 1.0)


def _hash(payload: object) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


def build_counterfactual_allocator_calibration(
    report: CounterfactualLearningReport,
    *,
    effect_to_multiplier_scale: float = 1.0,
    minimum_multiplier: float = 0.50,
    maximum_multiplier: float = 1.50,
) -> CounterfactualAllocatorCalibration:
    """Convert qualified causal research-treatment effects into bounded allocator multipliers."""
    if report.status is not CounterfactualLearningStatus.QUALIFIED:
        raise ValueError("counterfactual report must be qualified")
    if not math.isfinite(effect_to_multiplier_scale) or effect_to_multiplier_scale <= 0:
        raise ValueError("effect_to_multiplier_scale must be finite and positive")
    if not 0 < minimum_multiplier <= maximum_multiplier:
        raise ValueError("counterfactual multiplier bounds are invalid")
    control = next(
        (
            item for item in report.treatment_estimates
            if item.treatment_key.startswith("CONTROL@")
        ),
        None,
    )
    if control is None:
        raise ValueError("counterfactual allocator calibration requires a CONTROL treatment")
    action_prefixes = {
        LearningAction.LIGHT_SHADOW.value: "LIGHT_SHADOW@",
        LearningAction.DEEP_SHADOW.value: "DEEP_SHADOW@",
        LearningAction.HUMAN_REVIEW.value: "HUMAN_REVIEW@",
    }
    multipliers: list[tuple[str, float]] = []
    for action, prefix in action_prefixes.items():
        estimate = next(
            (item for item in report.treatment_estimates if item.treatment_key.startswith(prefix)),
            None,
        )
        if estimate is None:
            multipliers.append((action, 1.0))
            continue
        conservative_effect = estimate.simultaneous_lower_bound - control.posterior_mean
        raw = 1.0 + effect_to_multiplier_scale * conservative_effect
        multiplier = min(maximum_multiplier, max(minimum_multiplier, raw))
        multipliers.append((action, multiplier))
    normalized = tuple(sorted(multipliers))
    material = {
        "version": "counterfactual-allocator-calibration-v1",
        "report_hash": report.report_hash,
        "control_value": round(control.posterior_mean, 12),
        "effect_to_multiplier_scale": effect_to_multiplier_scale,
        "minimum_multiplier": minimum_multiplier,
        "maximum_multiplier": maximum_multiplier,
        "multipliers": normalized,
    }
    return CounterfactualAllocatorCalibration(
        report_hash=report.report_hash,
        control_value=control.posterior_mean,
        effect_to_multiplier_scale=effect_to_multiplier_scale,
        minimum_multiplier=minimum_multiplier,
        maximum_multiplier=maximum_multiplier,
        multipliers=normalized,
        calibration_hash=_hash(material),
    )
