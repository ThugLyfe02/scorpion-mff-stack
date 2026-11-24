from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path

from .association import associate_followup_with_evidence
from .causal_features import PaperIntentFeatureRow, load_paper_intent_features
from .config import DEFAULT_POLICY, Policy
from .domain import BookState, Effect, EventKind, RawDiscordMessage, SignalEvent
from .execution_attribution import PaperExecutionCostReport, PaperRouteCost
from .invariants import assert_valid_book
from .parser import ParseDecision, parse_message_with_evidence
from .provenance import stable_hash
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


@dataclass(frozen=True, slots=True)
class PaperNoChaseComparison:
    call_id: str
    initial_feature_sha256: str
    target_quantity: int
    status: str
    observed_route_shortfall_usd: Decimal | None
    no_chase_reference_shortfall_usd: Decimal | None
    observed_minus_no_chase_usd: Decimal | None
    observed_minus_no_chase_bps: Decimal | None
    modeled_pnl_delta: None = None


@dataclass(frozen=True, slots=True)
class PaperNoChaseReport:
    cost_report_sha256: str
    comparisons: tuple[PaperNoChaseComparison, ...]
    comparable_routes: int
    noncomparable_routes: int
    comparable_delta_usd: Decimal | None
    report_sha256: str
    authority: str = "RESEARCH_ONLY"
    estimand: str = "PATH_CONDITIONAL_ENTRY_SHORTFALL_NOT_CAUSAL_POLICY_VALUE"
    authorizes_training: bool = False
    authorizes_promotion: bool = False


def _compare_no_chase(route: PaperRouteCost) -> PaperNoChaseComparison:
    def withheld(reason: str) -> PaperNoChaseComparison:
        return PaperNoChaseComparison(
            route.call_id, route.initial_feature_sha256, route.target_quantity,
            reason, route.net_shortfall_usd, None, None, None,
        )

    if not route.complete:
        return withheld("ROUTE_NOT_QUALIFIED")
    if (
        route.initial_filled_quantity != 0 or not route.chase_filled_quantity
        or route.horizon_midpoint is None or route.initial_net_fees_usd is None
        or route.chase_net_fees_usd is None or route.chase_notional_usd is None
        or route.net_shortfall_usd is None
    ):
        return withheld("NO_OBSERVED_POSITIVE_CHASE_AFTER_ZERO_FILL")
    # Conditional reference: remove only the executed chase on this observed path,
    # retaining the initial cancellation's cost and marking the same parent quantity.
    # This does not simulate an alternative fill, an exit, or a policy intervention.
    reference = (
        route.initial_net_fees_usd
        + route.target_quantity * (route.horizon_midpoint - route.decision_midpoint)
    )
    delta = route.net_shortfall_usd - reference
    direct = (
        route.chase_notional_usd - route.chase_filled_quantity * route.horizon_midpoint
        + route.chase_net_fees_usd
    )
    if delta != direct:
        raise ValueError("no-chase decomposition mismatch")
    return PaperNoChaseComparison(
        route.call_id, route.initial_feature_sha256, route.target_quantity,
        "PATH_CONDITIONAL_COMPARABLE", route.net_shortfall_usd, reference, delta,
        delta / (route.target_quantity * route.decision_midpoint) * 10_000,
    )


def compare_paper_no_chase(cost: PaperExecutionCostReport) -> PaperNoChaseReport:
    """Consume only the verified attribution result, never treat a missing fill as zero.

    This pure diagnostic cannot establish causal effects, portfolio returns or trading
    permissions. In particular, it must not be inserted into the policy-value evaluator
    as independent challenger rewards.
    """
    with localcontext() as ctx:
        ctx.prec = 50
        if len({row.call_id for row in cost.routes}) != len(cost.routes):
            raise ValueError("duplicate paper route")
        rows = tuple(_compare_no_chase(route) for route in cost.routes)
        deltas = [
            row.observed_minus_no_chase_usd for row in rows
            if row.observed_minus_no_chase_usd is not None
        ]
        return PaperNoChaseReport(
            cost.report_sha256, rows, len(deltas), len(rows) - len(deltas),
            sum(deltas, Decimal(0)) if deltas else None,
            stable_hash({
                "version": "paper-no-chase-v1", "cost_report_sha256": cost.report_sha256,
                "comparisons": rows,
            }),
        )
