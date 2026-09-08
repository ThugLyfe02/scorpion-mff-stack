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
from .policy_bundle import RuntimePolicyBundle
from .pricing import entry_is_stale, entry_limit
from .quote_tape import HistoricalFillEvidence, HistoricalQuote, HistoricalQuoteTape
from .reducer import apply_fill, reduce_book

_OPTION_MULTIPLIER = Decimal("100")


class ForensicStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL_DEPTH = "PARTIAL_DEPTH"
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
    requested_quantity: int = 0
    depth_known: bool = False


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
    depth_evidence_complete: bool = False


@dataclass(frozen=True, slots=True)
class ForensicsReport:
    messages_seen: int
    completeness: tuple[ChannelCompleteness, ...]
    legs: tuple[ForensicLeg, ...]
    completed_trades: tuple[CompletedTrade, ...]
    policy_fingerprint: str = ""

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
    depth_evidence_complete: bool = True


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


def _entry_quote_and_limit(
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
    fill_quote = tape.first_ask_at_or_below(
        contract_key,
        target_ts,
        limit,
        max_wait=profile.entry_limit_wait,
    )
    if fill_quote is None:
        return ForensicStatus.LIMIT_NOT_FILLED, live, limit
    return ForensicStatus.FILLED, fill_quote, limit


def _record_leg(
    event: SignalEvent,
    bucket: StrategyBucket,
    status: ForensicStatus,
    *,
    quantity: int = 0,
    requested_quantity: int = 0,
    limit_price: Decimal | None = None,
    fill_price: Decimal | None = None,
    quote: HistoricalQuote | None = None,
    depth_known: bool = False,
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
        requested_quantity=requested_quantity,
        depth_known=depth_known,
    )


def _fill_status(fill: HistoricalFillEvidence) -> ForensicStatus:
    return ForensicStatus.FILLED if fill.complete else ForensicStatus.PARTIAL_DEPTH


def _depth_note(fill: HistoricalFillEvidence, base: str = "") -> str:
    details: list[str] = []
    if base:
        details.append(base)
    if not fill.depth_known:
        details.append("top-of-book size unavailable; depth evidence unknown")
    elif not fill.complete:
        details.append(
            f"displayed depth filled {fill.filled_quantity}/{fill.requested_quantity} contracts"
        )
    return "; ".join(details)


def run_execution_forensics(
    messages: tuple[ArchivedDiscordMessage, ...],
    quote_tape: HistoricalQuoteTape,
    *,
    allowed_author_ids: frozenset[str],
    profile: ExecutionProfile | None = None,
    eligibility_policy: StrategyEligibilityPolicy | None = None,
    runtime_policy: RuntimePolicyBundle | None = None,
    include_research_only: bool = False,
) -> tuple[tuple[ForensicLeg, ...], tuple[CompletedTrade, ...]]:
    profile = profile or ExecutionProfile()
    if runtime_policy is not None and eligibility_policy is not None:
        raise ValueError("pass runtime_policy or eligibility_policy, not both")
    policy_bundle = runtime_policy or RuntimePolicyBundle(
        eligibility=eligibility_policy or StrategyEligibilityPolicy()
    )
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

        eligibility = classify_eligibility(event, policy_bundle.eligibility)
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

        proposed_state, effects = reduce_book(state, event, policy_bundle.base)
        assert_valid_book(
            proposed_state,
            max_open_positions=policy_bundle.base.max_open_positions,
        )
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
            entry_status, entry_quote, entry_limit_price = _entry_quote_and_limit(
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
            requested = (
                1
                if effect.reason == "first_entry_pipe_test"
                else _quantity_for_budget(profile.base_budget(event.channel_id), entry_quote.ask)
            )
            if requested <= 0:
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
            if entry_limit_price is None:
                raise RuntimeError("filled entry must have a limit price")
            fill = quote_tape.bounded_buy_fill(
                key,
                target_ts,
                quantity=requested,
                limit_price=entry_limit_price,
                max_wait=profile.entry_limit_wait,
            )
            if fill is None or fill.filled_quantity <= 0:
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        ForensicStatus.LIMIT_NOT_FILLED,
                        requested_quantity=requested,
                        limit_price=entry_limit_price,
                        quote=entry_quote,
                    )
                )
                continue
            filled_quantity = fill.filled_quantity
            filled_state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                filled_quantity,
                fill.price,
            )
            assert_valid_book(
                filled_state,
                max_open_positions=policy_bundle.base.max_open_positions,
            )
            cash_in = fill.price * filled_quantity * _OPTION_MULTIPLIER
            ledgers[key] = _TradeLedger(
                entry_event_id=event.event_id,
                contract_key=key,
                channel_id=event.channel_id,
                author_id=event.author_id,
                bucket=eligibility.bucket,
                opened_ts_utc=event.source_ts_utc,
                initial_quantity=filled_quantity,
                gross_premium_in=cash_in,
                depth_evidence_complete=fill.depth_known and fill.complete,
            )
            state = filled_state
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    _fill_status(fill),
                    quantity=filled_quantity,
                    requested_quantity=requested,
                    limit_price=entry_limit_price,
                    fill_price=fill.price,
                    quote=fill.quote,
                    depth_known=fill.depth_known,
                    note=_depth_note(fill),
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
                add_status, add_quote, add_limit_price = _entry_quote_and_limit(
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
            requested = _quantity_for_budget(profile.add_budget(source_channel), add_quote.ask)
            if requested <= 0:
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
            if add_limit_price is None:
                raise RuntimeError("filled add must have a limit price")
            fill = quote_tape.bounded_buy_fill(
                key,
                target_ts,
                quantity=requested,
                limit_price=add_limit_price,
                max_wait=profile.entry_limit_wait,
            )
            if fill is None or fill.filled_quantity <= 0:
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        ForensicStatus.LIMIT_NOT_FILLED,
                        requested_quantity=requested,
                        limit_price=add_limit_price,
                        quote=add_quote,
                        note="source add was not synthesized",
                    )
                )
                continue
            filled_state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                fill.filled_quantity,
                fill.price,
            )
            ledger.gross_premium_in += (
                fill.price * fill.filled_quantity * _OPTION_MULTIPLIER
            )
            ledger.add_count += 1
            ledger.depth_evidence_complete = (
                ledger.depth_evidence_complete and fill.depth_known and fill.complete
            )
            state = filled_state
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    _fill_status(fill),
                    quantity=fill.filled_quantity,
                    requested_quantity=requested,
                    limit_price=add_limit_price,
                    fill_price=fill.price,
                    quote=fill.quote,
                    depth_known=fill.depth_known,
                    note=_depth_note(fill, "source add only; no synthetic averaging"),
                )
            )
            continue

        if effect.kind is EffectKind.PROPOSE_TRIM:
            requested = max(position.quantity - 1, 0)
            if requested == 0:
                state = proposed_state
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        ForensicStatus.NO_POSITION_QUANTITY,
                        note="already at one runner contract",
                    )
                )
                continue
            fill = quote_tape.bounded_sell_fill(
                key,
                target_ts,
                quantity=requested,
                max_lag=profile.quote_max_lag,
            )
            if fill is None or fill.filled_quantity <= 0:
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        ForensicStatus.NO_QUOTE,
                        requested_quantity=requested,
                        note="trim-side quote unavailable; no synthetic fill used",
                    )
                )
                continue
            filled_state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                -fill.filled_quantity,
                fill.price,
            )
            ledger.gross_proceeds += (
                fill.price * fill.filled_quantity * _OPTION_MULTIPLIER
            )
            ledger.trim_count += 1
            ledger.depth_evidence_complete = (
                ledger.depth_evidence_complete and fill.depth_known and fill.complete
            )
            state = filled_state
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    _fill_status(fill),
                    quantity=fill.filled_quantity,
                    requested_quantity=requested,
                    fill_price=fill.price,
                    quote=fill.quote,
                    depth_known=fill.depth_known,
                    note=_depth_note(fill, "trim-to-one; no synthetic runner exit"),
                )
            )
            continue

        if effect.kind is EffectKind.PROPOSE_CLOSE:
            requested = position.quantity
            if requested <= 0:
                state = proposed_state
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        ForensicStatus.NO_POSITION_QUANTITY,
                    )
                )
                continue
            fill = quote_tape.bounded_sell_fill(
                key,
                target_ts,
                quantity=requested,
                max_lag=profile.quote_max_lag,
            )
            if fill is None or fill.filled_quantity <= 0:
                legs.append(
                    _record_leg(
                        event,
                        eligibility.bucket,
                        ForensicStatus.NO_QUOTE,
                        requested_quantity=requested,
                        note="exit-side quote unavailable; no synthetic fill used",
                    )
                )
                continue
            complete_close = fill.complete
            filled_state = apply_fill(
                proposed_state,
                key,
                effect.generation,
                -fill.filled_quantity,
                fill.price,
                final=complete_close,
            )
            proceeds = fill.price * fill.filled_quantity * _OPTION_MULTIPLIER
            ledger.gross_proceeds += proceeds
            ledger.depth_evidence_complete = (
                ledger.depth_evidence_complete and fill.depth_known and complete_close
            )
            state = filled_state
            if complete_close:
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
                        depth_evidence_complete=ledger.depth_evidence_complete,
                    )
                )
                del ledgers[key]
            legs.append(
                _record_leg(
                    event,
                    eligibility.bucket,
                    _fill_status(fill),
                    quantity=fill.filled_quantity,
                    requested_quantity=requested,
                    fill_price=fill.price,
                    quote=fill.quote,
                    depth_known=fill.depth_known,
                    note=_depth_note(
                        fill,
                        "full close" if complete_close else "close incomplete on displayed depth",
                    ),
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
    runtime_policy: RuntimePolicyBundle | None = None,
    include_research_only: bool = False,
) -> ForensicsReport:
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
    legs, completed = run_execution_forensics(
        tuple(messages),
        quote_tape,
        allowed_author_ids=allowed_author_ids,
        profile=profile,
        runtime_policy=policy_bundle,
        include_research_only=include_research_only,
    )
    return ForensicsReport(
        messages_seen=len(messages),
        completeness=tuple(completeness),
        legs=legs,
        completed_trades=completed,
        policy_fingerprint=policy_bundle.fingerprint,
    )
