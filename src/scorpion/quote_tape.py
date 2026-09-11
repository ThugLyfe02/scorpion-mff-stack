from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class HistoricalQuote:
    contract_key: str
    ts_utc: datetime
    bid: Decimal
    ask: Decimal
    bid_size: int | None = None
    ask_size: int | None = None
    source: str = ""

    def __post_init__(self) -> None:
        if self.ts_utc.tzinfo is None or self.ts_utc.utcoffset() is None:
            raise ValueError("quote timestamp must be timezone-aware")
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("invalid historical quote")
        if self.bid_size is not None and self.bid_size <= 0:
            raise ValueError("bid_size must be positive when present")
        if self.ask_size is not None and self.ask_size <= 0:
            raise ValueError("ask_size must be positive when present")
        object.__setattr__(self, "ts_utc", self.ts_utc.astimezone(UTC))

    @property
    def spread_fraction(self) -> Decimal:
        midpoint = (self.bid + self.ask) / Decimal("2")
        return (self.ask - self.bid) / midpoint if midpoint > 0 else Decimal("0")


@dataclass(frozen=True, slots=True)
class HistoricalFillEvidence:
    side: Literal["BUY", "SELL"]
    requested_quantity: int
    filled_quantity: int
    price: Decimal
    quote: HistoricalQuote
    depth_known: bool
    upper_bound_quantity: int | None = None
    certainty: str = "TOP_OF_BOOK"

    @property
    def complete(self) -> bool:
        return self.filled_quantity == self.requested_quantity

    @property
    def maximum_supported_quantity(self) -> int:
        return (
            self.upper_bound_quantity
            if self.upper_bound_quantity is not None
            else self.filled_quantity
        )


