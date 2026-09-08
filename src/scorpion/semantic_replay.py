from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .domain import Effect, SignalEvent
from .replay import replay, state_fingerprint

EffectSignature = tuple[str, str, int, str]


@dataclass(frozen=True, slots=True)
class ReplaySnapshot:
    fingerprint: str
    effect_signatures: tuple[EffectSignature, ...]
    positions: dict[str, str]


@dataclass(frozen=True, slots=True)
class SemanticReplayDiff:
    state_changed: bool
    added_effects: tuple[EffectSignature, ...]
    removed_effects: tuple[EffectSignature, ...]
    position_changes: dict[str, tuple[str | None, str | None]]


def _effect_signature(effect: Effect) -> EffectSignature:
    return (
        effect.kind.value,
        effect.contract_key or "",
        effect.generation,
        effect.reason,
    )


def replay_snapshot(events: Sequence[SignalEvent]) -> ReplaySnapshot:
    state, effects = replay(events)
    signatures = tuple(sorted(_effect_signature(effect) for effect in effects))
    positions = {key: position.status.value for key, position in sorted(state.positions.items())}
    return ReplaySnapshot(state_fingerprint(state), signatures, positions)


def compare_replay_semantics(
    baseline_events: Sequence[SignalEvent],
    candidate_events: Sequence[SignalEvent],
) -> SemanticReplayDiff:
    baseline = replay_snapshot(baseline_events)
    candidate = replay_snapshot(candidate_events)
    baseline_effects = set(baseline.effect_signatures)
    candidate_effects = set(candidate.effect_signatures)
    position_changes: dict[str, tuple[str | None, str | None]] = {}
    for key in sorted(set(baseline.positions) | set(candidate.positions)):
        old = baseline.positions.get(key)
        new = candidate.positions.get(key)
        if old != new:
            position_changes[key] = (old, new)
    return SemanticReplayDiff(
        state_changed=baseline.fingerprint != candidate.fingerprint,
        added_effects=tuple(sorted(candidate_effects - baseline_effects)),
        removed_effects=tuple(sorted(baseline_effects - candidate_effects)),
        position_changes=position_changes,
    )
