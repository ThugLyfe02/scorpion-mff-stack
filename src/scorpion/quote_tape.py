from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HistoricalQuote:
    contract_key: str
    ts_utc: datetime
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        if self.ts_utc.tzinfo is None or self.ts_utc.utcoffset() is None:
            raise ValueError("quote timestamp must be timezone-aware")
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("invalid historical quote")
        object.__setattr__(self, "ts_utc", self.ts_utc.astimezone(UTC))


class HistoricalQuoteTape:
    """In-memory, contract-indexed NBBO-like quote tape.

    JSONL is intentionally provider-neutral. Each record needs contract_key, ts_utc, bid, ask.
    The execution-forensics engine only calls quotes at or after the simulated decision time, so
    it cannot accidentally use a pre-alert future-insensitive midpoint.
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
                    )
                )
            except (KeyError, ValueError) as exc:
                raise ValueError(f"invalid quote JSONL at line {line_number}") from exc
        return cls(tuple(quotes))

    def at_or_after(
        self,
        contract_key: str,
        target_ts_utc: datetime,
        *,
        max_lag: timedelta = timedelta(seconds=3),
    ) -> HistoricalQuote | None:
        target = target_ts_utc.astimezone(UTC)
        timestamps = self._timestamps.get(contract_key)
        quotes = self._quotes.get(contract_key)
        if not timestamps or not quotes:
            return None
        index = bisect.bisect_left(timestamps, target.timestamp())
        if index >= len(quotes):
            return None
        quote = quotes[index]
        lag = quote.ts_utc - target
        if lag < timedelta(0) or lag > max_lag:
            return None
        return quote
