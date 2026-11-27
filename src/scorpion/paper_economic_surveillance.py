"""Independently signed round-trip paper economics for fixed-cohort surveillance."""
from __future__ import annotations

import hashlib
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

from .execution_attribution import PaperExecutionCostReport, PaperOrderCost, _nonnegative_money
from .execution_meta_labels import (
    PaperOutcomeAdmission,
    _bounded_bytes,
    _fields,
    _text,
    _unique_json,
)
from .paper_session_statistics import VerifiedPaperCohort, _day, _input_rows, _read_signature
from .reconciliation import _parse_exit, reconcile_paper_round_trips
from .session_service import JournalError


def compare_fee_evidence(
    orders: tuple[PaperOrderCost, ...], round_trip_fees: object,
) -> tuple[str, ...]:
    """Check gross charges and rebates separately, never just their net.

    A signed fee statement with identical net cost can still contradict another
    signed statement. Missing fee evidence does not imply zero charges.
    """
    if not isinstance(round_trip_fees, list):
        raise ValueError("round trip fee roster")
    by_id: dict[str, tuple[Decimal, Decimal]] = {}
    for row in round_trip_fees:
        if not isinstance(row, dict):
            raise ValueError("round trip fee row")
        _fields(row, {"venue_order_id", "fees_usd", "rebates_usd"})
        identity = _text(row["venue_order_id"])
        if identity in by_id:
            raise ValueError("round trip duplicate fee")
        by_id[identity] = (
            _nonnegative_money(row["fees_usd"]), _nonnegative_money(row["rebates_usd"]),
        )
    blockers: set[str] = set()
    for order in orders:
        if order.venue_order_id is None:
            continue
        expected = by_id.get(order.venue_order_id)
        if expected is None or order.fees_usd is None or order.rebates_usd is None:
            blockers.add("cross_report_entry_fee_unavailable")
        elif expected != (order.fees_usd, order.rebates_usd):
            blockers.add("cross_report_entry_fee_conflict")
    return tuple(sorted(blockers))


def economic_intervals(
    cohort: VerifiedPaperCohort, *, cohort_inputs: Path, trust_path: Path,
    index_path: Path | None, clip: float,
    prior_costs: tuple[tuple[date, PaperExecutionCostReport], ...] = (),
) -> tuple[tuple[date, tuple[float, float], str], ...]:
    """Use net account cash change only for complete, flat, signed sessions.

    Missing evidence is full support. Cross-session entry/exit order and fill
    identities cannot be reused. The endpoint is clipped paper account return,
    not benchmark-adjusted alpha or a live-market performance estimate.
    """
    elapsed = tuple(row.session_date for row in cohort.report.sessions)
    rows: dict[date, dict[str, Path]] = {}
    if index_path is not None:
        index = _unique_json(_bounded_bytes(index_path, 1024*1024))
        _fields(index, {"schema", "sessions"})
        if (
            index["schema"] != "scorpion.paper-economic-inputs.v1"
            or not isinstance(index["sessions"], list) or len(index["sessions"]) > 252
        ):
            raise ValueError("economic index schema")
        for row in index["sessions"]:
            if not isinstance(row, dict):
                raise ValueError("economic input row")
            fields = {"export", "artifact", "collector_signature", "reviewer_signature"}
            _fields(row, fields | {"session_date"})
            day = _day(row["session_date"])
            if day not in elapsed or day in rows:
                raise ValueError("economic session roster conflict")
            rows[day] = {key: index_path.parent / _text(row[key]) for key in fields}

    cost_inputs = _input_rows(cohort_inputs, elapsed)
    costs = dict(cohort.costs)
    all_costs = (*prior_costs, *cohort.costs)
    seen_orders = {order.venue_order_id for _, cost in all_costs for order in cost.orders
                   if order.venue_order_id is not None}
    seen_fills = {fill for _, cost in all_costs for order in cost.orders
                  for fill in order.venue_fill_ids}
    seen_artifacts = {identity for _, cost in all_costs for identity in (
        cost.journal_sha256, cost.outcome_export_sha256, cost.cost_export_sha256,
        cost.source_artifact_sha256, cost.market_artifact_sha256,
    )}
    results: list[tuple[date, tuple[float, float], str]] = []
    for day, stat in zip(elapsed, cohort.report.sessions, strict=True):
        if day not in rows or day not in costs or day not in cost_inputs:
            results.append((day, (-clip, clip), "MISSING_ROUND_TRIP"))
            continue
        source, bundle, cost = cost_inputs[day], rows[day], costs[day]
        try:
            admission = PaperOutcomeAdmission(
                source["journal"], trust_path, cohort.policy.trust_sha256,
                source["outcome_export"], source["source_artifact"],
                _read_signature(source["collector_signature"]),
                _read_signature(source["reviewer_signature"]), cohort.report.as_of_ts_utc,
            )
            report = reconcile_paper_round_trips(
                admission, supplement_path=bundle["export"],
                source_artifact_path=bundle["artifact"],
                collector_signature=_read_signature(bundle["collector_signature"]),
                reviewer_signature=_read_signature(bundle["reviewer_signature"]),
            )
            raw = _bounded_bytes(bundle["export"])
            if hashlib.sha256(raw).hexdigest() != report.round_trip_export_sha256:
                raise ValueError("economic export changed")
            parsed = _unique_json(raw)
            exits = parsed["exit_orders"]
            fee_blockers = compare_fee_evidence(cost.orders, parsed["fees"])
            if not isinstance(exits, list):
                raise ValueError("invalid exit roster")
            exit_orders = tuple(_parse_exit(row) for row in exits)
        except (OSError, ValueError, ImportError, JournalError):
            results.append((day, (-clip, clip), "INVALID_ROUND_TRIP"))
            continue
        if (
            report.journal_sha256 != cost.journal_sha256
            or report.outcome_report_sha256 != cost.outcome_report_sha256
            or report.account_ref != cost.account_ref or report.session_date != day
        ):
            raise ValueError("economic account or evidence identity mismatch")
        for digest in (report.round_trip_export_sha256, report.round_trip_source_sha256):
            if digest in seen_artifacts:
                raise ValueError("economic artifact reuse")
            seen_artifacts.add(digest)
        for order in exit_orders:
            if order.venue_order_id in seen_orders:
                raise ValueError("economic order reuse")
            seen_orders.add(order.venue_order_id)
            for fill_id, *_ in order.fills:
                if fill_id in seen_fills:
                    raise ValueError("economic fill reuse")
                seen_fills.add(fill_id)
        if fee_blockers:
            results.append((day, (-clip, clip), "INCONSISTENT_OR_MISSING_ENTRY_FEES"))
            continue
        if (
            stat.status not in ("COVERED_COMPLETE", "COVERED_EMPTY")
            or {call.call_id for call in report.calls} != {route.call_id for route in cost.routes}
            or set(report.blockers) - {"no_observed_filled_entry_routes"}
            or not report.inventory_complete or report.reported_closing_positions
            or report.cash_difference_usd != 0 or report.opening_cash_usd <= 0
        ):
            results.append((day, (-clip, clip), "UNRESOLVED_ROUND_TRIP"))
            continue
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            value = float(
                (report.closing_cash_usd-report.opening_cash_usd)
                / report.opening_cash_usd * Decimal(10_000)
            )
        value = max(-clip, min(clip, value))
        results.append((day, (value, value), report.report_sha256))
    return tuple(results)
