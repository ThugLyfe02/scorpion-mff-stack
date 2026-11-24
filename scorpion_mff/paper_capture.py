"""Consume an authorized, normalized options-event capture using the existing listener.

Offline/capture-driven only. No vendor connection, no live switch, no broker credentials.
Each invocation creates a new single-session evidence directory; no implicit restart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .account import FeeModel
from .calendar import et_date
from .engine import Engine, EngineConfig
from .execution_book import ExecutionBook
from .fills import FillLog
from .health import Heartbeat
from .instruments import OptionContract
from .killswitch import KillSwitch
from .listener import Listener, TapeTransport
from .paper_market import MarketQuote, PaperQuoteBook, QuoteDrivenPaperBroker
from .parser import CHANNEL_IDS
from .reconcile import (
    PaperOrderObservation,
    ReconcileError,
    VenuePosition,
    compare_paper_orders,
    paper_orders_from_payload,
    reconcile,
    snapshot_from_payload,
)
from .types import RawMessage

MODEL = "regular-session-crossing-full-size-pessimistic-limit-v1"


@dataclass
class ReplayClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


def run_capture(
    lines: Iterable[str], output: Path, *, source: str, allowed_authors: frozenset[str],
    max_events: int = 250_000,
    require_market_status: bool = False,
    fee_model: FeeModel | None = None,
    max_signal_age_s: float | None = None,
) -> dict[str, Any]:
    if not source.strip() or not allowed_authors or "" in allowed_authors or max_events <= 0:
        raise ValueError("declare capture source, authorized authors and positive event bound")
    if max_signal_age_s is not None and (
        not math.isfinite(max_signal_age_s) or max_signal_age_s <= 0
    ):
        raise ValueError("invalid signal age budget")
    # Exclusive directory creation prevents appending a new run to a previous book.
    output.mkdir(parents=True, exist_ok=False)
    clock = ReplayClock()
    quotes = PaperQuoteBook(clock, require_market_status=require_market_status)
    broker = QuoteDrivenPaperBroker(quotes)
    engine = Engine(
        config=EngineConfig(
            max_quote_age_s=3.0, enforce_expiry=True, max_spread_ratio=None,
        ),
        fills=FillLog(output / "decisions.jsonl", fsync=True), clock=clock,
    )
    book = ExecutionBook(path=output / "modeled_execution.jsonl", fsync=True,
                         day_of=engine.trading_day)
    listener = Listener(
        transport=TapeTransport([]), engine=engine, broker=broker, quotes=quotes,
        heartbeat=Heartbeat(path=output / "heartbeat.json", clock=clock),
        kill=KillSwitch(path=output / "NO_TRADE", clock=clock), clock=clock, execution=book,
    )
    count = 0
    day: date | None = None
    digest = hashlib.sha256()
    reasons: Counter[str] = Counter()
    seen: dict[str, str] = {}
    for line in lines:
        if len(line) > 65_536:
            raise ValueError("oversized capture record")
        if not line.strip():
            continue
        if count >= max_events:
            raise ValueError("capture event bound exceeded")
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("capture row must be an object")
        count += 1
        if type(row.get("seq")) is not int or row["seq"] != count:
            raise ValueError("capture sequence gap or duplicate")
        ts = row["received_ts"]
        if (
            type(ts) not in (int, float) or not math.isfinite(ts)
            or ts <= 0 or ts < clock.now
        ):
            raise ValueError("invalid or regressing capture clock")
        clock.now = float(ts)
        event_day = et_date(datetime.fromtimestamp(clock.now, UTC))
        if day is not None and event_day != day:
            raise ValueError("single-session capture required; no overnight aliasing")
        day = event_day
        digest.update(line.encode("utf-8"))
        kind = row["kind"]
        if kind == "quote":
            q = MarketQuote(
                osi=row["osi"], event_ts=row["event_ts"], received_ts=clock.now,
                bid=row["bid"], ask=row["ask"], bid_size=row["bid_size"], ask_size=row["ask_size"],
            )
            reasons["quote:" + quotes.admit(q)] += 1
        elif kind == "market_status":
            reasons["status:" + quotes.admit_status(
                row["osi"], row["event_ts"], row["status"],
            )] += 1
        elif kind == "gap":
            quotes.gap()
            reasons["feed_gap"] += 1
        elif kind == "tick":
            listener.tick(clock.now)
        elif kind == "signal":
            mid = row["message_id"]
            author = row["author_id"]
            channel = row["channel"]
            text = row["text"]
            published = row["source_ts"]
            if not all(isinstance(v, str) and v for v in (mid, author, channel, text)):
                raise ValueError("invalid signal identity")
            if (
                type(published) not in (int, float) or not math.isfinite(published)
                or not 0 < published <= clock.now
            ):
                raise ValueError("noncausal signal timestamp")
            if row.get("event_type", "MESSAGE_CREATE") != "MESSAGE_CREATE":
                raise ValueError("edits/deletions require explicit provenance reconciliation")
            identity = json.dumps([author, channel, text, published], separators=(",", ":"))
            if mid in seen:
                if seen[mid] != identity:
                    raise ValueError("conflicting redelivered signal")
                reasons["duplicate_signal"] += 1
                continue
            seen[mid] = identity
            if author not in allowed_authors or channel not in CHANNEL_IDS:
                reasons["unauthorized_signal"] += 1
                continue
            if max_signal_age_s is not None and clock.now - published > max_signal_age_s:
                reasons["stale_signal"] += 1
                continue
            listener.handle(RawMessage(
                channel=channel, channel_id=CHANNEL_IDS[channel], text=text,
                message_id=mid, ts=clock.now, author_id=author,
            ))
        else:
            raise ValueError("unknown capture event kind")

    if count == 0:
        raise ValueError("empty capture")
    restored = ExecutionBook.load(output / "modeled_execution.jsonl",
                                  day_of=engine.trading_day)
    position_match = {
        p.contract_key: p.contracts for p in engine.open_positions
    } == {p.contract_key: p.contracts for p in book.open_positions()}
    # Independently reconcile the simulator's own acknowledgements and inventory.
    # A ledger that agrees only with itself cannot prove that it captured every effect.
    assert day is not None
    simulator_fills = sorted(
        (ack.broker_order_id, intent.contract_key, intent.action, ack.qty, ack.price)
        for intent, ack in broker.accepted.values()
    )
    ledger_fills = sorted(
        (fill.broker_order_id, fill.contract_key, fill.action, fill.qty, fill.price)
        for fill in book.fills
    )
    ledger_inventory: Counter[str] = Counter()
    for position in book.open_positions():
        osi = OptionContract.from_contract_key(position.contract_key, day).osi_symbol
        ledger_inventory[osi] += position.contracts
    simulator_inventory = {key: qty for key, qty in broker.inventory.items() if qty}
    inventory_match = dict(ledger_inventory) == simulator_inventory
    fill_match = simulator_fills == ledger_fills
    integrity = (
        restored.digest() == book.digest() and not restored.torn_lines
        and not book.anomalies and not book.unresolved_orders() and position_match
        and inventory_match and fill_match
    )
    flat = not book.open_positions()
    cashflow = sum(
        (Decimal(str(ack.price)) * ack.qty * 100
         * (1 if intent.action == "sell" else -1)
         for intent, ack in broker.accepted.values()),
        Decimal(0),
    )
    cashflow_match = (
        not flat or abs(cashflow - Decimal(str(book.realized_total))) <= Decimal("0.01")
    )
    integrity = integrity and cashflow_match
    fees = (
        sum(fee_model.fees_for(ack.qty) for _, ack in broker.accepted.values())
        if fee_model is not None else None
    )
    blockers: list[str] = []
    if not require_market_status:
        blockers.append("market_status_not_required")
    if fee_model is None:
        blockers.append("fee_model_not_declared")
    if max_signal_age_s is None:
        blockers.append("signal_age_budget_not_declared")
    if not book.fills:
        blockers.append("no_modeled_fills")
    if reasons["feed_gap"]:
        blockers.append("capture_contains_feed_gap")
    if (
        reasons["quote:conflicting_quote_timestamp"]
        or reasons["status:conflicting_status_timestamp"]
    ):
        blockers.append("ambiguous_market_event")
    if not integrity:
        blockers.append("reconciliation_failed")
    if not flat:
        blockers.append("open_positions")
    summary: dict[str, Any] = {
        "authority": "PAPER_ONLY", "source_declared_not_authenticated": source,
        "model": MODEL, "session_date": str(day), "input_sha256": digest.hexdigest(),
        "events": count, "admission_counts": dict(sorted(reasons.items())),
        "modeled_fills": len(book.fills), "modeled_realized_usd": book.realized_total,
        "modeled_cashflow_usd": float(cashflow),
        "fee_model": fee_model.to_dict() if fee_model is not None else None,
        "modeled_fees_usd": fees / 100 if fees is not None else None,
        "modeled_net_realized_usd": (
            float(cashflow - Decimal(fees) / 100)
            if fees is not None and flat and integrity and book.fills else None
        ),
        "open_modeled_positions": book.snapshot()["positions"],
        "execution_digest": book.digest(), "ledger_integrity": integrity,
        "capture_end_ts": clock.now,
        "modeled_execution_sha256": hashlib.sha256(
            (output / "modeled_execution.jsonl").read_bytes()
            if (output / "modeled_execution.jsonl").exists() else b""
        ).hexdigest(),
        "simulator_fill_match": fill_match, "simulator_inventory_match": inventory_match,
        "flat_cashflow_match": cashflow_match,
        "session_flat": flat,
        "require_market_status": require_market_status,
        "max_signal_age_s": max_signal_age_s,
        "modeled_sample_complete": not blockers,
        "sample_blockers": blockers,
        "certifies_live_fills": False,
    }
    # A crash before this exclusive, fsync'd receipt leaves an incomplete run, not a pass.
    with (output / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return summary


def compare_capture_venue(run: Path, export_path: Path) -> dict[str, Any]:
    """Offline comparison of independent paper observations with a completed local capture.

    The export's provenance is declared, not authenticated. This cannot submit orders
    or establish that the venue's simulator represents real execution.
    """
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    ledger_path = run / "modeled_execution.jsonl"
    ledger_bytes = ledger_path.read_bytes()
    if (
        summary.get("authority") != "PAPER_ONLY"
        or summary.get("ledger_integrity") is not True
        or summary.get("modeled_execution_sha256") != hashlib.sha256(ledger_bytes).hexdigest()
    ):
        raise ReconcileError("unverified modeled capture")
    book = ExecutionBook.load(
        ledger_path, day_of=lambda ts: et_date(datetime.fromtimestamp(ts, UTC)).isoformat(),
    )
    if (
        book.digest() != summary.get("execution_digest") or book.torn_lines
        or book.anomalies or book.unresolved_orders()
    ):
        raise ReconcileError("modeled ledger does not reproduce")
    raw = export_path.read_bytes()
    export = json.loads(raw)
    if not isinstance(export, dict):
        raise ReconcileError("paper venue export must be an object")
    source = export.get("source")
    if (
        export.get("schema") != "scorpion.paper-venue-export.v1"
        or export.get("authority") != "PAPER_ONLY"
        or not isinstance(source, str) or not source.strip()
        or source == summary.get("source_declared_not_authenticated")
        or export.get("input_sha256") != summary.get("input_sha256")
        or export.get("session_date") != summary.get("session_date")
        or export.get("complete_order_export") is not True
    ):
        raise ReconcileError("paper venue scope or provenance mismatch")
    day = date.fromisoformat(summary["session_date"])
    cutoff = export.get("as_of_ts")
    if (
        type(cutoff) not in (int, float) or not math.isfinite(cutoff)
        or cutoff < summary["capture_end_ts"]
        or et_date(datetime.fromtimestamp(cutoff, UTC)) != day
    ):
        raise ReconcileError("paper venue snapshot outside capture session")
    modeled = [
        PaperOrderObservation(
            client_id=order.client_id,
            osi=OptionContract.from_contract_key(order.contract_key, day).osi_symbol,
            action=order.action, requested_qty=order.intent.qty,
            limit=Decimal(str(order.intent.limit)), filled_qty=order.filled_qty,
            average_price=Decimal(str(order.avg_price)) if order.filled_qty else None,
            state=order.state,
        )
        for order in book.orders.values()
    ]
    external = paper_orders_from_payload(export.get("orders"))
    # Normalize and validate OSI identities; do not join on ticker or guessed expiry.
    for order in external:
        if OptionContract.parse_osi_symbol(order.osi).osi_symbol != order.osi:
            raise ReconcileError("noncanonical venue contract")
    venue_positions = snapshot_from_payload(export.get("positions"))
    for position in venue_positions:
        canonical = OptionContract.parse_osi_symbol(position.contract_key).osi_symbol
        if canonical != position.contract_key:
            raise ReconcileError("noncanonical venue position")
    # Reuse the established book-drift comparator with canonical OSI identities.
    local_positions = [
        replace(position, contract_key=OptionContract.from_contract_key(
            position.contract_key, day,
        ).osi_symbol)
        for position in book.open_positions()
    ]
    positions = reconcile(local_positions, [
        VenuePosition(p.contract_key, p.contracts) for p in venue_positions
    ])
    comparison = compare_paper_orders(modeled, external)
    return {
        "authority": "RESEARCH_ONLY", "source_declared_not_authenticated": source,
        "input_sha256": summary["input_sha256"],
        "modeled_execution_sha256": summary["modeled_execution_sha256"],
        "venue_export_sha256": hashlib.sha256(raw).hexdigest(),
        "as_of_ts": cutoff, "orders": comparison, "positions": positions.to_dict(),
        "exact_parity": comparison["exact_parity"] and positions.clean,
        "certifies_live_fills": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "reconcile":
        compare = argparse.ArgumentParser(description="Compare a completed paper run")
        compare.add_argument("--run", required=True, type=Path)
        compare.add_argument("--venue-export", required=True, type=Path)
        parsed = compare.parse_args(args_list[1:])
        try:
            report = compare_capture_venue(parsed.run, parsed.venue_export)
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": type(exc).__name__, "verified": False}))
            return 2
        print(json.dumps(report, sort_keys=True))
        return 0 if report["exact_parity"] else 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="NDJSON path or - for standard input")
    parser.add_argument("--output", required=True, type=Path, help="new private run directory")
    parser.add_argument("--source", required=True, help="declared authorized capture source")
    parser.add_argument("--author", required=True, action="append", help="allowed signal author ID")
    parser.add_argument("--require-market-status", action="store_true")
    parser.add_argument("--fee-model", type=Path, help="explicit existing fee schedule JSON")
    parser.add_argument(
        "--max-signal-age", type=float, help="declared maximum source lag in seconds"
    )
    args = parser.parse_args(args_list)
    fee_model = FeeModel.load(args.fee_model, missing_ok=False) if args.fee_model else None

    def run(lines: Iterable[str]) -> dict[str, Any]:
        return run_capture(
            lines, args.output, source=args.source, allowed_authors=frozenset(args.author),
            require_market_status=args.require_market_status, fee_model=fee_model,
            max_signal_age_s=args.max_signal_age,
        )

    if args.input == "-":
        summary = run(sys.stdin)
    else:
        with Path(args.input).open(encoding="utf-8") as handle:
            summary = run(handle)
    print(json.dumps(summary, sort_keys=True))
    if args.require_market_status:
        return 0 if summary["modeled_sample_complete"] else 1
    return 0 if summary["ledger_integrity"] and summary["session_flat"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
