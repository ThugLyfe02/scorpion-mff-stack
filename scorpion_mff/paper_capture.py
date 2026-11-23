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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .calendar import et_date
from .engine import Engine, EngineConfig
from .execution_book import ExecutionBook
from .fills import FillLog
from .health import Heartbeat
from .killswitch import KillSwitch
from .listener import Listener, TapeTransport
from .paper_market import MarketQuote, PaperQuoteBook, QuoteDrivenPaperBroker
from .parser import CHANNEL_IDS
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
) -> dict[str, Any]:
    if not source.strip() or not allowed_authors or "" in allowed_authors or max_events <= 0:
        raise ValueError("declare capture source, authorized authors and positive event bound")
    # Exclusive directory creation prevents appending a new run to a previous book.
    output.mkdir(parents=True, exist_ok=False)
    clock = ReplayClock()
    quotes = PaperQuoteBook(clock)
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
    integrity = (
        restored.digest() == book.digest() and not restored.torn_lines
        and not book.anomalies and not book.unresolved_orders() and position_match
    )
    summary: dict[str, Any] = {
        "authority": "PAPER_ONLY", "source_declared_not_authenticated": source,
        "model": MODEL, "session_date": str(day), "input_sha256": digest.hexdigest(),
        "events": count, "admission_counts": dict(sorted(reasons.items())),
        "modeled_fills": len(book.fills), "modeled_realized_usd": book.realized_total,
        "open_modeled_positions": book.snapshot()["positions"],
        "execution_digest": book.digest(), "ledger_integrity": integrity,
        "session_flat": not book.open_positions(),
        "certifies_live_fills": False,
    }
    # A crash before this exclusive, fsync'd receipt leaves an incomplete run, not a pass.
    with (output / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="NDJSON path or - for standard input")
    parser.add_argument("--output", required=True, type=Path, help="new private run directory")
    parser.add_argument("--source", required=True, help="declared authorized capture source")
    parser.add_argument("--author", required=True, action="append", help="allowed signal author ID")
    args = parser.parse_args(argv)
    if args.input == "-":
        summary = run_capture(sys.stdin, args.output, source=args.source,
                              allowed_authors=frozenset(args.author))
    else:
        with Path(args.input).open(encoding="utf-8") as handle:
            summary = run_capture(handle, args.output, source=args.source,
                                  allowed_authors=frozenset(args.author))
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["ledger_integrity"] and summary["session_flat"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
