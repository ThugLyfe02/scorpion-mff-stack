from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from .association import associate_followup_with_evidence
from .config import CHANNELS
from .domain import BookState, EffectKind, EventKind, RawDiscordMessage
from .eligibility import (
    EligibilityDisposition,
    StrategyBucket,
    StrategyEligibilityPolicy,
    classify_eligibility,
)
from .execution_forensics import CompletedTrade
from .history_archive import ArchivedDiscordMessage, ChannelCompleteness, HistoryArchive
from .invariants import assert_valid_book
from .microstructure import (
    FillCertainty,
    MicroFillEnvelope,
    OptionMicrostructureTape,
    datetime_from_ns,
)
from .parser import parse_message_with_evidence
from .policy_bundle import RuntimePolicyBundle
from .pricing import entry_is_stale, entry_limit
from .reducer import apply_fill, reduce_book

_OPTION_MULTIPLIER = Decimal("100")


class MicroForensicStatus(StrEnum):
    CERTIFIED_FILL = "CERTIFIED_FILL"
    CERTIFIED_PARTIAL = "CERTIFIED_PARTIAL"
    RESTING_FILL_UNCERTAIN = "RESTING_FILL_UNCERTAIN"
    DEPTH_UNKNOWN = "DEPTH_UNKNOWN"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    REVIEW = "REVIEW"
    NO_DECISION_QUOTE = "NO_DECISION_QUOTE"
    NO_MARKET_STATE = "NO_MARKET_STATE"
    STALE_ENTRY = "STALE_ENTRY"
    LIMIT_NOT_CERTIFIED = "LIMIT_NOT_CERTIFIED"
    UNAFFORDABLE = "UNAFFORDABLE"
    NO_POSITION_QUANTITY = "NO_POSITION_QUANTITY"
    NON_ACTIONABLE = "NON_ACTIONABLE"


@dataclass(frozen=True, slots=True)
class MicrostructureExecutionProfile:
    standard_base_dollars: Decimal = Decimal("1500")
    high_confidence_base_dollars: Decimal = Decimal("3000")
    add_fraction_of_base: Decimal = Decimal("0.50")
    decision_latency: timedelta = timedelta(milliseconds=250)
    order_transport_latency: timedelta = timedelta(0)
    feed_transport_latency: timedelta = timedelta(0)
    decision_quote_max_age: timedelta = timedelta(seconds=1)
    market_quote_max_age: timedelta = timedelta(seconds=1)
    entry_limit_wait: timedelta = timedelta(seconds=5)

    def __post_init__(self) -> None:
        if self.standard_base_dollars <= 0 or self.high_confidence_base_dollars <= 0:
            raise ValueError("base dollar budgets must be positive")
        if not Decimal("0") < self.add_fraction_of_base <= Decimal("1"):
            raise ValueError("add_fraction_of_base must be in (0,1]")
        for name in (
            "decision_latency",
            "order_transport_latency",
            "feed_transport_latency",
            "decision_quote_max_age",
            "market_quote_max_age",
            "entry_limit_wait",
        ):
            if getattr(self, name) < timedelta(0):
                raise ValueError(f"{name} cannot be negative")

    def base_budget(self, channel_id: str) -> Decimal:
        if channel_id == CHANNELS["high-confidence-options"]:
            return self.high_confidence_base_dollars
        return self.standard_base_dollars

    def add_budget(self, source_channel_id: str) -> Decimal:
        return self.base_budget(source_channel_id) * self.add_fraction_of_base


@dataclass(frozen=True, slots=True)
class MicroForensicLeg:
    event_id: str
    message_id: str
    event_kind: EventKind
    contract_key: str | None
    bucket: StrategyBucket
    status: MicroForensicStatus
    source_ts_utc: datetime
    decision_ts_utc: datetime | None
    decision_quote_event_ns: int | None
    decision_quote_recv_ns: int | None
    decision_quote_bid: Decimal | None
    decision_quote_ask: Decimal | None
    limit_price: Decimal | None
    order_arrival_ts_utc: datetime | None
    requested_quantity: int
    conservative_fill_quantity: int
    possible_fill_quantity: int
    fill_price: Decimal | None
    fill_certainty: FillCertainty | None
    fill_evidence_event_ns: int | None
    fill_evidence_publisher_id: int | None
    note: str = ""


