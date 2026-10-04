from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from .association import associate_followup_with_evidence
from .config import CHANNELS
from .domain import BookState, EffectKind, EventKind, RawDiscordMessage, SignalEvent
from .eligibility import (
    EligibilityDisposition,
    StrategyBucket,
    StrategyEligibilityPolicy,
    classify_eligibility,
)
from .history_archive import ArchivedDiscordMessage, ChannelCompleteness, HistoryArchive
from .invariants import assert_valid_book
from .parser import parse_message_with_evidence
from .pricing import entry_is_stale, entry_limit
from .quote_tape import HistoricalQuote, HistoricalQuoteTape
from .reducer import apply_fill, reduce_book

_OPTION_MULTIPLIER = Decimal("100")


class ForensicStatus(StrEnum):
    FILLED = "FILLED"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    REVIEW = "REVIEW"
    NO_QUOTE = "NO_QUOTE"
    STALE_ENTRY = "STALE_ENTRY"
    LIMIT_NOT_FILLED = "LIMIT_NOT_FILLED"
    UNAFFORDABLE = "UNAFFORDABLE"
    NO_POSITION_QUANTITY = "NO_POSITION_QUANTITY"
    NON_ACTIONABLE = "NON_ACTIONABLE"


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    standard_base_dollars: Decimal = Decimal("1500")
    high_confidence_base_dollars: Decimal = Decimal("3000")
    add_fraction_of_base: Decimal = Decimal("0.50")
    decision_latency: timedelta = timedelta(milliseconds=250)
    quote_max_lag: timedelta = timedelta(seconds=3)
    entry_limit_wait: timedelta = timedelta(seconds=5)

    def __post_init__(self) -> None:
        if self.standard_base_dollars <= 0 or self.high_confidence_base_dollars <= 0:
            raise ValueError("base dollar budgets must be positive")
        if not Decimal("0") < self.add_fraction_of_base <= Decimal("1"):
            raise ValueError("add_fraction_of_base must be in (0, 1]")
        if self.decision_latency < timedelta(0):
            raise ValueError("decision_latency cannot be negative")

    def base_budget(self, channel_id: str) -> Decimal:
        if channel_id == CHANNELS["high-confidence-options"]:
            return self.high_confidence_base_dollars
        return self.standard_base_dollars

    def add_budget(self, source_channel_id: str) -> Decimal:
        return self.base_budget(source_channel_id) * self.add_fraction_of_base


@dataclass(frozen=True, slots=True)
class ForensicLeg:
    event_id: str
    message_id: str
    channel_id: str
    author_id: str
    source_ts_utc: datetime
    event_kind: EventKind
    contract_key: str | None
    bucket: StrategyBucket
    status: ForensicStatus
    quantity: int
    reference_price: Decimal | None
    limit_price: Decimal | None
    fill_price: Decimal | None
    quote_ts_utc: datetime | None
    note: str = ""


@dataclass(frozen=True, slots=True)
class CompletedTrade:
    entry_event_id: str
    contract_key: str
    channel_id: str
    author_id: str
    bucket: StrategyBucket
    opened_ts_utc: datetime
    closed_ts_utc: datetime
    initial_quantity: int
    add_count: int
    trim_count: int
    gross_premium_in: Decimal
    gross_proceeds: Decimal
    pnl: Decimal
    return_fraction: Decimal
    holding_seconds: float


@dataclass(frozen=True, slots=True)
class ForensicsReport:
    messages_seen: int
    completeness: tuple[ChannelCompleteness, ...]
    legs: tuple[ForensicLeg, ...]
    completed_trades: tuple[CompletedTrade, ...]

    @property
    def exhaustive_channels(self) -> int:
        return sum(item.exhaustive for item in self.completeness)


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


def _entry_fill_quote(
    tape: HistoricalQuoteTape,
    contract_key: str,
    target_ts: datetime,
    reference_price: Decimal,
    profile: ExecutionProfile,
) -> tuple[ForensicStatus, HistoricalQuote | None, Decimal | None]:
    live = tape.at_or_after(contract_key, target_ts, max_lag=profile.quote_max_lag)
    if live is None:
        return ForensicStatus.NO_QUOTE, None, None
    if entry_is_stale(reference_price, live.ask):
        return ForensicStatus.STALE_ENTRY, live, None
    limit = entry_limit(reference_price, live.ask)
    if live.ask <= limit:
        return ForensicStatus.FILLED, live, limit
    fill = tape.first_ask_at_or_below(
        contract_key,
        target_ts,
        limit,
        max_wait=profile.entry_limit_wait,
    )
    if fill is None:
        return ForensicStatus.LIMIT_NOT_FILLED, live, limit
    return ForensicStatus.FILLED, fill, limit


