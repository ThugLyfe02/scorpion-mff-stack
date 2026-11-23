"""Conservative, quote-driven options simulation. No external order transport.

A successful fill is a model, not a venue acknowledgement. We deliberately model only
immediately marketable, full-size orders; resting orders and partial liquidity are not
silently promoted to fills. The legacy PaperBroker remains a deterministic test double.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from .broker import Ack, BrokerError, OrderLookup
from .calendar import CalendarError, et_date, session_of
from .instruments import InstrumentError, OptionContract
from .types import OrderIntent, Quote


@dataclass(frozen=True, slots=True)
class MarketQuote:
    osi: str
    event_ts: float
    received_ts: float
    bid: float
    ask: float
    bid_size: int
    ask_size: int


@dataclass(slots=True)
class Liquidity:
    quote: MarketQuote
    bid_remaining: int
    ask_remaining: int


@dataclass
class PaperQuoteBook:
    clock: Callable[[], float]
    max_age_s: float = 3.0
    quotes: dict[str, Liquidity] = field(default_factory=dict)
    watermarks: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not math.isfinite(self.max_age_s) or self.max_age_s <= 0:
            raise ValueError("invalid quote age budget")

    def admit(self, row: MarketQuote) -> str:
        """Return a stable refusal reason, or 'accepted'. Never renew old liquidity."""
        try:
            contract = OptionContract.parse_osi_symbol(row.osi)
        except InstrumentError:
            return "invalid_contract"
        if contract.osi_symbol != row.osi:
            return "noncanonical_contract"
        now = self.clock()
        if not all(math.isfinite(t) for t in (now, row.event_ts, row.received_ts)):
            return "invalid_clock"
        if not row.event_ts <= row.received_ts <= now:
            return "noncausal_quote"
        if now - row.event_ts > self.max_age_s:
            return "stale_quote"
        if row.event_ts <= self.watermarks.get(row.osi, -math.inf):
            return "duplicate_or_reordered_quote"
        self.watermarks[row.osi] = row.event_ts
        # A newer withdrawal or malformed market update invalidates the old price.
        if (
            not math.isfinite(row.bid) or not math.isfinite(row.ask)
            or row.bid <= 0 or row.ask < row.bid
            or type(row.bid_size) is not int or type(row.ask_size) is not int
            or row.bid_size < 0 or row.ask_size < 0
        ):
            self.quotes.pop(row.osi, None)
            return "invalid_or_withdrawn_market"
        self.quotes[row.osi] = Liquidity(row, row.bid_size, row.ask_size)
        return "accepted"

    def gap(self) -> None:
        self.quotes.clear()
        # A reconnect cannot replenish displayed size with the same event.

    def resolve(self, key: str) -> tuple[str, Liquidity]:
        now = self.clock()
        try:
            moment = datetime.fromtimestamp(now, UTC)
            session = session_of(moment)
            contract = OptionContract.from_contract_key(key, et_date(moment))
        except (CalendarError, InstrumentError, OverflowError, OSError, ValueError) as exc:
            raise BrokerError("paper_contract_or_calendar_unknown") from exc
        if session is None or not session.open_utc <= moment < session.close_utc:
            raise BrokerError("paper_outside_regular_session")
        if contract.expiry < session.day:
            raise BrokerError("paper_expired_contract")
        state = self.quotes.get(contract.osi_symbol)
        if state is None:
            raise BrokerError("paper_no_market")
        q = state.quote
        # A pre-open or previous-session observation cannot price a regular-session order.
        if q.event_ts < session.open_utc.timestamp():
            raise BrokerError("paper_quote_outside_session")
        if not q.event_ts <= q.received_ts <= now or now - q.event_ts > self.max_age_s:
            raise BrokerError("paper_stale_market")
        return contract.osi_symbol, state

    def quote(self, contract_key: str) -> Quote | None:
        try:
            _, state = self.resolve(contract_key)
        except BrokerError:
            return None
        q = state.quote
        return Quote(bid=q.bid, ask=q.ask, ts=q.event_ts)


@dataclass
class QuoteDrivenPaperBroker:
    book: PaperQuoteBook
    inventory: dict[str, int] = field(default_factory=dict)
    accepted: dict[str, tuple[OrderIntent, Ack]] = field(default_factory=dict)

    def lookup(self, idempotency_key: str) -> OrderLookup:
        return OrderLookup.SEEN if idempotency_key in self.accepted else OrderLookup.NOT_SEEN

    def place(self, intent: OrderIntent) -> Ack:
        key = intent.idempotency_key
        if not key:
            raise BrokerError("paper_missing_idempotency")
        prior = self.accepted.get(key)
        if prior is not None:
            if prior[0] != intent:
                raise BrokerError("paper_conflicting_idempotency")
            return prior[1]
        if type(intent.qty) is not int or intent.qty <= 0:
            raise BrokerError("paper_invalid_quantity")
        if not math.isfinite(intent.limit) or intent.limit <= 0:
            raise BrokerError("paper_invalid_limit")
        osi, state = self.book.resolve(intent.contract_key)
        held = self.inventory.get(osi, 0)
        limit = Decimal(str(intent.limit))
        if intent.action == "buy":
            if limit < Decimal(str(state.quote.ask)):
                raise BrokerError("paper_nonmarketable_buy")
            if intent.qty > state.ask_remaining:
                raise BrokerError("paper_insufficient_ask_size")
            state.ask_remaining -= intent.qty
            self.inventory[osi] = held + intent.qty
        elif intent.action == "sell":
            if intent.qty > held:
                raise BrokerError("paper_no_confirmed_inventory")
            if limit > Decimal(str(state.quote.bid)):
                raise BrokerError("paper_nonmarketable_sell")
            if intent.qty > state.bid_remaining:
                raise BrokerError("paper_insufficient_bid_size")
            state.bid_remaining -= intent.qty
            self.inventory[osi] = held - intent.qty
        else:
            raise BrokerError("paper_invalid_side")
        # Pessimistic price: the limit, never a better price merely because it was quoted.
        ack = Ack(
            ok=True, contract_key=intent.contract_key, qty=intent.qty, price=intent.limit,
            broker_order_id=f"modeled-{len(self.accepted) + 1}",
        )
        self.accepted[key] = (intent, ack)
        return ack
