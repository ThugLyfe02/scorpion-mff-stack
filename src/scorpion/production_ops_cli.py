from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .production_bottleneck_audit import audit_production_bottlenecks
from .production_chaos_drills import run_isolated_partial_outage_chaos_drills


def bottleneck_audit_main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit Scorpion production bottlenecks and rollout conflicts."
    )
    parser.add_argument("--db", required=True, help="Existing Scorpion SQLite database")
    args = parser.parse_args()
    report = audit_production_bottlenecks(args.db)
    print(json.dumps(asdict(report), sort_keys=True, indent=2, default=str))
    raise SystemExit(0 if report.ready_for_rollout else 2)


def partial_outage_drill_main() -> None:
    parser = argparse.ArgumentParser(
        description="Run isolated Scorpion partial-outage chaos drills."
    )
    parser.add_argument("--workspace", required=True, help="Directory for isolated drill stores")
    parser.add_argument("--component", required=True)
    parser.add_argument("--operator", required=True)
    args = parser.parse_args()
    report = run_isolated_partial_outage_chaos_drills(
        args.workspace,
        component=args.component,
        operator=args.operator,
    )
    print(json.dumps(asdict(report), sort_keys=True, indent=2, default=str))
    raise SystemExit(0 if report.passed else 2)
