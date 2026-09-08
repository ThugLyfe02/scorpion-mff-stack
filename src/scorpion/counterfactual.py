from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .association import associate_followup_with_evidence
from .config import DEFAULT_POLICY, Policy
from .domain import BookState, Effect, EventKind, RawDiscordMessage, SignalEvent
from .invariants import assert_valid_book
from .parser import ParseDecision, parse_message_with_evidence
from .reducer import reduce_book
from .replay import state_fingerprint

EvidenceParser = Callable[[RawDiscordMessage, frozenset[str] | None], ParseDecision]


@dataclass(frozen=True, slots=True)
class CounterfactualRun:
    fingerprint: str
    events: tuple[SignalEvent, ...]
    effects: tuple[Effect, ...]
    review_effects: int
    ambiguous_events: int


@dataclass(frozen=True, slots=True)
class CounterfactualDelta:
    fingerprint_changed: bool
    event_kind_changes: int
    contract_changes: int
    effect_changes: int


def run_counterfactual(
    messages: Sequence[RawDiscordMessage],
    *,
    parser: EvidenceParser = parse_message_with_evidence,
    allowed_author_ids: frozenset[str] | None = None,
    policy: Policy = DEFAULT_POLICY,
) -> CounterfactualRun:
    ordered = sorted(
        messages,
        key=lambda raw: (raw.source_ts_utc, raw.received_ts_utc, raw.revision_id),
    )
    state = BookState()
    events: list[SignalEvent] = []
    effects: list[Effect] = []
    message_contracts: dict[str, str] = {}

    for raw in ordered:
        parsed = parser(raw, allowed_author_ids)
        referenced_key = (
            message_contracts.get(raw.referenced_message_id)
            if raw.referenced_message_id is not None
            else None
        )
        associated = associate_followup_with_evidence(parsed.event, state, referenced_key)
        event = associated.event
        state, produced = reduce_book(state, event, policy)
        assert_valid_book(state, policy)
        events.append(event)
        effects.extend(produced)
        if event.contract_key is not None:
            message_contracts[event.message_id] = event.contract_key

    return CounterfactualRun(
        fingerprint=state_fingerprint(state),
        events=tuple(events),
        effects=tuple(effects),
        review_effects=sum(effect.kind.value == "REVIEW" for effect in effects),
        ambiguous_events=sum(event.kind is EventKind.AMBIGUOUS for event in events),
    )


def compare_counterfactual_runs(
    baseline: CounterfactualRun,
    candidate: CounterfactualRun,
) -> CounterfactualDelta:
    event_kind_changes = sum(
        left.kind is not right.kind
        for left, right in zip(baseline.events, candidate.events, strict=False)
    ) + abs(len(baseline.events) - len(candidate.events))
    contract_changes = sum(
        left.contract_key != right.contract_key
        for left, right in zip(baseline.events, candidate.events, strict=False)
    ) + abs(len(baseline.events) - len(candidate.events))
    baseline_effects = {
        (effect.kind.value, effect.contract_key, effect.generation, effect.reason)
        for effect in baseline.effects
    }
    candidate_effects = {
        (effect.kind.value, effect.contract_key, effect.generation, effect.reason)
        for effect in candidate.effects
    }
    return CounterfactualDelta(
        fingerprint_changed=baseline.fingerprint != candidate.fingerprint,
        event_kind_changes=event_kind_changes,
        contract_changes=contract_changes,
        effect_changes=len(baseline_effects ^ candidate_effects),
    )
