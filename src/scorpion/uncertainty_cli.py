from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from .config import ALLOWED_CHANNEL_IDS
from .history_archive import HistoryArchive
from .policy_bundle import RuntimePolicyBundle
from .quote_tape import HistoricalQuoteTape
from .sizing_lab import SizingConstraints
from .uncertainty_envelope import (
    compact_execution_scenarios,
    default_execution_scenarios,
    run_execution_uncertainty_envelope,
)


def _authors(values: list[str] | None) -> frozenset[str]:
    if values:
        return frozenset(value.strip() for value in values if value.strip())
    return frozenset(
        value.strip()
        for value in os.environ.get("SCORPION_SIGNAL_AUTHOR_IDS", "").split(",")
        if value.strip()
    )


def uncertainty_main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Stress exact-execution history across multiple causal latency/freshness/limit "
            "assumptions and report the worst conservative edge."
        )
    )
    parser.add_argument("--archive", default="scorpion-history.db")
    parser.add_argument("--quotes", required=True, type=Path)
    parser.add_argument("--author-id", action="append", dest="author_ids")
    parser.add_argument("--channel-id", action="append", dest="channel_ids")
    parser.add_argument("--full-grid", action="store_true")
    parser.add_argument("--minimum-scenario-samples", type=int, default=20)
    parser.add_argument("--minimum-passing-fraction", type=float, default=0.80)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    authors = _authors(args.author_ids)
    if not authors:
        raise SystemExit("--author-id or SCORPION_SIGNAL_AUTHOR_IDS is required")
    if args.minimum_scenario_samples <= 0:
        raise SystemExit("--minimum-scenario-samples must be positive")
    if not 0 < args.minimum_passing_fraction <= 1:
        raise SystemExit("--minimum-passing-fraction must be in (0,1]")

    scenarios = (
        default_execution_scenarios() if args.full_grid else compact_execution_scenarios()
    )
    report = run_execution_uncertainty_envelope(
        HistoryArchive(args.archive),
        HistoricalQuoteTape.from_jsonl(args.quotes),
        channel_ids=frozenset(args.channel_ids or ALLOWED_CHANNEL_IDS),
        allowed_author_ids=authors,
        runtime_policy=RuntimePolicyBundle(),
        scenarios=scenarios,
        constraints=SizingConstraints(min_samples=args.minimum_scenario_samples),
        minimum_scenario_samples=args.minimum_scenario_samples,
        minimum_passing_fraction=args.minimum_passing_fraction,
    )
    rendered = json.dumps(asdict(report), indent=2, sort_keys=True, default=str)
    if args.output is not None:
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)
