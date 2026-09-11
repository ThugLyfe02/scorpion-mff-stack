from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .config import ALLOWED_CHANNEL_IDS
from .execution_trust import assess_fill_model_trust
from .fill_calibration import evaluate_fill_calibration, record_fill_observation
from .history_archive import HistoryArchive
from .latency_value import run_latency_value_frontier
from .microstructure import OptionMicrostructureTape
from .microstructure_windows import (
    acquisition_plan_payload,
    build_acquisition_plan,
    verify_acquisition_warmup,
)


def _authors(values: list[str] | None) -> frozenset[str]:
    if values:
        return frozenset(item.strip() for item in values if item.strip())
    return frozenset(
        item.strip()
        for item in os.environ.get("SCORPION_SIGNAL_AUTHOR_IDS", "").split(",")
        if item.strip()
    )


def _grid(value: str) -> tuple[int, ...]:
    try:
        result = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("grid must be comma-separated integers") from exc
    if not result or any(item < 0 for item in result):
        raise argparse.ArgumentTypeError("grid must contain non-negative integers")
    return result


def micro_plan_main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a reproducible contract/time acquisition plan from Discord history."
    )
    parser.add_argument("--archive", default="scorpion-history.db")
    parser.add_argument("--author-id", action="append", dest="author_ids")
    parser.add_argument("--channel-id", action="append", dest="channel_ids")
    parser.add_argument("--pre-ms", type=int, default=2000)
    parser.add_argument("--post-ms", type=int, default=10000)
    parser.add_argument("--warmup-ms", type=int, default=60000)
    parser.add_argument("--merge-gap-ms", type=int, default=1000)
    parser.add_argument(
        "--events",
        help="Optional normalized microstructure JSONL used to verify warm-up quote-state coverage.",
    )
    args = parser.parse_args()
    authors = _authors(args.author_ids)
    if not authors:
        raise SystemExit("--author-id or SCORPION_SIGNAL_AUTHOR_IDS is required")
    for name in ("pre_ms", "post_ms", "warmup_ms", "merge_gap_ms"):
        if getattr(args, name) < 0:
            raise SystemExit(f"--{name.replace('_', '-')} cannot be negative")
    archive = HistoryArchive(args.archive)
    channels = frozenset(args.channel_ids or ALLOWED_CHANNEL_IDS)
    messages = tuple(
        message
        for channel_id in sorted(channels)
        for message in archive.iter_channel(channel_id)
    )
    plan = build_acquisition_plan(
        messages,
        allowed_author_ids=authors,
        pre_context=timedelta(milliseconds=args.pre_ms),
        post_context=timedelta(milliseconds=args.post_ms),
        quote_warmup=timedelta(milliseconds=args.warmup_ms),
        merge_gap=timedelta(milliseconds=args.merge_gap_ms),
    )
    payload = acquisition_plan_payload(plan)
    if args.events:
        tape = OptionMicrostructureTape.from_jsonl(args.events)
        payload["warmup_coverage"] = asdict(verify_acquisition_warmup(plan, tape))
    print(json.dumps(payload, indent=2, sort_keys=True))


def fill_calibration_main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate or append observed outcomes to Scorpion fill-interval calibration."
    )
    parser.add_argument("--db", default="scorpion-fill-calibration.db")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("report")
    observe = subparsers.add_parser("observe")
    observe.add_argument("prediction_id")
    observe.add_argument("--quantity", type=int, required=True)
    observe.add_argument("--deadline", required=True, help="Exact ISO-8601 prediction horizon")
    observe.add_argument("--source", required=True)
    observe.add_argument("--average-fill-price", type=Decimal)
    observe.add_argument("--note", default="")
    args = parser.parse_args()

    if args.command == "report":
        report = evaluate_fill_calibration(args.db)
        payload = {
            "calibration": asdict(report),
            "trust": asdict(assess_fill_model_trust(report)),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    deadline = datetime.fromisoformat(args.deadline.replace("Z", "+00:00"))
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise SystemExit("--deadline must be timezone-aware")
    record_fill_observation(
        args.db,
        args.prediction_id,
        actual_filled_quantity=args.quantity,
        observation_deadline_ts_utc=deadline.astimezone(UTC),
        source=args.source,
        average_fill_price=args.average_fill_price,
        note=args.note,
    )
    report = evaluate_fill_calibration(args.db)
    payload = {
        "calibration": asdict(report),
        "trust": asdict(assess_fill_model_trust(report)),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def latency_value_main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Measure the marginal research value of decision, feed, and order-transport latency "
            "using the nanosecond microstructure replay engine."
        )
    )
    parser.add_argument("--archive", default="scorpion-history.db")
    parser.add_argument("--events", required=True)
    parser.add_argument("--author-id", action="append", dest="author_ids")
    parser.add_argument("--channel-id", action="append", dest="channel_ids")
    parser.add_argument("--include-research-only", action="store_true")
    parser.add_argument("--decision-grid-ms", type=_grid, default=(50, 100, 250, 500, 1000))
    parser.add_argument("--order-grid-ms", type=_grid, default=(0, 10, 25, 50, 100))
    parser.add_argument("--feed-grid-ms", type=_grid, default=(0, 10, 25, 50, 100))
    args = parser.parse_args()
    authors = _authors(args.author_ids)
    if not authors:
        raise SystemExit("--author-id or SCORPION_SIGNAL_AUTHOR_IDS is required")
    channels = frozenset(args.channel_ids or ALLOWED_CHANNEL_IDS)
    archive = HistoryArchive(args.archive)
    tape = OptionMicrostructureTape.from_jsonl(args.events)
    report = run_latency_value_frontier(
        archive,
        tape,
        channel_ids=channels,
        allowed_author_ids=authors,
        decision_grid_ms=args.decision_grid_ms,
        order_transport_grid_ms=args.order_grid_ms,
        feed_transport_grid_ms=args.feed_grid_ms,
        include_research_only=args.include_research_only,
    )
    print(json.dumps(asdict(report), indent=2, sort_keys=True, default=str))