@dataclass(frozen=True, slots=True)
class MicrostructureForensicsReport:
    messages_seen: int
    completeness: tuple[ChannelCompleteness, ...]
    legs: tuple[MicroForensicLeg, ...]
    completed_trades: tuple[CompletedTrade, ...]
    policy_fingerprint: str
    certified_actionable_legs: int
    uncertain_actionable_legs: int


@dataclass(slots=True)
class _TradeLedger:
    entry_event_id: str
    contract_key: str
    channel_id: str
    author_id: str
    bucket: StrategyBucket
    opened_ts_utc: datetime
    initial_quantity: int
    gross_premium_in: Decimal
    gross_proceeds: Decimal = Decimal("0")
    add_count: int = 0
    trim_count: int = 0
    execution_evidence_complete: bool = True


def _to_raw(message: ArchivedDiscordMessage) -> RawDiscordMessage:
    return RawDiscordMessage(
        message_id=message.message_id,
        guild_id=message.guild_id,
        channel_id=message.channel_id,
        author_id=message.author_id,
        content=message.content,
        source_ts_utc=message.source_ts_utc,
        received_ts_utc=message.source_ts_utc,
        edited_ts_utc=message.edited_ts_utc,
        referenced_message_id=message.referenced_message_id,
    )


def _quantity_for_budget(budget: Decimal, option_price: Decimal) -> int:
    contract_cost = option_price * _OPTION_MULTIPLIER
    if contract_cost <= 0:
        return 0
    return int((budget / contract_cost).to_integral_value(rounding=ROUND_FLOOR))


def _status_from_fill(fill: MicroFillEnvelope) -> MicroForensicStatus:
    if fill.certainty is FillCertainty.AGGRESSIVE_DISPLAYED_COMPLETE:
        return MicroForensicStatus.CERTIFIED_FILL
    if fill.certainty is FillCertainty.AGGRESSIVE_DISPLAYED_PARTIAL:
        return MicroForensicStatus.CERTIFIED_PARTIAL
    if fill.certainty is FillCertainty.RESTING_QUEUE_UNKNOWN:
        return MicroForensicStatus.RESTING_FILL_UNCERTAIN
    if fill.certainty is FillCertainty.AGGRESSIVE_DEPTH_UNKNOWN:
        return MicroForensicStatus.DEPTH_UNKNOWN
    if fill.certainty is FillCertainty.NO_MARKET_STATE:
        return MicroForensicStatus.NO_MARKET_STATE
    return MicroForensicStatus.LIMIT_NOT_CERTIFIED


def _leg(
    *,
    event_id: str,
    message_id: str,
    event_kind: EventKind,
    contract_key: str | None,
    bucket: StrategyBucket,
    status: MicroForensicStatus,
    source_ts_utc: datetime,
    decision_ts_utc: datetime | None = None,
    decision_quote_event_ns: int | None = None,
    decision_quote_recv_ns: int | None = None,
    decision_quote_bid: Decimal | None = None,
    decision_quote_ask: Decimal | None = None,
    limit_price: Decimal | None = None,
    order_arrival_ts_utc: datetime | None = None,
    requested_quantity: int = 0,
    conservative_fill_quantity: int = 0,
    possible_fill_quantity: int = 0,
    fill_price: Decimal | None = None,
    fill_certainty: FillCertainty | None = None,
    fill_evidence_event_ns: int | None = None,
    fill_evidence_publisher_id: int | None = None,
    note: str = "",
) -> MicroForensicLeg:
    return MicroForensicLeg(
        event_id=event_id,
        message_id=message_id,
        event_kind=event_kind,
        contract_key=contract_key,
        bucket=bucket,
        status=status,
        source_ts_utc=source_ts_utc,
        decision_ts_utc=decision_ts_utc,
        decision_quote_event_ns=decision_quote_event_ns,
        decision_quote_recv_ns=decision_quote_recv_ns,
        decision_quote_bid=decision_quote_bid,
        decision_quote_ask=decision_quote_ask,
        limit_price=limit_price,
        order_arrival_ts_utc=order_arrival_ts_utc,
        requested_quantity=requested_quantity,
        conservative_fill_quantity=conservative_fill_quantity,
        possible_fill_quantity=possible_fill_quantity,
        fill_price=fill_price,
        fill_certainty=fill_certainty,
        fill_evidence_event_ns=fill_evidence_event_ns,
        fill_evidence_publisher_id=fill_evidence_publisher_id,
        note=note,
    )


