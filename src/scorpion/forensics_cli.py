from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import date, datetime, timedelta
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
from .execution_sensitivity import run_latency_sensitivity
from .history_archive import HistoryArchive
from .quote_tape import HistoricalQuoteTape
from .sizing_lab import (
    SizingConstraints,
    build_sizing_envelope,
    rank_segments,
    segment_completed_trades,
)
from .strategy_selector import SelectionStatus, select_candidates


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


def _latency_grid(value: str) -> tuple[int, ...]:
    try:
        values = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("latency grid must be comma-separated integers") from exc
    if not values or any(item < 0 for item in values):
        raise argparse.ArgumentTypeError("latency grid must contain non-negative values")
    return values


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
    parser.add_argument("--min-quote-coverage", type=float, default=0.95)
    parser.add_argument("--latency-sensitivity", action="store_true")
    parser.add_argument(
        "--latency-grid-ms",
        type=_latency_grid,
        default=(50, 100, 250, 500, 1000, 2000),
    )
    parser.add_argument("--full", action="store_true", help="Include completed trade rows")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    authors = _authors(args.author_ids)
    if not authors:
        raise SystemExit("--author-id or SCORPION_SIGNAL_AUTHOR_IDS is required")
    if args.latency_ms < 0:
        raise SystemExit("--latency-ms cannot be negative")
    if not 0 < args.min_quote_coverage <= 1:
        raise SystemExit("--min-quote-coverage must be in (0,1]")

    channels = frozenset(args.channel_ids or ALLOWED_CHANNEL_IDS)
    archive = HistoryArchive(args.archive)
    quote_tape = HistoricalQuoteTape.from_jsonl(args.quotes)
    report = run_archive_forensics(
        archive,
        quote_tape,
        channel_ids=channels,
        allowed_author_ids=authors,
        profile=ExecutionProfile(decision_latency=timedelta(milliseconds=args.latency_ms)),
        include_research_only=args.include_research_only,
    )
    segments = segment_completed_trades(report.completed_trades)
    constraints = SizingConstraints()
    rankings = rank_segments(segments, constraints=constraints)
    ranking_by_segment = {metric.segment: metric for metric in rankings}
    selections = select_candidates(rankings)
    selected_segments = {
        candidate.segment
        for candidate in selections
        if candidate.status is SelectionStatus.SELECTED
    }
    completeness_ok = all(item.exhaustive for item in report.completeness)

    actionable_legs = [
        leg
        for leg in report.legs
        if leg.event_kind.value in {"ENTRY", "ADD", "TRIM", "EXIT"}
        and leg.status is not ForensicStatus.RESEARCH_ONLY
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
    quote_coverage = quote_supported / len(actionable_legs) if actionable_legs else 0.0
    quote_coverage_ok = quote_coverage >= args.min_quote_coverage
    sizing_gate_ok = completeness_ok and quote_coverage_ok

    payload: dict[str, Any] = {
        "messages_seen": report.messages_seen,
        "channel_completeness": [asdict(item) for item in report.completeness],
        "historical_completeness_ok": completeness_ok,
        "completed_quote_supported_trades": len(report.completed_trades),
        "actionable_legs": len(actionable_legs),
        "quote_evidence_coverage": quote_coverage,
        "minimum_quote_evidence_coverage": args.min_quote_coverage,
        "quote_coverage_ok": quote_coverage_ok,
        "sizing_evidence_gate_ok": sizing_gate_ok,
        "leg_status_counts": {
            status.value: sum(leg.status is status for leg in report.legs)
            for status in ForensicStatus
        },
        "segment_rankings": [asdict(metric) for metric in rankings],
        "strategy_candidates": [asdict(candidate) for candidate in selections],
        "sizing_envelopes": [],
        "warnings": [],
    }
    if not completeness_ok:
        payload["warnings"].append(
            "History is not proven exhaustive for every requested channel; sizing envelopes "
            "are withheld until each channel reaches the beginning of accessible history."
        )
    if not quote_coverage_ok:
        payload["warnings"].append(
            "Timestamped option-quote coverage is below the configured evidence threshold; "
            "sizing envelopes are withheld to reduce missing-data selection bias."
        )
    if args.include_research_only:
        payload["warnings"].append(
            "ETF/lotto research-only buckets are included for analysis but remain blocked "
            "from the reviewed execution path by default."
        )

    if sizing_gate_ok:
        envelopes = []
        for name in sorted(selected_segments):
            metric = ranking_by_segment[name]
            envelopes.append(
                asdict(
                    build_sizing_envelope(
                        name,
                        segments[name],
                        constraints=constraints,
                        metrics=metric,
                    )
                )
            )
        payload["sizing_envelopes"] = envelopes
        if not envelopes:
            payload["warnings"].append(
                "No segment survived sample-depth, conservative-edge, false-discovery, "
                "stability, drawdown, and win-floor gates; no sizing envelope was emitted."
            )

    if args.latency_sensitivity:
        payload["latency_sensitivity"] = asdict(
            run_latency_sensitivity(
                archive,
                quote_tape,
                channel_ids=channels,
                allowed_author_ids=authors,
                latency_grid_ms=args.latency_grid_ms,
                include_research_only=args.include_research_only,
            )
        )
    if args.full:
        payload["completed_trades"] = [
            _trade_payload(trade) for trade in report.completed_trades
        ]

    rendered = json.dumps(payload, indent=2, sort_keys=True, default=_json_default)
    if args.output is not None:
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)
