from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from .quote_tape import HistoricalQuote, HistoricalQuoteTape

_NS_PER_SECOND = 1_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class MarketEventKind(StrEnum):
    QUOTE = "QUOTE"
    TRADE = "TRADE"


class AggressorSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    UNKNOWN = "UNKNOWN"


class FillCertainty(StrEnum):
    AGGRESSIVE_DISPLAYED_COMPLETE = "AGGRESSIVE_DISPLAYED_COMPLETE"
    AGGRESSIVE_DISPLAYED_PARTIAL = "AGGRESSIVE_DISPLAYED_PARTIAL"
    AGGRESSIVE_DEPTH_UNKNOWN = "AGGRESSIVE_DEPTH_UNKNOWN"
    RESTING_QUEUE_UNKNOWN = "RESTING_QUEUE_UNKNOWN"
    NO_FILL_EVIDENCE = "NO_FILL_EVIDENCE"
    NO_MARKET_STATE = "NO_MARKET_STATE"


@dataclass(frozen=True, slots=True)
class OptionMarketEvent:
    contract_key: str
    kind: MarketEventKind
    ts_event_ns: int
    ts_recv_ns: int
    publisher_id: int
    sequence: int
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    bid_publisher_id: int | None = None
    ask_publisher_id: int | None = None
    trade_price: Decimal | None = None
    trade_size: int | None = None
    aggressor_side: AggressorSide = AggressorSide.UNKNOWN
    source: str = "OPRA"

    def __post_init__(self) -> None:
        if not self.contract_key.strip():
            raise ValueError("contract_key is required")
        if self.ts_event_ns <= 0 or self.ts_recv_ns <= 0:
            raise ValueError("market timestamps must be positive nanoseconds")
        if self.publisher_id < 0 or self.sequence < 0:
            raise ValueError("publisher_id and sequence cannot be negative")
        if self.kind is MarketEventKind.QUOTE:
            if self.bid is None or self.ask is None:
                raise ValueError("quote event requires bid and ask")
            if self.bid <= 0 or self.ask <= 0:
                raise ValueError("quote prices must be positive")
            if self.bid_size is not None and self.bid_size <= 0:
                raise ValueError("bid_size must be positive when present")
            if self.ask_size is not None and self.ask_size <= 0:
                raise ValueError("ask_size must be positive when present")
        if self.kind is MarketEventKind.TRADE:
            if self.trade_price is None or self.trade_size is None:
                raise ValueError("trade event requires trade_price and trade_size")
            if self.trade_price <= 0 or self.trade_size <= 0:
                raise ValueError("trade price and size must be positive")

    @property
    def capture_latency_ns(self) -> int:
        return self.ts_recv_ns - self.ts_event_ns

    @property
    def event_ts_utc(self) -> datetime:
        return datetime_from_ns(self.ts_event_ns)

    @property
    def recv_ts_utc(self) -> datetime:
        return datetime_from_ns(self.ts_recv_ns)

    @property
    def stable_id(self) -> str:
        material = "|".join(
            (
                self.contract_key,
                self.kind.value,
                str(self.ts_event_ns),
                str(self.ts_recv_ns),
                str(self.publisher_id),
                str(self.sequence),
                str(self.bid or ""),
                str(self.ask or ""),
                str(self.trade_price or ""),
                str(self.trade_size or ""),
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MicroFillEnvelope:
    side: str
    requested_quantity: int
    lower_bound_quantity: int
    upper_bound_quantity: int
    modeled_price: Decimal | None
    certainty: FillCertainty
    order_arrival_ns: int
    evidence_event_ns: int | None
    evidence_publisher_id: int | None
    depth_known: bool
    reason: str

    @property
    def conservative_complete(self) -> bool:
        return self.lower_bound_quantity == self.requested_quantity

    @property
    def any_fill_possible(self) -> bool:
        return self.upper_bound_quantity > 0


@dataclass(frozen=True, slots=True)
class MicrostructureQualityReport:
    events: int
    quote_events: int
    trade_events: int
    contracts: int
    duplicate_events: int
    negative_capture_latency_events: int
    crossed_quote_events: int
    missing_quote_depth_events: int
    receive_time_regressions: int
    capture_latency_p50_us: float
    capture_latency_p95_us: float
    critical: bool


@dataclass(frozen=True, slots=True)
class DecisionQuote:
    quote: OptionMarketEvent
    decision_ts_ns: int
    available_ts_ns: int
    age_ns: int


def ns_from_datetime(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    normalized = value.astimezone(UTC)
    delta = normalized - _EPOCH
    return (
        delta.days * 86_400 * _NS_PER_SECOND
        + delta.seconds * _NS_PER_SECOND
        + delta.microseconds * 1_000
    )


def datetime_from_ns(value: int) -> datetime:
    if value < 0:
        raise ValueError("nanosecond timestamp cannot be negative")
    seconds, nanos = divmod(value, _NS_PER_SECOND)
    return _EPOCH + timedelta(seconds=seconds, microseconds=nanos // 1_000)


def timedelta_to_ns(value: timedelta) -> int:
    if value < timedelta(0):
        raise ValueError("timedelta cannot be negative")
    return (
        value.days * 86_400 * _NS_PER_SECOND
        + value.seconds * _NS_PER_SECOND
        + value.microseconds * 1_000
    )


def _percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return float(ordered[index])


class OptionMicrostructureTape:
    """Dual-clock, nanosecond-resolution U.S. options microstructure tape.

    ``ts_event_ns`` represents market/consolidator event time. ``ts_recv_ns`` represents the
    provider capture time and therefore the earliest point at which a historical strategy using
    that provider could have known the event. Decision pricing is indexed by receive time; fill
    reconstruction is indexed by event time. This prevents future market data from leaking into
    the decision while still reconstructing what the market did after order arrival.
    """

    def __init__(self, events: tuple[OptionMarketEvent, ...]) -> None:
        self._input_events = events
        grouped: dict[str, list[OptionMarketEvent]] = {}
        for event in events:
            grouped.setdefault(event.contract_key, []).append(event)
        self._events: dict[str, tuple[OptionMarketEvent, ...]] = {}
        self._event_ns: dict[str, tuple[int, ...]] = {}
        self._quotes_event: dict[str, tuple[OptionMarketEvent, ...]] = {}
        self._quote_event_ns: dict[str, tuple[int, ...]] = {}
        self._quotes_recv: dict[str, tuple[OptionMarketEvent, ...]] = {}
        self._quote_recv_ns: dict[str, tuple[int, ...]] = {}
        for contract_key, items in grouped.items():
            ordered = tuple(
                sorted(items, key=lambda item: (item.ts_event_ns, item.ts_recv_ns, item.sequence))
            )
            quotes_event = tuple(item for item in ordered if item.kind is MarketEventKind.QUOTE)
            quotes_recv = tuple(
                sorted(
                    quotes_event,
                    key=lambda item: (item.ts_recv_ns, item.ts_event_ns, item.sequence),
                )
            )
            self._events[contract_key] = ordered
            self._event_ns[contract_key] = tuple(item.ts_event_ns for item in ordered)
            self._quotes_event[contract_key] = quotes_event
            self._quote_event_ns[contract_key] = tuple(item.ts_event_ns for item in quotes_event)
            self._quotes_recv[contract_key] = quotes_recv
            self._quote_recv_ns[contract_key] = tuple(item.ts_recv_ns for item in quotes_recv)

    @classmethod
    def from_jsonl(cls, path: str | Path) -> OptionMicrostructureTape:
        events: list[OptionMarketEvent] = []
        for line_number, line in enumerate(Path(path).read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                events.append(
                    OptionMarketEvent(
                        contract_key=str(row["contract_key"]),
                        kind=MarketEventKind(str(row["kind"])),
                        ts_event_ns=int(row["ts_event_ns"]),
                        ts_recv_ns=int(row["ts_recv_ns"]),
                        publisher_id=int(row.get("publisher_id", 0)),
                        sequence=int(row.get("sequence", 0)),
                        bid=Decimal(str(row["bid"])) if row.get("bid") is not None else None,
                        ask=Decimal(str(row["ask"])) if row.get("ask") is not None else None,
                        bid_size=int(row["bid_size"]) if row.get("bid_size") is not None else None,
                        ask_size=int(row["ask_size"]) if row.get("ask_size") is not None else None,
                        bid_publisher_id=(
                            int(row["bid_publisher_id"])
                            if row.get("bid_publisher_id") is not None
                            else None
                        ),
                        ask_publisher_id=(
                            int(row["ask_publisher_id"])
                            if row.get("ask_publisher_id") is not None
                            else None
                        ),
                        trade_price=(
                            Decimal(str(row["trade_price"]))
                            if row.get("trade_price") is not None
                            else None
                        ),
                        trade_size=(
                            int(row["trade_size"])
                            if row.get("trade_size") is not None
                            else None
                        ),
                        aggressor_side=AggressorSide(str(row.get("aggressor_side", "UNKNOWN"))),
                        source=str(row.get("source", "OPRA")),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid microstructure JSONL at line {line_number}") from exc
        return cls(tuple(events))

    def quality_report(self) -> MicrostructureQualityReport:
        duplicate_count = len(self._input_events) - len(
            {event.stable_id for event in self._input_events}
        )
        negative_capture = sum(event.capture_latency_ns < 0 for event in self._input_events)
        crossed = sum(
            event.kind is MarketEventKind.QUOTE
            and event.bid is not None
            and event.ask is not None
            and event.ask < event.bid
            for event in self._input_events
        )
        missing_depth = sum(
            event.kind is MarketEventKind.QUOTE
            and (event.bid_size is None or event.ask_size is None)
            for event in self._input_events
        )
        regressions = 0
        last_recv: int | None = None
        for event in self._input_events:
            if last_recv is not None and event.ts_recv_ns < last_recv:
                regressions += 1
            last_recv = event.ts_recv_ns
        latencies = [
            event.capture_latency_ns
            for event in self._input_events
            if event.capture_latency_ns >= 0
        ]
        return MicrostructureQualityReport(
            events=len(self._input_events),
            quote_events=sum(event.kind is MarketEventKind.QUOTE for event in self._input_events),
            trade_events=sum(event.kind is MarketEventKind.TRADE for event in self._input_events),
            contracts=len(self._events),
            duplicate_events=duplicate_count,
            negative_capture_latency_events=negative_capture,
            crossed_quote_events=crossed,
            missing_quote_depth_events=missing_depth,
            receive_time_regressions=regressions,
            capture_latency_p50_us=_percentile(latencies, 0.50) / 1_000.0,
            capture_latency_p95_us=_percentile(latencies, 0.95) / 1_000.0,
            critical=negative_capture > 0 or crossed > 0,
        )

    def decision_quote(
        self,
        contract_key: str,
        decision_ts_utc: datetime,
        *,
        feed_transport_latency: timedelta = timedelta(0),
        max_age: timedelta = timedelta(seconds=1),
    ) -> DecisionQuote | None:
        decision_ns = ns_from_datetime(decision_ts_utc)
        transport_ns = timedelta_to_ns(feed_transport_latency)
        max_age_ns = timedelta_to_ns(max_age)
        cutoff_recv_ns = decision_ns - transport_ns
        quotes = self._quotes_recv.get(contract_key)
        recv_values = self._quote_recv_ns.get(contract_key)
        if quotes is None or not recv_values:
            return None
        index = bisect.bisect_right(recv_values, cutoff_recv_ns) - 1
        if index < 0:
            return None
        quote = quotes[index]
        available_ns = quote.ts_recv_ns + transport_ns
        age_ns = decision_ns - available_ns
        if age_ns < 0 or age_ns > max_age_ns:
            return None
        return DecisionQuote(quote, decision_ns, available_ns, age_ns)

    def market_quote_at(
        self,
        contract_key: str,
        market_ts_ns: int,
        *,
        max_age: timedelta = timedelta(seconds=1),
    ) -> OptionMarketEvent | None:
        quotes = self._quotes_event.get(contract_key)
        event_values = self._quote_event_ns.get(contract_key)
        if quotes is None or not event_values:
            return None
        index = bisect.bisect_right(event_values, market_ts_ns) - 1
        if index < 0:
            return None
        quote = quotes[index]
        if market_ts_ns - quote.ts_event_ns > timedelta_to_ns(max_age):
            return None
        return quote

    def events_between(
        self,
        contract_key: str,
        start_ns: int,
        end_ns: int,
    ) -> tuple[OptionMarketEvent, ...]:
        if end_ns < start_ns:
            raise ValueError("end_ns cannot precede start_ns")
        events = self._events.get(contract_key)
        values = self._event_ns.get(contract_key)
        if events is None or not values:
            return ()
        left = bisect.bisect_left(values, start_ns)
        right = bisect.bisect_right(values, end_ns)
        return events[left:right]

    def simulate_limit_buy(
        self,
        contract_key: str,
        *,
        order_arrival_ts_utc: datetime,
        quantity: int,
        limit_price: Decimal,
        max_wait: timedelta = timedelta(seconds=5),
        market_quote_max_age: timedelta = timedelta(seconds=1),
    ) -> MicroFillEnvelope:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if limit_price <= 0:
            raise ValueError("limit_price must be positive")
        arrival_ns = ns_from_datetime(order_arrival_ts_utc)
        market = self.market_quote_at(
            contract_key,
            arrival_ns,
            max_age=market_quote_max_age,
        )
        if market is None or market.ask is None:
            return MicroFillEnvelope(
                "BUY", quantity, 0, 0, None, FillCertainty.NO_MARKET_STATE,
                arrival_ns, None, None, False, "no causal market quote at order arrival",
            )
        if market.ask <= limit_price:
            if market.ask_size is None:
                return MicroFillEnvelope(
                    "BUY", quantity, 0, quantity, market.ask,
                    FillCertainty.AGGRESSIVE_DEPTH_UNKNOWN, arrival_ns,
                    market.ts_event_ns, market.ask_publisher_id or market.publisher_id,
                    False, "marketable at arrival but displayed ask size is unavailable",
                )
            supported = min(quantity, market.ask_size)
            certainty = (
                FillCertainty.AGGRESSIVE_DISPLAYED_COMPLETE
                if supported == quantity
                else FillCertainty.AGGRESSIVE_DISPLAYED_PARTIAL
            )
            return MicroFillEnvelope(
                "BUY", quantity, supported, supported, market.ask, certainty,
                arrival_ns, market.ts_event_ns,
                market.ask_publisher_id or market.publisher_id, True,
                "marketable limit supported by displayed ask depth at order arrival",
            )

        deadline_ns = arrival_ns + timedelta_to_ns(max_wait)
        upper = 0
        evidence_ns: int | None = None
        publisher: int | None = None
        modeled_price: Decimal | None = None
        depth_known = True
        for event in self.events_between(contract_key, arrival_ns, deadline_ns):
            if event.kind is MarketEventKind.QUOTE and event.ask is not None:
                if event.ask <= limit_price:
                    possible = event.ask_size if event.ask_size is not None else quantity
                    depth_known = depth_known and event.ask_size is not None
                    if min(quantity, possible) > upper:
                        upper = min(quantity, possible)
                        evidence_ns = event.ts_event_ns
                        publisher = event.ask_publisher_id or event.publisher_id
                        modeled_price = event.ask
            elif (
                event.kind is MarketEventKind.TRADE
                and event.trade_price is not None
                and event.trade_size is not None
                and event.trade_price <= limit_price
            ):
                upper = min(quantity, upper + event.trade_size)
                evidence_ns = event.ts_event_ns
                publisher = event.publisher_id
                modeled_price = event.trade_price
        if upper == 0:
            return MicroFillEnvelope(
                "BUY", quantity, 0, 0, None, FillCertainty.NO_FILL_EVIDENCE,
                arrival_ns, None, None, True,
                "resting limit never received supporting quote/trade evidence in window",
            )
        return MicroFillEnvelope(
            "BUY", quantity, 0, upper, modeled_price, FillCertainty.RESTING_QUEUE_UNKNOWN,
            arrival_ns, evidence_ns, publisher, depth_known,
            "price/trade evidence makes a fill possible but L1 cannot prove queue position",
        )

    def simulate_aggressive_sell(
        self,
        contract_key: str,
        *,
        order_arrival_ts_utc: datetime,
        quantity: int,
        market_quote_max_age: timedelta = timedelta(seconds=1),
    ) -> MicroFillEnvelope:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        arrival_ns = ns_from_datetime(order_arrival_ts_utc)
        market = self.market_quote_at(
            contract_key,
            arrival_ns,
            max_age=market_quote_max_age,
        )
        if market is None or market.bid is None:
            return MicroFillEnvelope(
                "SELL", quantity, 0, 0, None, FillCertainty.NO_MARKET_STATE,
                arrival_ns, None, None, False, "no causal market quote at order arrival",
            )
        if market.bid_size is None:
            return MicroFillEnvelope(
                "SELL", quantity, 0, quantity, market.bid,
                FillCertainty.AGGRESSIVE_DEPTH_UNKNOWN, arrival_ns,
                market.ts_event_ns, market.bid_publisher_id or market.publisher_id,
                False, "aggressive sell has bid price but displayed bid size is unavailable",
            )
        supported = min(quantity, market.bid_size)
        certainty = (
            FillCertainty.AGGRESSIVE_DISPLAYED_COMPLETE
            if supported == quantity
            else FillCertainty.AGGRESSIVE_DISPLAYED_PARTIAL
        )
        return MicroFillEnvelope(
            "SELL", quantity, supported, supported, market.bid, certainty,
            arrival_ns, market.ts_event_ns,
            market.bid_publisher_id or market.publisher_id, True,
            "aggressive sell supported by displayed bid depth at order arrival",
        )

    def as_quote_tape(self) -> HistoricalQuoteTape:
        quotes: list[HistoricalQuote] = []
        for contract_key in sorted(self._quotes_event):
            for event in self._quotes_event[contract_key]:
                if event.bid is None or event.ask is None or event.ask < event.bid:
                    continue
                quotes.append(
                    HistoricalQuote(
                        contract_key=contract_key,
                        ts_utc=event.event_ts_utc,
                        bid=event.bid,
                        ask=event.ask,
                        bid_size=event.bid_size,
                        ask_size=event.ask_size,
                        source=f"{event.source}:{event.publisher_id}",
                    )
                )
        return HistoricalQuoteTape(tuple(quotes))
