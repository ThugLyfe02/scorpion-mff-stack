from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .domain import EventKind, SignalEvent


@dataclass(frozen=True, slots=True)
class ShadowPrediction:
    model_name: str
    model_version: str
    predicted_kind: EventKind
    confidence: float
    latency_ms: float
    rationale: str = ""


class ShadowClassifier(Protocol):
    """Offline/side-channel classifier contract.

    Implementations may use an LLM or another model, but their output is observation-only.
    It never mutates the money-path state machine or creates executable effects.
    """

    def classify(self, text: str) -> ShadowPrediction: ...


class DisagreementKind(StrEnum):
    AGREE = "AGREE"
    SHADOW_ACTION_MORE_AGGRESSIVE = "SHADOW_ACTION_MORE_AGGRESSIVE"
    DETERMINISTIC_ACTION_MORE_AGGRESSIVE = "DETERMINISTIC_ACTION_MORE_AGGRESSIVE"
    DIFFERENT_ACTION = "DIFFERENT_ACTION"


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    disagreement: DisagreementKind
    deterministic_kind: EventKind
    shadow_kind: EventKind


def compare_shadow(event: SignalEvent, prediction: ShadowPrediction) -> ShadowComparison:
    if event.kind is prediction.predicted_kind:
        kind = DisagreementKind.AGREE
    else:
        deterministic_action = event.kind in {
            EventKind.ENTRY,
            EventKind.ADD,
            EventKind.TRIM,
            EventKind.EXIT,
        }
        shadow_action = prediction.predicted_kind in {
            EventKind.ENTRY,
            EventKind.ADD,
            EventKind.TRIM,
            EventKind.EXIT,
        }
        if shadow_action and not deterministic_action:
            kind = DisagreementKind.SHADOW_ACTION_MORE_AGGRESSIVE
        elif deterministic_action and not shadow_action:
            kind = DisagreementKind.DETERMINISTIC_ACTION_MORE_AGGRESSIVE
        else:
            kind = DisagreementKind.DIFFERENT_ACTION
    return ShadowComparison(kind, event.kind, prediction.predicted_kind)
