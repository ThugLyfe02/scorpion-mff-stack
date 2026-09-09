from __future__ import annotations

import argparse
import csv
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .microstructure import AggressorSide, MarketEventKind, OptionMarketEvent, ns_from_datetime

_OCC_RE = re.compile(
    r"^\s*(?P<root>[A-Z0-9]{1,6})\s*(?P<date>\d{6})(?P<cp>[CP])(?P<strike>\d{8})\s*$",
    re.IGNORECASE,
)
_FIXED_9 = Decimal("0.000000001")


def _normalized_decimal(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def contract_key_from_occ_symbol(symbol: str) -> str:
    match = _OCC_RE.match(symbol)
    if match is None:
        raise ValueError(f"unsupported OCC option symbol: {symbol!r}")
    date_raw = match["date"]
    expiry = datetime.strptime(date_raw, "%y%m%d").date().isoformat()
    side = "CALL" if match["cp"].upper() == "C" else "PUT"
    strike = Decimal(int(match["strike"])) / Decimal("1000")
    return f"{match['root'].upper()}|{side}|{_normalized_decimal(strike)}|{expiry}"


def _timestamp_ns(value: str) -> int:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("timestamp is required")
    if cleaned.isdigit():
        return int(cleaned)
    parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return ns_from_datetime(parsed)


def _price(value: str | None, *, encoding: str) -> Decimal | None:
    if value is None or not value.strip():
        return None
    parsed = Decimal(value.strip())
    if encoding == "fixed9":
        return parsed * _FIXED_9
    if encoding == "decimal":
        return parsed
    raise ValueError("price encoding must be fixed9 or decimal")


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _aggressor(value: str | None) -> AggressorSide:
    normalized = (value or "").strip().upper()
    if normalized in {"B", "BID", "BUY"}:
        return AggressorSide.BUY
    if normalized in {"A", "ASK", "SELL"}:
        return AggressorSide.SELL
    return AggressorSide.UNKNOWN


def normalize_databento_opra_row(
    row: Mapping[str, str],
    *,
    price_encoding: str = "fixed9",
    consolidated_publisher_id: int = 30,
    include_regional_quotes: bool = False,
) -> tuple[OptionMarketEvent, ...]:
    symbol = row.get("symbol") or row.get("raw_symbol") or ""
    contract_key = contract_key_from_occ_symbol(symbol)
    ts_recv_ns = _timestamp_ns(row.get("ts_recv", ""))
    ts_event_ns = _timestamp_ns(row.get("ts_event", ""))
    publisher_id = int(row.get("publisher_id") or 0)
    sequence = int(row.get("sequence") or 0)
    action = (row.get("action") or "").strip().upper()

    bid = _price(row.get("bid_px_00"), encoding=price_encoding)
    ask = _price(row.get("ask_px_00"), encoding=price_encoding)
    bid_size = _optional_int(row.get("bid_sz_00"))
    ask_size = _optional_int(row.get("ask_sz_00"))
    bid_publisher_id = _optional_int(row.get("bid_pb_00"))
    ask_publisher_id = _optional_int(row.get("ask_pb_00"))

    events: list[OptionMarketEvent] = []
    quote_allowed = include_regional_quotes or publisher_id == consolidated_publisher_id
    if quote_allowed and bid is not None and ask is not None and bid > 0 and ask > 0:
        events.append(
            OptionMarketEvent(
                contract_key=contract_key,
                kind=MarketEventKind.QUOTE,
                ts_event_ns=ts_event_ns,
                ts_recv_ns=ts_recv_ns,
                publisher_id=publisher_id,
                sequence=sequence,
                bid=bid,
                ask=ask,
                bid_size=bid_size,
                ask_size=ask_size,
                bid_publisher_id=bid_publisher_id,
                ask_publisher_id=ask_publisher_id,
                source="DATABENTO_OPRA",
            )
        )

    if action in {"T", "TRADE"}:
        trade_price = _price(row.get("price"), encoding=price_encoding)
        trade_size = _optional_int(row.get("size"))
        if trade_price is not None and trade_price > 0 and trade_size is not None:
            events.append(
                OptionMarketEvent(
                    contract_key=contract_key,
                    kind=MarketEventKind.TRADE,
                    ts_event_ns=ts_event_ns,
                    ts_recv_ns=ts_recv_ns,
                    publisher_id=publisher_id,
                    sequence=sequence,
                    trade_price=trade_price,
                    trade_size=trade_size,
                    aggressor_side=_aggressor(row.get("side")),
                    source="DATABENTO_OPRA",
                )
            )
    return tuple(events)


def _event_payload(event: OptionMarketEvent) -> dict[str, object]:
    return {
        "contract_key": event.contract_key,
        "kind": event.kind.value,
        "ts_event_ns": event.ts_event_ns,
        "ts_recv_ns": event.ts_recv_ns,
        "publisher_id": event.publisher_id,
        "sequence": event.sequence,
        "bid": str(event.bid) if event.bid is not None else None,
        "ask": str(event.ask) if event.ask is not None else None,
        "bid_size": event.bid_size,
        "ask_size": event.ask_size,
        "bid_publisher_id": event.bid_publisher_id,
        "ask_publisher_id": event.ask_publisher_id,
        "trade_price": str(event.trade_price) if event.trade_price is not None else None,
        "trade_size": event.trade_size,
        "aggressor_side": event.aggressor_side.value,
        "source": event.source,
    }


def normalize_opra_csv(
    input_path: str | Path,
    output_path: str | Path,
    *,
    price_encoding: str = "fixed9",
    consolidated_publisher_id: int = 30,
    include_regional_quotes: bool = False,
) -> tuple[int, int]:
    rows = 0
    events_written = 0
    output = Path(output_path)
    with Path(input_path).open(newline="") as source, output.open("w") as destination:
        reader = csv.DictReader(source)
        for row_number, row in enumerate(reader, start=2):
            rows += 1
            try:
                events = normalize_databento_opra_row(
                    row,
                    price_encoding=price_encoding,
                    consolidated_publisher_id=consolidated_publisher_id,
                    include_regional_quotes=include_regional_quotes,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid OPRA CSV row {row_number}") from exc
            for event in events:
                destination.write(json.dumps(_event_payload(event), sort_keys=True) + "\n")
                events_written += 1
    return rows, events_written


def opra_normalize_main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize Databento OPRA MBP-1/CMBP-1 CSV into Scorpion microstructure JSONL."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--price-encoding", choices=("fixed9", "decimal"), default="fixed9")
    parser.add_argument("--consolidated-publisher-id", type=int, default=30)
    parser.add_argument("--include-regional-quotes", action="store_true")
    args = parser.parse_args()
    rows, events = normalize_opra_csv(
        args.input,
        args.output,
        price_encoding=args.price_encoding,
        consolidated_publisher_id=args.consolidated_publisher_id,
        include_regional_quotes=args.include_regional_quotes,
    )
    print(json.dumps({"rows_read": rows, "events_written": events}, indent=2, sort_keys=True))
