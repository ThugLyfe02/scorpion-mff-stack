from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from .domain import EventKind


@dataclass(frozen=True, slots=True)
class HardExampleCurriculumPolicy:
    maximum_examples: int = 500
    minimum_label_confidence: float = 0.90
    minimum_information_value: float = 0.15
    maximum_aleatoric: float = 0.65
    maximum_label_fraction: float = 0.50
    maximum_slice_fraction: float = 0.40
    minimum_examples_per_label: int = 1
    hotspot_bonus_weight: float = 0.15
    epistemic_bonus_weight: float = 0.10
    aleatoric_penalty_weight: float = 0.20

    def __post_init__(self) -> None:
        if self.maximum_examples <= 0:
            raise ValueError("maximum_examples must be positive")
        if self.minimum_examples_per_label < 0:
            raise ValueError("minimum_examples_per_label cannot be negative")
        for name in (
            "minimum_label_confidence",
            "minimum_information_value",
            "maximum_aleatoric",
            "maximum_label_fraction",
            "maximum_slice_fraction",
            "hotspot_bonus_weight",
            "epistemic_bonus_weight",
            "aleatoric_penalty_weight",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be finite and in [0,1]")
        if self.maximum_label_fraction <= 0 or self.maximum_slice_fraction <= 0:
            raise ValueError("curriculum diversity fractions must be positive")


@dataclass(frozen=True, slots=True)
class CurriculumExample:
    event_id: str
    label: EventKind
    label_source_id: str
    label_confidence: float
    information_value: float
    residual_hotspot_score: float
    epistemic: float
    aleatoric: float
    slices: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.label_source_id.strip():
            raise ValueError("event_id and label_source_id are required")
        for name in ("label_confidence", "information_value", "epistemic", "aleatoric"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be finite and in [0,1]")
        if not math.isfinite(self.residual_hotspot_score) or self.residual_hotspot_score < 0:
            raise ValueError("residual_hotspot_score must be finite and non-negative")
        if any(not key.strip() or not value.strip() for key, value in self.slices.items()):
            raise ValueError("slice names and values must be non-empty")


@dataclass(frozen=True, slots=True)
class CurriculumSelectionItem:
    event_id: str
    label: EventKind
    label_source_id: str
    score: float
    slices: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class HardExampleCurriculum:
    selected: tuple[CurriculumSelectionItem, ...]
    exclusions: tuple[tuple[str, str], ...]
    label_counts: tuple[tuple[str, int], ...]
    slice_counts: tuple[tuple[str, int], ...]
    source_dataset_fingerprint: str
    holdout_fingerprint: str
    curriculum_fingerprint: str


def _slice_keys(example: CurriculumExample) -> tuple[str, ...]:
    return tuple(f"{key}={value}" for key, value in sorted(example.slices.items()))


def _hotspot_strength(value: float) -> float:
    return 1.0 - math.exp(-value)


def _score(example: CurriculumExample, policy: HardExampleCurriculumPolicy) -> float:
    return max(
        0.0,
        example.information_value
        + policy.hotspot_bonus_weight * _hotspot_strength(example.residual_hotspot_score)
        + policy.epistemic_bonus_weight * example.epistemic
        - policy.aleatoric_penalty_weight * example.aleatoric,
    )


def build_hard_example_curriculum(
    examples: tuple[CurriculumExample, ...],
    *,
    source_dataset_fingerprint: str,
    holdout_event_ids: frozenset[str] = frozenset(),
    policy: HardExampleCurriculumPolicy | None = None,
) -> HardExampleCurriculum:
    """Select hard-but-learnable research examples without contaminating the frozen holdout."""
    policy = policy or HardExampleCurriculumPolicy()
    if not source_dataset_fingerprint.strip():
        raise ValueError("source_dataset_fingerprint is required")
    event_ids = [example.event_id for example in examples]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("curriculum input event ids must be unique")

    eligible: list[CurriculumExample] = []
    exclusions: list[tuple[str, str]] = []
    for example in examples:
        if example.event_id in holdout_event_ids:
            exclusions.append((example.event_id, "frozen_holdout"))
        elif example.label_confidence < policy.minimum_label_confidence:
            exclusions.append((example.event_id, "insufficient_label_confidence"))
        elif example.information_value < policy.minimum_information_value:
            exclusions.append((example.event_id, "low_information_value"))
        elif example.aleatoric > policy.maximum_aleatoric:
            exclusions.append((example.event_id, "excessive_aleatoric_uncertainty"))
        else:
            eligible.append(example)

    ranked = sorted(
        eligible,
        key=lambda item: (-_score(item, policy), item.event_id),
    )
    target = min(policy.maximum_examples, len(ranked))
    label_quota = max(1, math.ceil(policy.maximum_examples * policy.maximum_label_fraction))
    slice_quota = max(1, math.ceil(policy.maximum_examples * policy.maximum_slice_fraction))
    selected: list[CurriculumExample] = []
    selected_ids: set[str] = set()
    label_counts: Counter[EventKind] = Counter()
    slice_counts: Counter[str] = Counter()

    def can_add(example: CurriculumExample) -> bool:
        if len(selected) >= target or example.event_id in selected_ids:
            return False
        if label_counts[example.label] >= label_quota:
            return False
        return all(slice_counts[key] < slice_quota for key in _slice_keys(example))

    def add(example: CurriculumExample) -> None:
        selected.append(example)
        selected_ids.add(example.event_id)
        label_counts[example.label] += 1
        slice_counts.update(_slice_keys(example))

    # Seed label breadth before globally optimizing difficulty. This prevents the curriculum from
    # collapsing onto one easy-to-find failure class and preserves signal for broad model quality.
    if policy.minimum_examples_per_label:
        by_label: dict[EventKind, list[CurriculumExample]] = {}
        for example in ranked:
            by_label.setdefault(example.label, []).append(example)
        for label in sorted(by_label, key=lambda item: item.value):
            added = 0
            for example in by_label[label]:
                if can_add(example):
                    add(example)
                    added += 1
                if added >= policy.minimum_examples_per_label or len(selected) >= target:
                    break

    for example in ranked:
        if can_add(example):
            add(example)
        if len(selected) >= target:
            break

    for example in ranked:
        if example.event_id not in selected_ids and not any(
            event_id == example.event_id for event_id, _ in exclusions
        ):
            exclusions.append((example.event_id, "diversity_quota"))

    items = tuple(
        CurriculumSelectionItem(
            event_id=example.event_id,
            label=example.label,
            label_source_id=example.label_source_id,
            score=_score(example, policy),
            slices=tuple(sorted(example.slices.items())),
        )
        for example in selected
    )
    holdout_fingerprint = hashlib.sha256(
        json.dumps(sorted(holdout_event_ids), separators=(",", ":")).encode()
    ).hexdigest()
    material = {
        "version": "hard-example-curriculum-v1",
        "source_dataset_fingerprint": source_dataset_fingerprint,
        "holdout_fingerprint": holdout_fingerprint,
        "policy": asdict(policy),
        "selected": [
            {
                "event_id": item.event_id,
                "label": item.label.value,
                "label_source_id": item.label_source_id,
                "score": round(item.score, 12),
                "slices": item.slices,
            }
            for item in items
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return HardExampleCurriculum(
        selected=items,
        exclusions=tuple(sorted(exclusions)),
        label_counts=tuple(sorted((label.value, count) for label, count in label_counts.items())),
        slice_counts=tuple(sorted(slice_counts.items())),
        source_dataset_fingerprint=source_dataset_fingerprint,
        holdout_fingerprint=holdout_fingerprint,
        curriculum_fingerprint=fingerprint,
    )
