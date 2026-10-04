from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from .counterfactual_learning_efficiency_v2 import CausalLearningReportV2
from .information_value import LearningAction


@dataclass(frozen=True, slots=True)
class CausalAllocatorCalibrationV2:
    report_hash: str
    control_value: float
    minimum_multiplier: float
    maximum_multiplier: float
    effect_to_multiplier_scale: float
    multipliers: tuple[tuple[str, float], ...]
    calibration_hash: str

    def multiplier_for(self, action: str, segment: str = "") -> float:
        del segment
        return dict(self.multipliers).get(action, 1.0)


def _hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def build_causal_allocator_calibration_v2(
    report: CausalLearningReportV2,
    *,
    effect_to_multiplier_scale: float = 1.0,
    minimum_multiplier: float = 0.50,
    maximum_multiplier: float = 1.50,
) -> CausalAllocatorCalibrationV2:
    if not report.qualified:
        raise ValueError("causal learning v2 report must be qualified")
    if not math.isfinite(effect_to_multiplier_scale) or effect_to_multiplier_scale <= 0:
        raise ValueError("effect_to_multiplier_scale must be finite and positive")
    if not 0 < minimum_multiplier <= maximum_multiplier:
        raise ValueError("causal multiplier bounds are invalid")
    control = next(
        (
            item for item in report.treatment_estimates
            if item.treatment_key.startswith("CONTROL@")
        ),
        None,
    )
    if control is None:
        raise ValueError("causal allocator calibration requires a CONTROL treatment")
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
        multiplier = min(
            maximum_multiplier,
            max(minimum_multiplier, 1.0 + effect_to_multiplier_scale * conservative_effect),
        )
        multipliers.append((action, multiplier))
    normalized = tuple(sorted(multipliers))
    material = {
        "version": "causal-allocator-calibration-v2",
        "report_hash": report.report_hash,
        "control_value": round(control.posterior_mean, 12),
        "minimum_multiplier": minimum_multiplier,
        "maximum_multiplier": maximum_multiplier,
        "effect_to_multiplier_scale": effect_to_multiplier_scale,
        "multipliers": normalized,
    }
    return CausalAllocatorCalibrationV2(
        report_hash=report.report_hash,
        control_value=control.posterior_mean,
        minimum_multiplier=minimum_multiplier,
        maximum_multiplier=maximum_multiplier,
        effect_to_multiplier_scale=effect_to_multiplier_scale,
        multipliers=normalized,
        calibration_hash=_hash(material),
    )
