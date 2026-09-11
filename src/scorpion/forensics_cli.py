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

from . import __version__
from .config import ALLOWED_CHANNEL_IDS
from .dataset_fingerprint import fingerprint_history_archive, fingerprint_quote_jsonl
from .execution_forensics import (
    CompletedTrade,
    ExecutionProfile,
    ForensicStatus,
    run_archive_forensics,
)
from .execution_sensitivity import run_latency_sensitivity
from .history_archive import HistoryArchive
from .parser import PARSER_VERSION
from .policy_bundle import RuntimePolicyBundle
from .provenance import build_research_manifest
from .quote_tape import HistoricalQuoteTape
from .sizing_lab import (
    SizingConstraints,
    build_sizing_envelope,
    rank_segments,
    segment_completed_trades,
)
from .strategy_selector import SelectionStatus, select_candidates
from .walk_forward import WalkForwardPolicy, evaluate_walk_forward


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
    parser.add_argument("--min-depth-coverage", type=float, default=0.95)
    parser.add_argument(
        "--code-revision",
        default=os.environ.get("SCORPION_CODE_REVISION", "").strip(),
        help="Exact git commit SHA/revision used to produce this report.",
    )
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
    if not 0 < args.min_depth_coverage <= 1:
        raise SystemExit("--min-depth-coverage must be in (0,1]")

    channels = frozenset(args.channel_ids or ALLOWED_CHANNEL_IDS)
    archive = HistoryArchive(args.archive)
    quote_tape = HistoricalQuoteTape.from_jsonl(args.quotes)
    runtime_policy = RuntimePolicyBundle()
    execution_profile = ExecutionProfile(
        decision_latency=timedelta(milliseconds=args.latency_ms)
    )
    sizing_constraints = SizingConstraints()
    walk_forward_policy = WalkForwardPolicy()

    report = run_archive_forensics(
        archive,
        quote_tape,
        channel_ids=channels,
        allowed_author_ids=authors,
        profile=execution_profile,
        runtime_policy=runtime_policy,
        include_research_only=args.include_research_only,
    )

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
            ForensicStatus.PARTIAL_DEPTH,
            ForensicStatus.STALE_ENTRY,
            ForensicStatus.LIMIT_NOT_FILLED,
        }
        for leg in actionable_legs
    )
    quote_coverage = quote_supported / len(actionable_legs) if actionable_legs else 0.0
    quote_coverage_ok = quote_coverage >= args.min_quote_coverage
    completeness_ok = all(item.exhaustive for item in report.completeness)

    completed = report.completed_trades
    depth_complete_trades = tuple(trade for trade in completed if trade.depth_evidence_complete)
    depth_coverage = len(depth_complete_trades) / len(completed) if completed else 0.0
    depth_coverage_ok = depth_coverage >= args.min_depth_coverage
    provenance_ok = bool(args.code_revision.strip())

    # Discovery/sizing is intentionally restricted to lifecycles with explicit displayed-depth
    # support. The broader completed set remains visible in diagnostics for sensitivity analysis.
    sizing_segments = segment_completed_trades(depth_complete_trades)
    rankings = rank_segments(sizing_segments, constraints=sizing_constraints)
    ranking_by_segment = {metric.segment: metric for metric in rankings}
    selections = select_candidates(rankings)
    selected_segments = {
        candidate.segment
        for candidate in selections
        if candidate.status is SelectionStatus.SELECTED
    }

    walk_forward_reports = {
        name: evaluate_walk_forward(
            sizing_segments[name],
            policy=walk_forward_policy,
            sizing_constraints=sizing_constraints,
        )
        for name in sorted(selected_segments)
    }
    walk_forward_passed = {
        name for name, result in walk_forward_reports.items() if result.passed
    }

    history_fingerprint = fingerprint_history_archive(archive, channel_ids=channels)
    quote_fingerprint = fingerprint_quote_jsonl(args.quotes)
    manifest = build_research_manifest(
        code_revision=args.code_revision.strip() or "UNSET",
        package_version=__version__,
        parser_version=PARSER_VERSION,
        policies={
            "runtime": runtime_policy,
            "execution_profile": execution_profile,
            "sizing_constraints": sizing_constraints,
            "walk_forward": walk_forward_policy,
        },
        datasets={
            "discord_history": history_fingerprint.sha256,
            "option_quote_tape": quote_fingerprint.sha256,
        },
        parameters={
            "channels": tuple(sorted(channels)),
            "include_research_only": args.include_research_only,
            "latency_ms": args.latency_ms,
            "minimum_quote_coverage": args.min_quote_coverage,
            "minimum_depth_coverage": args.min_depth_coverage,
        },
    )

    sizing_gate_ok = completeness_ok and quote_coverage_ok and depth_coverage_ok and provenance_ok
    payload: dict[str, Any] = {
        "research_manifest": {
            **manifest.payload(),
            "manifest_hash": manifest.manifest_hash,
        },
        "history_dataset": asdict(history_fingerprint),
        "quote_dataset": asdict(quote_fingerprint),
        "policy_fingerprint": report.policy_fingerprint,
        "messages_seen": report.messages_seen,
        "channel_completeness": [asdict(item) for item in report.completeness],
        "historical_completeness_ok": completeness_ok,
        "completed_quote_supported_trades": len(completed),
        "depth_complete_completed_trades": len(depth_complete_trades),
        "depth_evidence_coverage": depth_coverage,
        "minimum_depth_evidence_coverage": args.min_depth_coverage,
        "depth_coverage_ok": depth_coverage_ok,
        "actionable_legs": len(actionable_legs),
        "quote_evidence_coverage": quote_coverage,
        "minimum_quote_evidence_coverage": args.min_quote_coverage,
        "quote_coverage_ok": quote_coverage_ok,
        "code_provenance_ok": provenance_ok,
        "sizing_evidence_gate_ok": sizing_gate_ok,
        "leg_status_counts": {
            status.value: sum(leg.status is status for leg in report.legs)
            for status in ForensicStatus
        },
        "segment_rankings": [asdict(metric) for metric in rankings],
        "strategy_candidates": [asdict(candidate) for candidate in selections],
        "walk_forward": {
            name: asdict(result) for name, result in walk_forward_reports.items()
        },
        "sizing_envelopes": [],
        "warnings": [],
    }
    if not provenance_ok:
        payload["warnings"].append(
            "Exact code revision is missing; sizing envelopes are withheld because the report "
            "cannot be reproduced against a cryptographically identified code revision."
        )
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
    if not depth_coverage_ok:
        payload["warnings"].append(
            "Displayed top-of-book depth evidence is below threshold; risk sizing is withheld "
            "instead of assuming multi-contract fills from price-only quotes."
        )
    if args.include_research_only:
        payload["warnings"].append(
            "ETF/lotto research-only buckets are included for analysis but remain blocked "
            "from the reviewed execution path by default."
        )

    if sizing_gate_ok:
        envelopes = []
        for name in sorted(walk_forward_passed):
            metric = ranking_by_segment[name]
            envelopes.append(
                asdict(
                    build_sizing_envelope(
                        name,
                        sizing_segments[name],
                        constraints=sizing_constraints,
                        metrics=metric,
                    )
                )
            )
        payload["sizing_envelopes"] = envelopes
        failed_oos = sorted(selected_segments - walk_forward_passed)
        if failed_oos:
            payload["warnings"].append(
                "Selected in-sample segments failed purged walk-forward validation and were "
                f"withheld from sizing: {','.join(failed_oos)}"
            )
        if not envelopes:
            payload["warnings"].append(
                "No segment survived provenance, completeness, liquidity depth, FDR, "
                "stability, drawdown, win-floor, and purged out-of-sample gates."
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
        payload["completed_trades"] = [_trade_payload(trade) for trade in completed]

    rendered = json.dumps(payload, indent=2, sort_keys=True, default=_json_default)
    if args.output is not None:
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)
