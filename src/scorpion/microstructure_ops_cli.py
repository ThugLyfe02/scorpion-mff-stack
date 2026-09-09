from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .config import ALLOWED_CHANNEL_IDS
from .fill_calibration import evaluate_fill_calibration, record_fill_observation
from .history_archive import HistoryArchive
from .microstructure_windows import acquisition_plan_payload, build_acquisition_plan


def _authors(values: list[str] | None) -> frozenset[str]:
    if values:
        return frozenset(item.strip() for item in values if item.strip())
    return frozenset(
        item.strip()
        for item in os.environ.get("SCORPION_SIGNAL_AUTHOR_IDS", "").split(",")
        if item.strip()
    )


def micro_plan_main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a reproducible contract/time acquisition plan from Discord history."
    )
    parser.add_argument("--archive", default="scorpion-history.db")
    parser.add_argument("--author-id", action="append", dest="author_ids")
    parser.add_argument("--channel-id", action="append", dest="channel_ids")
    parser.add_argument("--pre-ms", type=int, default=2000)
    parser.add_argument("--post-ms", type=int, default=10000)
    parser.add_argument("--merge-gap-ms", type=int, default=1000)
    args = parser.parse_args()
    authors = _authors(args.author_ids)
    if not authors:
        raise SystemExit("--author-id or SCORPION_SIGNAL_AUTHOR_IDS is required")
    for name in ("pre_ms", "post_ms", "merge_gap_ms"):
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
        merge_gap=timedelta(milliseconds=args.merge_gap_ms),
    )
    print(json.dumps(acquisition_plan_payload(plan), indent=2, sort_keys=True))


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
        print(json.dumps(asdict(evaluate_fill_calibration(args.db)), indent=2, sort_keys=True))
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
    print(json.dumps(asdict(evaluate_fill_calibration(args.db)), indent=2, sort_keys=True))