def _fill_leg(
    event_id: str,
    message_id: str,
    event_kind: EventKind,
    contract_key: str,
    bucket: StrategyBucket,
    source_ts_utc: datetime,
    decision_ts: datetime,
    decision_quote_event_ns: int,
    decision_quote_recv_ns: int,
    decision_bid: Decimal,
    decision_ask: Decimal,
    limit_price: Decimal | None,
    order_arrival: datetime,
    fill: MicroFillEnvelope,
    *,
    note: str = "",
) -> MicroForensicLeg:
    return _leg(
        event_id=event_id,
        message_id=message_id,
        event_kind=event_kind,
        contract_key=contract_key,
        bucket=bucket,
        status=_status_from_fill(fill),
        source_ts_utc=source_ts_utc,
        decision_ts_utc=decision_ts,
        decision_quote_event_ns=decision_quote_event_ns,
        decision_quote_recv_ns=decision_quote_recv_ns,
        decision_quote_bid=decision_bid,
        decision_quote_ask=decision_ask,
        limit_price=limit_price,
        order_arrival_ts_utc=order_arrival,
        requested_quantity=fill.requested_quantity,
        conservative_fill_quantity=fill.lower_bound_quantity,
        possible_fill_quantity=fill.upper_bound_quantity,
        fill_price=fill.modeled_price,
        fill_certainty=fill.certainty,
        fill_evidence_event_ns=fill.evidence_event_ns,
        fill_evidence_publisher_id=fill.evidence_publisher_id,
        note="; ".join(part for part in (note, fill.reason) if part),
    )


