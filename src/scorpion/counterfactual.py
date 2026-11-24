from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .association import associate_followup_with_evidence
from .causal_features import PaperIntentFeatureRow, load_paper_intent_features
from .config import DEFAULT_POLICY, Policy
from .domain import BookState, Effect, EventKind, RawDiscordMessage, SignalEvent
from .invariants import assert_valid_book
from .parser import ParseDecision, parse_message_with_evidence
from .reducer import reduce_book
from .replay import semantic_state_fingerprint

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
        assert_valid_book(state, max_open_positions=policy.max_open_positions)
        events.append(event)
        effects.extend(produced)
        if event.contract_key is not None:
            message_contracts[event.message_id] = event.contract_key

    return CounterfactualRun(
        fingerprint=semantic_state_fingerprint(state),
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
    baseline_effects = Counter(
        (effect.kind.value, effect.contract_key, effect.generation, effect.reason)
        for effect in baseline.effects
    )
    candidate_effects = Counter(
        (effect.kind.value, effect.contract_key, effect.generation, effect.reason)
        for effect in candidate.effects
    )
    effect_changes = sum((baseline_effects - candidate_effects).values()) + sum(
        (candidate_effects - baseline_effects).values()
    )
    return CounterfactualDelta(
        fingerprint_changed=baseline.fingerprint != candidate.fingerprint,
        event_kind_changes=event_kind_changes,
        contract_changes=contract_changes,
        effect_changes=effect_changes,
    )


@dataclass(frozen=True, slots=True)
class PaperIntentAlternative:
    call_id: str
    ticker: str
    initial_feature_sha256: str
    chase_feature_sha256: str
    requested_limit_change_bps: Decimal
    requested_quantity_change: int
    source_age_change_ms: Decimal
    modeled_pnl_delta: None = None


@dataclass(frozen=True, slots=True)
class PaperIntentAlternativeReport:
    journal_sha256: str
    pairs: tuple[PaperIntentAlternative, ...]
    initial_without_qualified_chase: int
    unpaired_chase: int
    feature_refusals: tuple[tuple[str, int], ...]
    authority: str = "RESEARCH_ONLY"


def compare_paper_intent_alternatives(path: str | Path) -> PaperIntentAlternativeReport:
    """Compare observed initial/chase proposals, not two assumed executed trades."""
    evidence = load_paper_intent_features(path)
    by_role: dict[tuple[str, str], PaperIntentFeatureRow] = {
        (row.call_id, row.role): row for row in evidence.rows
    }
    initials = {key for key, role in by_role if role == "INITIAL"}
    chases = {key for key, role in by_role if role == "CHASE"}
    pairs: list[PaperIntentAlternative] = []
    for key in sorted(initials & chases):
        first, second = by_role[(key, "INITIAL")], by_role[(key, "CHASE")]
        if (
            first.ticker != second.ticker or first.rules_sha256 != second.rules_sha256
            or first.intent_seq >= second.intent_seq
        ):
            raise ValueError("counterfactual intent identity mismatch")
        initial = Decimal(str(first.features["limit"]))
        chase = Decimal(str(second.features["limit"]))
        pairs.append(PaperIntentAlternative(
            call_id=key, ticker=first.ticker,
            initial_feature_sha256=first.feature_sha256,
            chase_feature_sha256=second.feature_sha256,
            requested_limit_change_bps=(chase - initial) / initial * 10_000,
            requested_quantity_change=(
                int(second.features["quantity"]) - int(first.features["quantity"])
            ),
            source_age_change_ms=(
                Decimal(str(second.features["source_age_ms"]))
                - Decimal(str(first.features["source_age_ms"]))
            ),
        ))
    return PaperIntentAlternativeReport(
        evidence.journal_sha256, tuple(pairs), len(initials - chases),
        len(chases - initials), evidence.refusals,
    )
