from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from .config import ALLOWED_CHANNEL_IDS
from .execution_forensics import (
    CompletedTrade,
    ExecutionProfile,
    ForensicStatus,
    run_archive_forensics,
)
from .history_archive import HistoryArchive
from .quote_tape import HistoricalQuoteTape
from .sizing_lab import (
    SizingConstraints,
    build_sizing_envelope,
    rank_segments,
    segment_completed_trades,
)


def _json_default(value: object) -> object:
    if isinstance(value, (Decimal, datetime, date)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _authors(cli_values: list[str] | None) -> frozenset[str]:
    if cli_values:
        return frozenset(value.strip() for value in cli_values if value.strip())
    return frozenset(
        value.strip()
        for value in os.environ.get("SCORPION_SIGNAL_AUTHOR_IDS", "").split(",")
        if value.strip()
    )


def _trade_payload(trade: CompletedTrade) -> dict[str, Any]:
    return asdict(trade)


def forensics_main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Replay archived Discord history against timestamped option quotes using the "
            "current deterministic execution rules."
        )
    )
    parser.add_argument("--archive", default="scorpion-history.db")
    parser.add_argument("--quotes", required=True, help="Provider-neutral quote JSONL")
    parser.add_argument("--author-id", action="append", dest="author_ids")
    parser.add_argument("--channel-id", action="append", dest="channel_ids")
    parser.add_argument("--include-research-only", action="store_true")
    parser.add_argument("--latency-ms", type=int, default=250)
    parser.add_argument("--full", action="store_true", help="Include completed trade rows")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    authors = _authors(args.author_ids)
    if not authors:
        raise SystemExit("--author-id or SCORPION_SIGNAL_AUTHOR_IDS is required")
    channels = frozenset(args.channel_ids or ALLOWED_CHANNEL_IDS)
    report = run_archive_forensics(
        HistoryArchive(args.archive),
        HistoricalQuoteTape.from_jsonl(args.quotes),
        channel_ids=channels,
        allowed_author_ids=authors,
        profile=ExecutionProfile(
            decision_latency=__import__("datetime").timedelta(milliseconds=args.latency_ms)
        ),
        include_research_only=args.include_research_only,
    )
    segments = segment_completed_trades(report.completed_trades)
    constraints = SizingConstraints()
    rankings = rank_segments(segments, constraints=constraints)
    completeness_ok = all(item.exhaustive for item in report.completeness)

    actionable_legs = [
        leg
        for leg in report.legs
        if leg.event_kind.value in {"ENTRY", "ADD", "TRIM", "EXIT"}
    ]
    quote_supported = sum(
        leg.status
        in {
            ForensicStatus.FILLED,
            ForensicStatus.STALE_ENTRY,
            ForensicStatus.LIMIT_NOT_FILLED,
        }
        for leg in actionable_legs
    )
    payload: dict[str, Any] = {
        "messages_seen": report.messages_seen,
        "channel_completeness": [asdict(item) for item in report.completeness],
        "historical_completeness_ok": completeness_ok,
        "completed_quote_supported_trades": len(report.completed_trades),
        "actionable_legs": len(actionable_legs),
        "quote_evidence_coverage": (
            quote_supported / len(actionable_legs) if actionable_legs else 0.0
        ),
        "leg_status_counts": {
            status.value: sum(leg.status is status for leg in report.legs)
            for status in ForensicStatus
        },
        "segment_rankings": [asdict(metric) for metric in rankings],
        "sizing_envelopes": [],
        "warnings": [],
    }
    if not completeness_ok:
        payload["warnings"].append(
            "History is not proven exhaustive for every requested channel; sizing envelopes "
            "are withheld until each channel reaches the beginning."
        )
    else:
        payload["sizing_envelopes"] = [
            asdict(build_sizing_envelope(name, trades, constraints=constraints))
            for name, trades in segments.items()
        ]
    if args.full:
        payload["completed_trades"] = [
            _trade_payload(trade) for trade in report.completed_trades
        ]

    rendered = json.dumps(payload, indent=2, sort_keys=True, default=_json_default)
    if args.output is not None:
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)