def run_microstructure_forensics(
    messages: tuple[ArchivedDiscordMessage, ...],
    tape: OptionMicrostructureTape,
    *,
    allowed_author_ids: frozenset[str],
    profile: MicrostructureExecutionProfile | None = None,
    eligibility_policy: StrategyEligibilityPolicy | None = None,
    runtime_policy: RuntimePolicyBundle | None = None,
    include_research_only: bool = False,
) -> tuple[tuple[MicroForensicLeg, ...], tuple[CompletedTrade, ...]]:
    profile = profile or MicrostructureExecutionProfile()
    if runtime_policy is not None and eligibility_policy is not None:
        raise ValueError("pass runtime_policy or eligibility_policy, not both")
    policy_bundle = runtime_policy or RuntimePolicyBundle(
        eligibility=eligibility_policy or StrategyEligibilityPolicy()
    )
    ordered = sorted(messages, key=lambda item: (item.source_ts_utc, item.message_id))
    state = BookState()
    message_contract: dict[str, str] = {}
    ledgers: dict[str, _TradeLedger] = {}
    legs: list[MicroForensicLeg] = []
    completed: list[CompletedTrade] = []

    for archived in ordered:
        raw = _to_raw(archived)
        parsed = parse_message_with_evidence(raw, allowed_author_ids)
        referenced_key = (
            message_contract.get(raw.referenced_message_id)
            if raw.referenced_message_id is not None
            else None
        )
        associated = associate_followup_with_evidence(parsed.event, state, referenced_key)
        event = associated.event
        if event.contract_key is not None:
            message_contract[event.message_id] = event.contract_key
        eligibility = classify_eligibility(event, policy_bundle.eligibility)

        if (
            eligibility.disposition is EligibilityDisposition.RESEARCH_ONLY
            and event.kind in {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT}
            and not include_research_only
        ):
            legs.append(
                _leg(
                    event_id=event.event_id,
                    message_id=event.message_id,
                    event_kind=event.kind,
                    contract_key=event.contract_key,
                    bucket=eligibility.bucket,
                    status=MicroForensicStatus.RESEARCH_ONLY,
                    source_ts_utc=event.source_ts_utc,
                    note=eligibility.reason,
                )
            )
            continue
        if event.kind is EventKind.IGNORE:
            continue
        if event.kind is EventKind.AMBIGUOUS:
            legs.append(
                _leg(
                    event_id=event.event_id,
                    message_id=event.message_id,
                    event_kind=event.kind,
                    contract_key=event.contract_key,
                    bucket=eligibility.bucket,
                    status=MicroForensicStatus.REVIEW,
                    source_ts_utc=event.source_ts_utc,
                    note=event.reason,
                )
            )
            continue

        proposed_state, effects = reduce_book(state, event, policy_bundle.base)
        assert_valid_book(
            proposed_state,
            max_open_positions=policy_bundle.base.max_open_positions,
        )
        effect = effects[0] if effects else None
        if effect is None:
            state = proposed_state
            legs.append(
                _leg(
                    event_id=event.event_id,
                    message_id=event.message_id,
                    event_kind=event.kind,
                    contract_key=event.contract_key,
                    bucket=eligibility.bucket,
                    status=MicroForensicStatus.NON_ACTIONABLE,
                    source_ts_utc=event.source_ts_utc,
                    note="no deterministic effect",
                )
            )
            continue
        if effect.kind in {EffectKind.REVIEW, EffectKind.HALT}:
            state = proposed_state
            legs.append(
                _leg(
                    event_id=event.event_id,
                    message_id=event.message_id,
                    event_kind=event.kind,
                    contract_key=event.contract_key,
                    bucket=eligibility.bucket,
                    status=MicroForensicStatus.REVIEW,
                    source_ts_utc=event.source_ts_utc,
                    note=effect.reason,
                )
            )
            continue
        key = effect.contract_key
        if key is None:
            legs.append(
                _leg(
                    event_id=event.event_id,
                    message_id=event.message_id,
                    event_kind=event.kind,
                    contract_key=None,
                    bucket=eligibility.bucket,
                    status=MicroForensicStatus.REVIEW,
                    source_ts_utc=event.source_ts_utc,
                    note="effect missing contract",
                )
            )
            continue

        decision_ts = event.source_ts_utc.astimezone(UTC) + profile.decision_latency
        order_arrival = decision_ts + profile.order_transport_latency
        decision = tape.decision_quote(
            key,
            decision_ts,
            feed_transport_latency=profile.feed_transport_latency,
            max_age=profile.decision_quote_max_age,
        )
        if decision is None or decision.quote.bid is None or decision.quote.ask is None:
            legs.append(
                _leg(
                    event_id=event.event_id,
                    message_id=event.message_id,
                    event_kind=event.kind,
                    contract_key=key,
                    bucket=eligibility.bucket,
                    status=MicroForensicStatus.NO_DECISION_QUOTE,
                    source_ts_utc=event.source_ts_utc,
                    decision_ts_utc=decision_ts,
                    order_arrival_ts_utc=order_arrival,
                    note="no quote was causally available to the strategy at decision time",
                )
            )
            continue
        quote = decision.quote

        if effect.kind is EffectKind.PROPOSE_OPEN:
            reference = event.referenced_price
            if reference is None:
                legs.append(
                    _leg(
                        event_id=event.event_id,
                        message_id=event.message_id,
                        event_kind=event.kind,
                        contract_key=key,
                        bucket=eligibility.bucket,
                        status=MicroForensicStatus.REVIEW,
                        source_ts_utc=event.source_ts_utc,
                        decision_ts_utc=decision_ts,
                        note="entry missing source reference price",
                    )
                )
                continue
            if entry_is_stale(reference, quote.ask):
                legs.append(
                    _leg(
                        event_id=event.event_id,
                        message_id=event.message_id,
                        event_kind=event.kind,
                        contract_key=key,
                        bucket=eligibility.bucket,
                        status=MicroForensicStatus.STALE_ENTRY,
                        source_ts_utc=event.source_ts_utc,
                        decision_ts_utc=decision_ts,
                        decision_quote_event_ns=quote.ts_event_ns,
                        decision_quote_recv_ns=quote.ts_recv_ns,
                        decision_quote_bid=quote.bid,
                        decision_quote_ask=quote.ask,
                        order_arrival_ts_utc=order_arrival,
                        note="causally available ask exceeded the +25% source-price dislocation gate",
                    )
                )
                continue
            limit = entry_limit(reference, quote.ask)
            requested = (
                1
                if effect.reason == "first_entry_pipe_test"
                else _quantity_for_budget(profile.base_budget(event.channel_id), quote.ask)
            )
            if requested <= 0:
                legs.append(
                    _leg(
                        event_id=event.event_id,
                        message_id=event.message_id,
                        event_kind=event.kind,
                        contract_key=key,
                        bucket=eligibility.bucket,
                        status=MicroForensicStatus.UNAFFORDABLE,
                        source_ts_utc=event.source_ts_utc,
                        decision_ts_utc=decision_ts,
                        decision_quote_event_ns=quote.ts_event_ns,
                        decision_quote_recv_ns=quote.ts_recv_ns,
                        decision_quote_bid=quote.bid,
                        decision_quote_ask=quote.ask,
                        limit_price=limit,
                        order_arrival_ts_utc=order_arrival,
                    )
                )
                continue
            fill = tape.simulate_limit_buy(
                key,
                order_arrival_ts_utc=order_arrival,
                quantity=requested,
                limit_price=limit,
                max_wait=profile.entry_limit_wait,
                market_quote_max_age=profile.market_quote_max_age,
            )
            legs.append(
                _fill_leg(
                    event.event_id,
                    event.message_id,
                    event.kind,
                    key,
                    eligibility.bucket,
                    event.source_ts_utc,
                    decision_ts,
                    quote.ts_event_ns,
                    quote.ts_recv_ns,
                    quote.bid,
                    quote.ask,
                    limit,
                    order_arrival,
                    fill,
                )
            )
            if fill.lower_bound_quantity <= 0 or fill.modeled_price is None:
                continue
            filled_state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                fill.lower_bound_quantity,
                fill.modeled_price,
            )
            cash_in = fill.modeled_price * fill.lower_bound_quantity * _OPTION_MULTIPLIER
            opened = datetime_from_ns(fill.evidence_event_ns or fill.order_arrival_ns)
            ledgers[key] = _TradeLedger(
                entry_event_id=event.event_id,
                contract_key=key,
                channel_id=event.channel_id,
                author_id=event.author_id,
                bucket=eligibility.bucket,
                opened_ts_utc=opened,
                initial_quantity=fill.lower_bound_quantity,
                gross_premium_in=cash_in,
                execution_evidence_complete=fill.conservative_complete,
            )
            state = filled_state
            continue

        position = state.positions.get(key)
        ledger = ledgers.get(key)
        if position is None or ledger is None:
            legs.append(
                _leg(
                    event_id=event.event_id,
                    message_id=event.message_id,
                    event_kind=event.kind,
                    contract_key=key,
                    bucket=eligibility.bucket,
                    status=MicroForensicStatus.REVIEW,
                    source_ts_utc=event.source_ts_utc,
                    decision_ts_utc=decision_ts,
                    note="no certified historical position exists for follow-up",
                )
            )
            continue

        if effect.kind is EffectKind.PROPOSE_ADD:
            reference = event.referenced_price
            if reference is not None and entry_is_stale(reference, quote.ask):
                legs.append(
                    _leg(
                        event_id=event.event_id,
                        message_id=event.message_id,
                        event_kind=event.kind,
                        contract_key=key,
                        bucket=eligibility.bucket,
                        status=MicroForensicStatus.STALE_ENTRY,
                        source_ts_utc=event.source_ts_utc,
                        decision_ts_utc=decision_ts,
                        decision_quote_event_ns=quote.ts_event_ns,
                        decision_quote_recv_ns=quote.ts_recv_ns,
                        decision_quote_bid=quote.bid,
                        decision_quote_ask=quote.ask,
                        order_arrival_ts_utc=order_arrival,
                        note="source add failed the same dislocation gate as entries",
                    )
                )
                continue
            limit = entry_limit(reference, quote.ask) if reference is not None else quote.ask
            source_channel = position.source_channel_id or ledger.channel_id
            requested = _quantity_for_budget(profile.add_budget(source_channel), quote.ask)
            if requested <= 0:
                legs.append(
                    _leg(
                        event_id=event.event_id,
                        message_id=event.message_id,
                        event_kind=event.kind,
                        contract_key=key,
                        bucket=eligibility.bucket,
                        status=MicroForensicStatus.UNAFFORDABLE,
                        source_ts_utc=event.source_ts_utc,
                        decision_ts_utc=decision_ts,
                    )
                )
                continue
            fill = tape.simulate_limit_buy(
                key,
                order_arrival_ts_utc=order_arrival,
                quantity=requested,
                limit_price=limit,
                max_wait=profile.entry_limit_wait,
                market_quote_max_age=profile.market_quote_max_age,
            )
            legs.append(
                _fill_leg(
                    event.event_id,
                    event.message_id,
                    event.kind,
                    key,
                    eligibility.bucket,
                    event.source_ts_utc,
                    decision_ts,
                    quote.ts_event_ns,
                    quote.ts_recv_ns,
                    quote.bid,
                    quote.ask,
                    limit,
                    order_arrival,
                    fill,
                    note="source add only; no synthetic averaging",
                )
            )
            if fill.lower_bound_quantity <= 0 or fill.modeled_price is None:
                continue
            state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                fill.lower_bound_quantity,
                fill.modeled_price,
            )
            ledger.gross_premium_in += (
                fill.modeled_price * fill.lower_bound_quantity * _OPTION_MULTIPLIER
            )
            ledger.add_count += 1
            ledger.execution_evidence_complete = (
                ledger.execution_evidence_complete and fill.conservative_complete
            )
            continue

        if effect.kind is EffectKind.PROPOSE_TRIM:
            requested = max(position.quantity - 1, 0)
            if requested <= 0:
                state = proposed_state
                legs.append(
                    _leg(
                        event_id=event.event_id,
                        message_id=event.message_id,
                        event_kind=event.kind,
                        contract_key=key,
                        bucket=eligibility.bucket,
                        status=MicroForensicStatus.NO_POSITION_QUANTITY,
                        source_ts_utc=event.source_ts_utc,
                        decision_ts_utc=decision_ts,
                        note="already at one runner contract",
                    )
                )
                continue
            fill = tape.simulate_aggressive_sell(
                key,
                order_arrival_ts_utc=order_arrival,
                quantity=requested,
                market_quote_max_age=profile.market_quote_max_age,
            )
            legs.append(
                _fill_leg(
                    event.event_id,
                    event.message_id,
                    event.kind,
                    key,
                    eligibility.bucket,
                    event.source_ts_utc,
                    decision_ts,
                    quote.ts_event_ns,
                    quote.ts_recv_ns,
                    quote.bid,
                    quote.ask,
                    None,
                    order_arrival,
                    fill,
                    note="trim-to-one; no synthetic runner exit",
                )
            )
            if fill.lower_bound_quantity <= 0 or fill.modeled_price is None:
                continue
            state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                -fill.lower_bound_quantity,
                fill.modeled_price,
            )
            ledger.gross_proceeds += (
                fill.modeled_price * fill.lower_bound_quantity * _OPTION_MULTIPLIER
            )
            ledger.trim_count += 1
            ledger.execution_evidence_complete = (
                ledger.execution_evidence_complete and fill.conservative_complete
            )
            continue

        if effect.kind is EffectKind.PROPOSE_CLOSE:
            requested = position.quantity
            if requested <= 0:
                state = proposed_state
                legs.append(
                    _leg(
                        event_id=event.event_id,
                        message_id=event.message_id,
                        event_kind=event.kind,
                        contract_key=key,
                        bucket=eligibility.bucket,
                        status=MicroForensicStatus.NO_POSITION_QUANTITY,
                        source_ts_utc=event.source_ts_utc,
                        decision_ts_utc=decision_ts,
                    )
                )
                continue
            fill = tape.simulate_aggressive_sell(
                key,
                order_arrival_ts_utc=order_arrival,
                quantity=requested,
                market_quote_max_age=profile.market_quote_max_age,
            )
            legs.append(
                _fill_leg(
                    event.event_id,
                    event.message_id,
                    event.kind,
                    key,
                    eligibility.bucket,
                    event.source_ts_utc,
                    decision_ts,
                    quote.ts_event_ns,
                    quote.ts_recv_ns,
                    quote.bid,
                    quote.ask,
                    None,
                    order_arrival,
                    fill,
                    note=(
                        "full close"
                        if fill.conservative_complete
                        else "close not fully supported by displayed depth"
                    ),
                )
            )
            if fill.lower_bound_quantity <= 0 or fill.modeled_price is None:
                continue
            complete_close = fill.conservative_complete
            state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                -fill.lower_bound_quantity,
                fill.modeled_price,
                final=complete_close,
            )
            ledger.gross_proceeds += (
                fill.modeled_price * fill.lower_bound_quantity * _OPTION_MULTIPLIER
            )
            ledger.execution_evidence_complete = (
                ledger.execution_evidence_complete and complete_close
            )
            if complete_close:
                closed = datetime_from_ns(fill.evidence_event_ns or fill.order_arrival_ns)
                pnl = ledger.gross_proceeds - ledger.gross_premium_in
                return_fraction = (
                    pnl / ledger.gross_premium_in
                    if ledger.gross_premium_in > 0
                    else Decimal("0")
                )
                completed.append(
                    CompletedTrade(
                        entry_event_id=ledger.entry_event_id,
                        contract_key=key,
                        channel_id=ledger.channel_id,
                        author_id=ledger.author_id,
                        bucket=ledger.bucket,
                        opened_ts_utc=ledger.opened_ts_utc,
                        closed_ts_utc=closed,
                        initial_quantity=ledger.initial_quantity,
                        add_count=ledger.add_count,
                        trim_count=ledger.trim_count,
                        gross_premium_in=ledger.gross_premium_in,
                        gross_proceeds=ledger.gross_proceeds,
                        pnl=pnl,
                        return_fraction=return_fraction,
                        holding_seconds=max(
                            0.0,
                            (closed - ledger.opened_ts_utc).total_seconds(),
                        ),
                        depth_evidence_complete=ledger.execution_evidence_complete,
                    )
                )
                del ledgers[key]

    return tuple(legs), tuple(completed)


