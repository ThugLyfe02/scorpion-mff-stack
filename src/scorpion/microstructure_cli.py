from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from . import __version__
from .backtest_overfit import OverfitPolicy, build_daily_return_panel, evaluate_backtest_overfit
from .config import ALLOWED_CHANNEL_IDS
from .contract_terms import ContractTermsRegistry
from .dataset_fingerprint import fingerprint_history_archive
from .execution_attribution import build_execution_attribution_report
from .execution_meta_labels import (
    ExecutionMetaLabelPolicy,
    build_execution_meta_labels,
    persist_execution_meta_labels,
)
from .execution_trust import FillTrustPolicy, assess_fill_model_trust
from .fill_calibration import evaluate_fill_calibration
from .history_archive import HistoryArchive
from .microstructure import OptionMicrostructureTape, ns_from_datetime
from .microstructure_forensics import (
    MicroForensicStatus,
    MicrostructureExecutionProfile,
    run_archive_microstructure_forensics,
)
from .parser import PARSER_VERSION
from .policy_bundle import RuntimePolicyBundle
from .provenance import build_research_manifest, fingerprint_file
from .regime_stability import RegimeStabilityPolicy, evaluate_regime_stability
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
        return frozenset(item.strip() for item in cli_values if item.strip())
    return frozenset(
        item.strip()
        for item in os.environ.get("SCORPION_SIGNAL_AUTHOR_IDS", "").split(",")
        if item.strip()
    )


