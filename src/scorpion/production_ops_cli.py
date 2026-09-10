from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .hot_path_benchmark import HotPathBenchmarkPolicy, benchmark_hot_path
from .operator_observability import build_operator_observability_snapshot
from .production_bottleneck_audit import audit_production_bottlenecks
from .production_chaos_drills import run_isolated_partial_outage_chaos_drills
from .production_readiness import ProductionReadinessPolicy, evaluate_production_readiness


def _emit(payload: object) -> None:
    print(json.dumps(asdict(payload), sort_keys=True, indent=2, default=str))


def bottleneck_audit_main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit Scorpion production bottlenecks and rollout conflicts."
    )
    parser.add_argument("--db", required=True, help="Existing Scorpion SQLite database")
    args = parser.parse_args()
    report = audit_production_bottlenecks(args.db)
    _emit(report)
    raise SystemExit(0 if report.ready_for_rollout else 2)


def operator_status_main() -> None:
    parser = argparse.ArgumentParser(
        description="Render one read-only operator snapshot across Scorpion production seams."
    )
    parser.add_argument("--db", required=True, help="Existing Scorpion SQLite database")
    args = parser.parse_args()
    report = build_operator_observability_snapshot(args.db)
    _emit(report)
    raise SystemExit(0 if not report.blockers else 2)


def hot_path_benchmark_main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark Scorpion's isolated core, durable receipt and normalized hot path."
    )
    parser.add_argument("--workspace", required=True, help="Isolated benchmark workspace")
    parser.add_argument("--samples", type=int, default=80)
    args = parser.parse_args()
    report = benchmark_hot_path(
        samples=args.samples,
        workspace=args.workspace,
        policy=HotPathBenchmarkPolicy(minimum_samples=min(30, args.samples)),
    )
    _emit(report)
    raise SystemExit(0 if report.passed else 2)


def production_readiness_main() -> None:
    parser = argparse.ArgumentParser(
        description="Issue a short-lived production-readiness certificate from live evidence."
    )
    parser.add_argument("--db", required=True, help="Existing Scorpion SQLite database")
    parser.add_argument("--workspace", required=True, help="Isolated benchmark/chaos workspace")
    parser.add_argument("--component", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--samples", type=int, default=80)
    parser.add_argument(
        "--skip-chaos",
        action="store_true",
        help="Do not run isolated chaos drills; resulting policy explicitly records this choice.",
    )
    args = parser.parse_args()
    report = evaluate_production_readiness(
        args.db,
        workspace=args.workspace,
        component=args.component,
        operator=args.operator,
        policy=ProductionReadinessPolicy(
            benchmark_samples=args.samples,
            require_chaos_drills=not args.skip_chaos,
        ),
        benchmark_policy=HotPathBenchmarkPolicy(minimum_samples=min(30, args.samples)),
    )
    _emit(report)
    raise SystemExit(0 if report.ready else 2)


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
    _emit(report)
    raise SystemExit(0 if report.passed else 2)