def _record_leg(
    event: SignalEvent,
    bucket: StrategyBucket,
    status: ForensicStatus,
    *,
    quantity: int = 0,
    limit_price: Decimal | None = None,
    fill_price: Decimal | None = None,
    quote: HistoricalQuote | None = None,
    note: str = "",
) -> ForensicLeg:
    return ForensicLeg(
        event_id=event.event_id,
        message_id=event.message_id,
        channel_id=event.channel_id,
        author_id=event.author_id,
        source_ts_utc=event.source_ts_utc,
        event_kind=event.kind,
        contract_key=event.contract_key,
        bucket=bucket,
        status=status,
        quantity=quantity,
        reference_price=event.referenced_price,
        limit_price=limit_price,
        fill_price=fill_price,
        quote_ts_utc=quote.ts_utc if quote else None,
        note=note,
    )


def run_execution_forensics(
    messages: tuple[ArchivedDiscordMessage, ...],
    quote_tape: HistoricalQuoteTape,
    *,
    allowed_author_ids: frozenset[str],
    profile: ExecutionProfile | None = None,
    eligibility_policy: StrategyEligibilityPolicy | None = None,
    include_research_only: bool = False,
) -> tuple[tuple[ForensicLeg, ...], tuple[CompletedTrade, ...]]:
    profile = profile or ExecutionProfile()
    eligibility_policy = eligibility_policy or StrategyEligibilityPolicy()
    ordered = sorted(messages, key=lambda item: (item.source_ts_utc, item.message_id))
    state = BookState()
    message_contract: dict[str, str] = {}
    ledgers: dict[str, _TradeLedger] = {}
    legs: list[ForensicLeg] = []
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

        eligibility = classify_eligibility(event, eligibility_policy)
        if (
            eligibility.disposition is EligibilityDisposition.RESEARCH_ONLY
            and event.kind in {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT}
            and not include_research_only
        ):
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    ForensicStatus.RESEARCH_ONLY,
                    note=eligibility.reason,
                )
            )
            continue

        if event.kind is EventKind.IGNORE:
            continue
        if event.kind is EventKind.AMBIGUOUS:
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    ForensicStatus.REVIEW,
                    note=event.reason,
                )
            )
            continue

        proposed_state, effects = reduce_book(state, event)
        assert_valid_book(proposed_state)
        effect = effects[0] if effects else None
        if effect is None:
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    ForensicStatus.NON_ACTIONABLE,
                    note="no deterministic effect",
                )
            )
            state = proposed_state
            continue
        if effect.kind in {EffectKind.REVIEW, EffectKind.HALT}:
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    ForensicStatus.REVIEW,
                    note=effect.reason,
                )
            )
            state = proposed_state
            continue

        key = effect.contract_key
        if key is None:
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    ForensicStatus.REVIEW,
                    note="effect missing contract",
                )
            )
            continue
        target_ts = event.source_ts_utc.astimezone(UTC) + profile.decision_latency

        if effect.kind is EffectKind.PROPOSE_OPEN:
            reference = event.referenced_price
            if reference is None:
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        ForensicStatus.REVIEW,
                        note="entry missing source reference price",
                    )
                )
                continue
            entry_status, entry_quote, entry_limit_price = _entry_fill_quote(
                quote_tape,
                key,
                target_ts,
                reference,
                profile,
            )
            if entry_status is not ForensicStatus.FILLED or entry_quote is None:
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        entry_status,
                        limit_price=entry_limit_price,
                        quote=entry_quote,
                    )
                )
                continue
            if effect.reason == "first_entry_pipe_test":
                quantity = 1
            else:
                quantity = _quantity_for_budget(
                    profile.base_budget(event.channel_id),
                    entry_quote.ask,
                )
            if quantity <= 0:
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        ForensicStatus.UNAFFORDABLE,
                        limit_price=entry_limit_price,
                        quote=entry_quote,
                        note="configured clip cannot buy one contract",
                    )
                )
                continue
            filled_state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                quantity,
                entry_quote.ask,
            )
            assert_valid_book(filled_state)
            cash_in = entry_quote.ask * quantity * _OPTION_MULTIPLIER
            ledgers[key] = _TradeLedger(
                entry_event_id=event.event_id,
                contract_key=key,
                channel_id=event.channel_id,
                author_id=event.author_id,
                bucket=eligibility.bucket,
                opened_ts_utc=event.source_ts_utc,
                initial_quantity=quantity,
                gross_premium_in=cash_in,
            )
            state = filled_state
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    ForensicStatus.FILLED,
                    quantity=quantity,
                    limit_price=entry_limit_price,
                    fill_price=entry_quote.ask,
                    quote=entry_quote,
                )
            )
            continue

        position = state.positions.get(key)
        ledger = ledgers.get(key)
        if position is None or ledger is None:
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    ForensicStatus.REVIEW,
                    note="no filled historical position ledger",
                )
            )
            continue

        if effect.kind is EffectKind.PROPOSE_ADD:
            reference = event.referenced_price
            if reference is not None:
                add_status, add_quote, add_limit_price = _entry_fill_quote(
                    quote_tape,
                    key,
                    target_ts,
                    reference,
                    profile,
                )
            else:
                add_quote = quote_tape.at_or_after(
                    key,
                    target_ts,
                    max_lag=profile.quote_max_lag,
                )
                add_status = (
                    ForensicStatus.FILLED
                    if add_quote is not None
                    else ForensicStatus.NO_QUOTE
                )
                add_limit_price = add_quote.ask if add_quote is not None else None
            if add_status is not ForensicStatus.FILLED or add_quote is None:
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        add_status,
                        limit_price=add_limit_price,
                        quote=add_quote,
                        note="source add was not synthesized",
                    )
                )
                continue
            source_channel = position.source_channel_id or ledger.channel_id
            quantity = _quantity_for_budget(profile.add_budget(source_channel), add_quote.ask)
            if quantity <= 0:
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        ForensicStatus.UNAFFORDABLE,
                        limit_price=add_limit_price,
                        quote=add_quote,
                    )
                )
                continue
            filled_state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                quantity,
                add_quote.ask,
            )
            ledger.gross_premium_in += add_quote.ask * quantity * _OPTION_MULTIPLIER
            ledger.add_count += 1
            state = filled_state
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    ForensicStatus.FILLED,
                    quantity=quantity,
                    limit_price=add_limit_price,
                    fill_price=add_quote.ask,
                    quote=add_quote,
                )
            )
            continue

        exit_quote = quote_tape.at_or_after(key, target_ts, max_lag=profile.quote_max_lag)
        if exit_quote is None:
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    ForensicStatus.NO_QUOTE,
                    note="exit-side quote unavailable; no synthetic fill used",
                )
            )
            continue

        if effect.kind is EffectKind.PROPOSE_TRIM:
            quantity = max(position.quantity - 1, 0)
            if quantity == 0:
                state = proposed_state
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        ForensicStatus.NO_POSITION_QUANTITY,
                        quote=exit_quote,
                        note="already at one runner contract",
                    )
                )
                continue
            filled_state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                -quantity,
                exit_quote.bid,
            )
            ledger.gross_proceeds += exit_quote.bid * quantity * _OPTION_MULTIPLIER
            ledger.trim_count += 1
            state = filled_state
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    ForensicStatus.FILLED,
                    quantity=quantity,
                    fill_price=exit_quote.bid,
                    quote=exit_quote,
                    note="trim-to-one; no synthetic runner exit is invented",
                )
            )
            continue

        if effect.kind is EffectKind.PROPOSE_CLOSE:
            quantity = position.quantity
            if quantity <= 0:
                state = proposed_state
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        ForensicStatus.NO_POSITION_QUANTITY,
                        quote=exit_quote,
                    )
                )
                continue
            filled_state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                -quantity,
                exit_quote.bid,
                final=True,
            )
            proceeds = exit_quote.bid * quantity * _OPTION_MULTIPLIER
            ledger.gross_proceeds += proceeds
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
                    closed_ts_utc=event.source_ts_utc,
                    initial_quantity=ledger.initial_quantity,
                    add_count=ledger.add_count,
                    trim_count=ledger.trim_count,
                    gross_premium_in=ledger.gross_premium_in,
                    gross_proceeds=ledger.gross_proceeds,
                    pnl=pnl,
                    return_fraction=return_fraction,
                    holding_seconds=max(
                        0.0,
                        (event.source_ts_utc - ledger.opened_ts_utc).total_seconds(),
                    ),
                )
            )
            state = filled_state
            del ledgers[key]
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    ForensicStatus.FILLED,
                    quantity=quantity,
                    fill_price=exit_quote.bid,
                    quote=exit_quote,
                )
            )

    return tuple(legs), tuple(completed)


def run_archive_forensics(
    archive: HistoryArchive,
    quote_tape: HistoricalQuoteTape,
    *,
    channel_ids: frozenset[str],
    allowed_author_ids: frozenset[str],
    profile: ExecutionProfile | None = None,
    eligibility_policy: StrategyEligibilityPolicy | None = None,
    include_research_only: bool = False,
) -> ForensicsReport:
    messages: list[ArchivedDiscordMessage] = []
    completeness: list[ChannelCompleteness] = []
    for channel_id in sorted(channel_ids):
        messages.extend(archive.iter_channel(channel_id))
        completeness.append(archive.completeness(channel_id))
    legs, completed = run_execution_forensics(
        tuple(messages),
        quote_tape,
        allowed_author_ids=allowed_author_ids,
        profile=profile,
        eligibility_policy=eligibility_policy,
        include_research_only=include_research_only,
    )
    return ForensicsReport(
        messages_seen=len(messages),
        completeness=tuple(completeness),
        legs=legs,
        completed_trades=completed,
    )
