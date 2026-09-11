from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal

from .microstructure import (
    MarketEventKind,
    OptionMarketEvent,
    OptionMicrostructureTape,
    datetime_from_ns,
)

_NS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class DiagnosticBar:
    contract_key: str
    starts_ns: int
    ends_ns: int
    event_count: int
    quote_count: int
    trade_count: int
    first_bid: Decimal | None
    first_ask: Decimal | None
    last_bid: Decimal | None
    last_ask: Decimal | None
    trade_open: Decimal | None
    trade_high: Decimal | None
    trade_low: Decimal | None
    trade_close: Decimal | None
    trade_volume: int

    @property
    def starts_ts_utc(self) -> str:
        return datetime_from_ns(self.starts_ns).isoformat()

    @property
    def ends_ts_utc(self) -> str:
        return datetime_from_ns(self.ends_ns).isoformat()


def aggregate_diagnostic_bars(
    events: tuple[OptionMarketEvent, ...],
    *,
    interval: timedelta = timedelta(seconds=15),
) -> tuple[DiagnosticBar, ...]:
    """Aggregate tick events for human visualization only.

    These bars are deliberately isolated from every execution/fill API. The execution simulator
    consumes native event records; 1s/15s/etc. bars exist only so humans can inspect the same
    windows without losing the underlying evidence.
    """
    interval_ns = int(interval.total_seconds() * _NS_PER_SECOND)
    if interval_ns <= 0:
        raise ValueError("interval must be positive")
    grouped: dict[tuple[str, int], list[OptionMarketEvent]] = {}
    for event in events:
        bucket = (event.ts_event_ns // interval_ns) * interval_ns
        grouped.setdefault((event.contract_key, bucket), []).append(event)

    bars: list[DiagnosticBar] = []
    for (contract_key, starts_ns), items in sorted(grouped.items()):
        ordered = sorted(items, key=lambda item: (item.ts_event_ns, item.sequence))
        quotes = [item for item in ordered if item.kind is MarketEventKind.QUOTE]
        trades = [item for item in ordered if item.kind is MarketEventKind.TRADE]
        trade_prices = [item.trade_price for item in trades if item.trade_price is not None]
        bars.append(
            DiagnosticBar(
                contract_key=contract_key,
                starts_ns=starts_ns,
                ends_ns=starts_ns + interval_ns,
                event_count=len(ordered),
                quote_count=len(quotes),
                trade_count=len(trades),
                first_bid=quotes[0].bid if quotes else None,
                first_ask=quotes[0].ask if quotes else None,
                last_bid=quotes[-1].bid if quotes else None,
                last_ask=quotes[-1].ask if quotes else None,
                trade_open=trade_prices[0] if trade_prices else None,
                trade_high=max(trade_prices) if trade_prices else None,
                trade_low=min(trade_prices) if trade_prices else None,
                trade_close=trade_prices[-1] if trade_prices else None,
                trade_volume=sum(item.trade_size or 0 for item in trades),
            )
        )
    return tuple(bars)


def micro_bars_main() -> None:
    parser = argparse.ArgumentParser(
        description="Render human-only bars from native microstructure events; never used for fills."
    )
    parser.add_argument("events")
    parser.add_argument("--seconds", type=int, default=15)
    args = parser.parse_args()
    if args.seconds <= 0:
        raise SystemExit("--seconds must be positive")
    tape = OptionMicrostructureTape.from_jsonl(args.events)
    events = tuple(
        event
        for contract_events in tape._events.values()  # noqa: SLF001 - diagnostic export only
        for event in contract_events
    )
    bars = aggregate_diagnostic_bars(events, interval=timedelta(seconds=args.seconds))
    for bar in bars:
        payload = asdict(bar)
        payload["starts_ts_utc"] = bar.starts_ts_utc
        payload["ends_ts_utc"] = bar.ends_ts_utc
        print(json.dumps(payload, default=str, sort_keys=True))