class HistoricalQuoteTape:
    """In-memory, contract-indexed quote tape with explicit causal lookup semantics.

    JSONL is provider-neutral. Required fields are contract_key, ts_utc, bid and ask; optional
    bid_size/ask_size/source improve execution-evidence quality.

    Decision-time pricing must use :meth:`at_or_before`, because a strategy cannot compute a
    limit from a quote that had not yet existed. Methods that search at/after a timestamp are
    reserved for post-order fill evidence.
    """

    def __init__(self, quotes: tuple[HistoricalQuote, ...]) -> None:
        grouped: dict[str, list[HistoricalQuote]] = {}
        for quote in quotes:
            grouped.setdefault(quote.contract_key, []).append(quote)
        self._quotes: dict[str, tuple[HistoricalQuote, ...]] = {}
        self._timestamps: dict[str, tuple[float, ...]] = {}
        for contract_key, items in grouped.items():
            ordered = tuple(sorted(items, key=lambda quote: quote.ts_utc))
            self._quotes[contract_key] = ordered
            self._timestamps[contract_key] = tuple(
                quote.ts_utc.timestamp() for quote in ordered
            )

    @classmethod
    def from_jsonl(cls, path: str | Path) -> HistoricalQuoteTape:
        quotes: list[HistoricalQuote] = []
        for line_number, line in enumerate(Path(path).read_text().splitlines(), start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            try:
                quotes.append(
                    HistoricalQuote(
                        contract_key=str(row["contract_key"]),
                        ts_utc=datetime.fromisoformat(str(row["ts_utc"])),
                        bid=Decimal(str(row["bid"])),
                        ask=Decimal(str(row["ask"])),
                        bid_size=(
                            int(row["bid_size"]) if row.get("bid_size") is not None else None
                        ),
                        ask_size=(
                            int(row["ask_size"]) if row.get("ask_size") is not None else None
                        ),
                        source=str(row.get("source", "")),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid quote JSONL at line {line_number}") from exc
        return cls(tuple(quotes))

    def _start_index(self, contract_key: str, target_ts_utc: datetime) -> int | None:
        timestamps = self._timestamps.get(contract_key)
        if not timestamps:
            return None
        return bisect.bisect_left(timestamps, target_ts_utc.astimezone(UTC).timestamp())

    def at_or_before(
        self,
        contract_key: str,
        target_ts_utc: datetime,
        *,
        max_age: timedelta = timedelta(seconds=3),
    ) -> HistoricalQuote | None:
        """Return the newest quote that was already knowable at ``target_ts_utc``.

        This is the correct lookup for decision-time pricing. ``max_age`` prevents stale quotes
        from being treated as current simply because they are the last recorded observation.
        """
        if max_age < timedelta(0):
            raise ValueError("max_age cannot be negative")
        target = target_ts_utc.astimezone(UTC)
        quotes = self._quotes.get(contract_key)
        timestamps = self._timestamps.get(contract_key)
        if quotes is None or not timestamps:
            return None
        index = bisect.bisect_right(timestamps, target.timestamp()) - 1
        if index < 0:
            return None
        quote = quotes[index]
        age = target - quote.ts_utc
        if age < timedelta(0) or age > max_age:
            return None
        return quote

    def at_or_after(
        self,
        contract_key: str,
        target_ts_utc: datetime,
        *,
        max_lag: timedelta = timedelta(seconds=3),
    ) -> HistoricalQuote | None:
        if max_lag < timedelta(0):
            raise ValueError("max_lag cannot be negative")
        target = target_ts_utc.astimezone(UTC)
        quotes = self._quotes.get(contract_key)
        index = self._start_index(contract_key, target)
        if quotes is None or index is None or index >= len(quotes):
            return None
        quote = quotes[index]
        lag = quote.ts_utc - target
        if lag < timedelta(0) or lag > max_lag:
            return None
        return quote

    def quotes_between(
        self,
        contract_key: str,
        start_ts_utc: datetime,
        end_ts_utc: datetime,
    ) -> tuple[HistoricalQuote, ...]:
        start = start_ts_utc.astimezone(UTC)
        end = end_ts_utc.astimezone(UTC)
        if end < start:
            raise ValueError("end timestamp cannot precede start timestamp")
        quotes = self._quotes.get(contract_key)
        timestamps = self._timestamps.get(contract_key)
        if quotes is None or not timestamps:
            return ()
        left = bisect.bisect_left(timestamps, start.timestamp())
        right = bisect.bisect_right(timestamps, end.timestamp())
        return quotes[left:right]

    def first_ask_at_or_below(
        self,
        contract_key: str,
        target_ts_utc: datetime,
        limit_price: Decimal,
        *,
        max_wait: timedelta = timedelta(seconds=5),
    ) -> HistoricalQuote | None:
        if limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if max_wait < timedelta(0):
            raise ValueError("max_wait cannot be negative")
        target = target_ts_utc.astimezone(UTC)
        quotes = self._quotes.get(contract_key)
        index = self._start_index(contract_key, target)
        if quotes is None or index is None:
            return None
        for quote in quotes[index:]:
            elapsed = quote.ts_utc - target
            if elapsed < timedelta(0):
                continue
            if elapsed > max_wait:
                break
            if quote.ask <= limit_price:
                return quote
        return None

    def bounded_buy_fill(
        self,
        contract_key: str,
        target_ts_utc: datetime,
        *,
        quantity: int,
        limit_price: Decimal,
        max_wait: timedelta = timedelta(seconds=5),
    ) -> HistoricalFillEvidence | None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        quote = self.first_ask_at_or_below(
            contract_key,
            target_ts_utc,
            limit_price,
            max_wait=max_wait,
        )
        if quote is None:
            return None
        depth_known = quote.ask_size is not None
        available = quote.ask_size if quote.ask_size is not None else quantity
        filled = min(quantity, available)
        return HistoricalFillEvidence(
            side="BUY",
            requested_quantity=quantity,
            filled_quantity=filled,
            price=quote.ask,
            quote=quote,
            depth_known=depth_known,
            upper_bound_quantity=filled if depth_known else quantity,
            certainty="QUOTE_TOUCH",
        )

    def bounded_sell_fill(
        self,
        contract_key: str,
        target_ts_utc: datetime,
        *,
        quantity: int,
        max_lag: timedelta = timedelta(seconds=3),
    ) -> HistoricalFillEvidence | None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        quote = self.at_or_after(contract_key, target_ts_utc, max_lag=max_lag)
        if quote is None:
            return None
        depth_known = quote.bid_size is not None
        available = quote.bid_size if quote.bid_size is not None else quantity
        filled = min(quantity, available)
        return HistoricalFillEvidence(
            side="SELL",
            requested_quantity=quantity,
            filled_quantity=filled,
            price=quote.bid,
            quote=quote,
            depth_known=depth_known,
            upper_bound_quantity=filled if depth_known else quantity,
            certainty="AGGRESSIVE_BID",
        )
