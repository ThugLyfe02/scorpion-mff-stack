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

    @property
    def complete(self) -> bool:
        return self.filled_quantity == self.requested_quantity


class HistoricalQuoteTape:
    """In-memory, contract-indexed NBBO-like quote tape.

    JSONL is provider-neutral. Required fields are contract_key, ts_utc, bid and ask; optional
    bid_size/ask_size/source improve execution-evidence quality. Lookups are causal: no quote
    before the simulated decision timestamp can be used for a fill.
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

    def at_or_after(
        self,
        contract_key: str,
        target_ts_utc: datetime,
        *,
        max_lag: timedelta = timedelta(seconds=3),
    ) -> HistoricalQuote | None:
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
        return HistoricalFillEvidence(
            side="BUY",
            requested_quantity=quantity,
            filled_quantity=min(quantity, available),
            price=quote.ask,
            quote=quote,
            depth_known=depth_known,
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
        return HistoricalFillEvidence(
            side="SELL",
            requested_quantity=quantity,
            filled_quantity=min(quantity, available),
            price=quote.bid,
            quote=quote,
            depth_known=depth_known,
        )