def microstructure_forensics_main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Replay Discord history against nanosecond option quote/trade events with causal "
            "decision pricing and conservative displayed-depth fill certification."
        )
    )
    parser.add_argument("--archive", default="scorpion-history.db")
    parser.add_argument("--events", required=True, help="Normalized microstructure JSONL")
    parser.add_argument("--author-id", action="append", dest="author_ids")
    parser.add_argument("--channel-id", action="append", dest="channel_ids")
    parser.add_argument("--include-research-only", action="store_true")
    parser.add_argument("--decision-latency-ms", type=int, default=250)
    parser.add_argument("--order-transport-ms", type=int, default=0)
    parser.add_argument("--feed-transport-ms", type=int, default=0)
    parser.add_argument("--decision-quote-max-age-ms", type=int, default=1000)
    parser.add_argument("--market-quote-max-age-ms", type=int, default=1000)
    parser.add_argument("--limit-wait-ms", type=int, default=5000)
    parser.add_argument("--min-certified-execution-coverage", type=float, default=0.95)
    parser.add_argument(
        "--contract-terms",
        type=Path,
        help="Point-in-time contract terms JSONL with verified multiplier/deliverable metadata.",
    )
    parser.add_argument(
        "--allow-unverified-contract-terms",
        action="store_true",
        help="Permit diagnostics without verified standard-contract terms; not recommended.",
    )
    parser.add_argument(
        "--fill-calibration-db",
        type=Path,
        help="Shadow/paper fill calibration database used to gate authoritative research.",
    )
    parser.add_argument(
        "--allow-uncalibrated-fill-model",
        action="store_true",
        help="Permit sizing research before empirical fill-model trust is established.",
    )
    parser.add_argument(
        "--meta-label-db",
        type=Path,
        help="Optional research DB for execution-aware shadow-model targets.",
    )
    parser.add_argument(
        "--code-revision",
        default=os.environ.get("SCORPION_CODE_REVISION", "").strip(),
    )
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    authors = _authors(args.author_ids)
    if not authors:
        raise SystemExit("--author-id or SCORPION_SIGNAL_AUTHOR_IDS is required")
    for name in (
        "decision_latency_ms",
        "order_transport_ms",
        "feed_transport_ms",
        "decision_quote_max_age_ms",
        "market_quote_max_age_ms",
        "limit_wait_ms",
    ):
        if getattr(args, name) < 0:
            raise SystemExit(f"--{name.replace('_', '-')} cannot be negative")
    if not 0 < args.min_certified_execution_coverage <= 1:
        raise SystemExit("--min-certified-execution-coverage must be in (0,1]")

    channels = frozenset(args.channel_ids or ALLOWED_CHANNEL_IDS)
    archive = HistoryArchive(args.archive)
    tape = OptionMicrostructureTape.from_jsonl(args.events)
    quality = tape.quality_report()
    runtime_policy = RuntimePolicyBundle()
    profile = MicrostructureExecutionProfile(
        decision_latency=timedelta(milliseconds=args.decision_latency_ms),
        order_transport_latency=timedelta(milliseconds=args.order_transport_ms),
        feed_transport_latency=timedelta(milliseconds=args.feed_transport_ms),
        decision_quote_max_age=timedelta(milliseconds=args.decision_quote_max_age_ms),
        market_quote_max_age=timedelta(milliseconds=args.market_quote_max_age_ms),
        entry_limit_wait=timedelta(milliseconds=args.limit_wait_ms),
    )
    sizing_constraints = SizingConstraints()
    walk_forward_policy = WalkForwardPolicy()
    fill_trust_policy = FillTrustPolicy()
    regime_policy = RegimeStabilityPolicy()
    overfit_policy = OverfitPolicy()
    meta_label_policy = ExecutionMetaLabelPolicy()
    report = run_archive_microstructure_forensics(
        archive,
        tape,
        channel_ids=channels,
        allowed_author_ids=authors,
        profile=profile,
        runtime_policy=runtime_policy,
        include_research_only=args.include_research_only,
    )

    completeness_ok = all(item.exhaustive for item in report.completeness)
    evidence_denominator = report.certified_actionable_legs + report.uncertain_actionable_legs
    certified_coverage = (
        report.certified_actionable_legs / evidence_denominator if evidence_denominator else 0.0
    )
    certified_coverage_ok = certified_coverage >= args.min_certified_execution_coverage
    provenance_ok = bool(args.code_revision.strip())
    quality_ok = not quality.critical

    contract_requests = tuple(
        (leg.contract_key, ns_from_datetime(leg.source_ts_utc))
        for leg in report.legs
        if leg.contract_key is not None
    )
    contract_registry = (
        ContractTermsRegistry.from_jsonl(args.contract_terms)
        if args.contract_terms is not None
        else None
    )
    contract_coverage = (
        contract_registry.coverage(contract_requests) if contract_registry is not None else None
    )
    contract_terms_ok = (
        contract_coverage.standard_only
        if contract_coverage is not None
        else args.allow_unverified_contract_terms
    )

    fill_calibration = (
        evaluate_fill_calibration(args.fill_calibration_db)
        if args.fill_calibration_db is not None
        else None
    )
    fill_trust = (
        assess_fill_model_trust(fill_calibration, policy=fill_trust_policy)
        if fill_calibration is not None
        else None
    )
    fill_model_trust_ok = (
        fill_trust.allows_authoritative_research
        if fill_trust is not None
        else args.allow_uncalibrated_fill_model
    )

    attribution, attribution_legs = build_execution_attribution_report(report, tape)
    execution_meta_labels = build_execution_meta_labels(
        report,
        tape,
        policy=meta_label_policy,
    )

    certified_completed = tuple(
        trade for trade in report.completed_trades if trade.depth_evidence_complete
    )
    regime_stability = evaluate_regime_stability(
        certified_completed,
        report.legs,
        policy=regime_policy,
    )
    regime_stability_ok = regime_stability.passed

    segments = segment_completed_trades(certified_completed)
    rankings = rank_segments(segments, constraints=sizing_constraints)
    ranking_by_segment = {metric.segment: metric for metric in rankings}
    selections = select_candidates(rankings)
    selected = {
        item.segment for item in selections if item.status is SelectionStatus.SELECTED
    }
    walk_reports = {
        name: evaluate_walk_forward(
            segments[name],
            policy=walk_forward_policy,
            sizing_constraints=sizing_constraints,
        )
        for name in sorted(selected)
    }
    walk_passed = {name for name, item in walk_reports.items() if item.passed}

    _, overfit_panel = build_daily_return_panel(
        segments,
        minimum_trade_samples=sizing_constraints.min_samples,
    )
    overfit_report = evaluate_backtest_overfit(overfit_panel, policy=overfit_policy)
    selection_overfit_ok = overfit_report.passed

    history_fp = fingerprint_history_archive(archive, channel_ids=channels)
    market_fp = fingerprint_file(args.events)
    datasets = {
        "discord_history": history_fp.sha256,
        "option_microstructure": market_fp.sha256,
        "contract_terms": (
            contract_registry.fingerprint if contract_registry is not None else "UNSET"
        ),
    }
    manifest = build_research_manifest(
        code_revision=args.code_revision.strip() or "UNSET",
        package_version=__version__,
        parser_version=PARSER_VERSION,
        policies={
            "runtime": runtime_policy,
            "microstructure_execution_profile": profile,
            "sizing_constraints": sizing_constraints,
            "walk_forward": walk_forward_policy,
            "fill_trust": fill_trust_policy,
            "regime_stability": regime_policy,
            "backtest_overfit": overfit_policy,
            "execution_meta_labels": meta_label_policy,
        },
        datasets=datasets,
        parameters={
            "channels": tuple(sorted(channels)),
            "include_research_only": args.include_research_only,
            "minimum_certified_execution_coverage": args.min_certified_execution_coverage,
        },
    )

    meta_labels_persisted = 0
    if args.meta_label_db is not None:
        meta_labels_persisted = persist_execution_meta_labels(
            args.meta_label_db,
            execution_meta_labels,
            research_manifest_hash=manifest.manifest_hash,
        )

    sizing_gate_ok = (
        completeness_ok
        and quality_ok
        and certified_coverage_ok
        and provenance_ok
        and contract_terms_ok
        and fill_model_trust_ok
        and regime_stability_ok
        and selection_overfit_ok
    )
    status_counts = {
        status.value: sum(leg.status is status for leg in report.legs)
        for status in MicroForensicStatus
    }
    meta_outcomes = Counter(label.outcome.value for label in execution_meta_labels)
    payload: dict[str, Any] = {
        "research_manifest": {
            **manifest.payload(),
            "manifest_hash": manifest.manifest_hash,
        },
        "history_dataset": asdict(history_fp),
        "microstructure_dataset": asdict(market_fp),
        "microstructure_quality": asdict(quality),
        "policy_fingerprint": report.policy_fingerprint,
        "messages_seen": report.messages_seen,
        "channel_completeness": [asdict(item) for item in report.completeness],
        "historical_completeness_ok": completeness_ok,
        "certified_actionable_legs": report.certified_actionable_legs,
        "uncertain_actionable_legs": report.uncertain_actionable_legs,
        "certified_execution_coverage": certified_coverage,
        "minimum_certified_execution_coverage": args.min_certified_execution_coverage,
        "certified_execution_coverage_ok": certified_coverage_ok,
        "market_data_quality_ok": quality_ok,
        "code_provenance_ok": provenance_ok,
        "contract_terms": asdict(contract_coverage) if contract_coverage is not None else None,
        "contract_terms_ok": contract_terms_ok,
        "fill_calibration": asdict(fill_calibration) if fill_calibration is not None else None,
        "fill_model_trust": asdict(fill_trust) if fill_trust is not None else None,
        "fill_model_trust_ok": fill_model_trust_ok,
        "execution_attribution": asdict(attribution),
        "execution_meta_labels": {
            "count": len(execution_meta_labels),
            "persisted": meta_labels_persisted,
            "outcome_counts": dict(sorted(meta_outcomes.items())),
        },
        "regime_stability": asdict(regime_stability),
        "regime_stability_ok": regime_stability_ok,
        "backtest_overfit": asdict(overfit_report),
        "selection_overfit_ok": selection_overfit_ok,
        "completed_certified_trades": len(certified_completed),
        "leg_status_counts": status_counts,
        "segment_rankings": [asdict(item) for item in rankings],
        "strategy_candidates": [asdict(item) for item in selections],
        "walk_forward": {name: asdict(item) for name, item in walk_reports.items()},
        "sizing_evidence_gate_ok": sizing_gate_ok,
        "sizing_envelopes": [],
        "warnings": [],
    }

    warnings: list[str] = payload["warnings"]
    if not provenance_ok:
        warnings.append("Exact code revision is missing; sizing is withheld.")
    if not completeness_ok:
        warnings.append("Discord history is not proven exhaustive; sizing is withheld.")
    if not quality_ok:
        warnings.append(
            "Microstructure feed has critical timestamp/crossed-market quality failures; "
            "sizing is withheld."
        )
    if not certified_coverage_ok:
        warnings.append(
            "Too many actionable legs have queue/depth uncertainty; sizing is withheld instead "
            "of treating possible resting fills as realized fills."
        )
    if not contract_terms_ok:
        warnings.append(
            "Point-in-time option contract terms are missing, ambiguous, adjusted, or use a "
            "nonstandard multiplier; authoritative sizing is withheld rather than assuming x100."
        )
    if not fill_model_trust_ok:
        warnings.append(
            "The execution fill model is not empirically trusted by shadow/paper calibration; "
            "authoritative sizing is withheld."
        )
    if not regime_stability_ok:
        warnings.append(
            "Certified trade performance is not proven stable across spread, quote-age, latency, "
            "and session regimes; authoritative sizing is withheld."
        )
    if not selection_overfit_ok:
        warnings.append(
            "The strategy-selection search is not proven robust under CSCV-style backtest-overfit "
            "testing; authoritative sizing is withheld."
        )

    if sizing_gate_ok:
        payload["sizing_envelopes"] = [
            asdict(
                build_sizing_envelope(
                    name,
                    segments[name],
                    constraints=sizing_constraints,
                    metrics=ranking_by_segment[name],
                )
            )
            for name in sorted(walk_passed)
        ]
        failed_oos = sorted(selected - walk_passed)
        if failed_oos:
            warnings.append(
                "In-sample candidates failed purged walk-forward and were withheld: "
                + ",".join(failed_oos)
            )
        if not payload["sizing_envelopes"]:
            warnings.append(
                "No segment survived contract truth, calibrated execution evidence, regime "
                "stability, selection-overfit control, statistical selection, and purged "
                "out-of-sample gates."
            )

    if args.full:
        payload["completed_trades"] = [asdict(item) for item in report.completed_trades]
        payload["legs"] = [asdict(item) for item in report.legs]
        payload["execution_attribution_legs"] = [asdict(item) for item in attribution_legs]
        payload["execution_meta_label_rows"] = [asdict(item) for item in execution_meta_labels]

    rendered = json.dumps(payload, indent=2, sort_keys=True, default=_json_default)
    if args.output is not None:
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)