def run_archive_microstructure_forensics(
    archive: HistoryArchive,
    tape: OptionMicrostructureTape,
    *,
    channel_ids: frozenset[str],
    allowed_author_ids: frozenset[str],
    profile: MicrostructureExecutionProfile | None = None,
    eligibility_policy: StrategyEligibilityPolicy | None = None,
    runtime_policy: RuntimePolicyBundle | None = None,
    include_research_only: bool = False,
) -> MicrostructureForensicsReport:
    messages: list[ArchivedDiscordMessage] = []
    completeness: list[ChannelCompleteness] = []
    for channel_id in sorted(channel_ids):
        messages.extend(archive.iter_channel(channel_id))
        completeness.append(archive.completeness(channel_id))
    if runtime_policy is not None and eligibility_policy is not None:
        raise ValueError("pass runtime_policy or eligibility_policy, not both")
    policy_bundle = runtime_policy or RuntimePolicyBundle(
        eligibility=eligibility_policy or StrategyEligibilityPolicy()
    )
    legs, completed = run_microstructure_forensics(
        tuple(messages),
        tape,
        allowed_author_ids=allowed_author_ids,
        profile=profile,
        runtime_policy=policy_bundle,
        include_research_only=include_research_only,
    )
    actionable = {
        EventKind.ENTRY,
        EventKind.ADD,
        EventKind.TRIM,
        EventKind.EXIT,
    }
    certified = sum(
        leg.event_kind in actionable
        and leg.status
        in {MicroForensicStatus.CERTIFIED_FILL, MicroForensicStatus.CERTIFIED_PARTIAL}
        for leg in legs
    )
    uncertain = sum(
        leg.event_kind in actionable
        and leg.status
        in {
            MicroForensicStatus.RESTING_FILL_UNCERTAIN,
            MicroForensicStatus.DEPTH_UNKNOWN,
            MicroForensicStatus.LIMIT_NOT_CERTIFIED,
            MicroForensicStatus.NO_MARKET_STATE,
        }
        for leg in legs
    )
    return MicrostructureForensicsReport(
        messages_seen=len(messages),
        completeness=tuple(completeness),
        legs=legs,
        completed_trades=completed,
        policy_fingerprint=policy_bundle.fingerprint,
        certified_actionable_legs=certified,
        uncertain_actionable_legs=uncertain,
    )
