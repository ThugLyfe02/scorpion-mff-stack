"""Offline worker coverage and paired fill-model diagnostics; never an execution policy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

from .delivery import PaperDeliveryLedger
from .execution_meta_labels import PaperOutcomeAdmission
from .paper_equity import replay_equity_market_journal
from .paper_venue import load_paper_risk_policy
from .paper_worker import load_worker_authorization
from .provenance import stable_hash
from .session_service import read_verified_paper_records


@dataclass(frozen=True, slots=True)
class WorkerCalibrationRow:
    intent_seq: int
    role: str
    disposition: str
    observed_quantity: int | None
    modeled_quantity: int | None
    requested_quantity: int
    signed_fraction_error: Decimal | None
    reason: str


@dataclass(frozen=True, slots=True)
class WorkerCalibrationReport:
    source_sha256: str
    worker_audit_sha256: str
    outcome_report_sha256: str
    model_report_sha256: str
    ledger_head: str
    session_date: str
    reviewed_ts_utc: datetime | None
    rows: tuple[WorkerCalibrationRow, ...]
    candidates: int
    durable_dispatches: int
    qualified_observed_entries: int
    comparable_entries: int
    mean_signed_fill_fraction_error: Decimal | None
    mean_absolute_fill_fraction_error: Decimal | None
    blockers: tuple[str, ...]
    report_sha256: str
    authority: str = "RESEARCH_ONLY"
    estimand: str = "EXTERNAL_PAPER_MINUS_FIXED_TAPE_MODELED_FILL_FRACTION"
    authorizes_training: bool = False
    authorizes_promotion: bool = False
    authorizes_live_orders: bool = False


def _coverage(
    source: tuple[dict[str, object], ...], audit: tuple[dict[str, object], ...],
) -> tuple[dict[int, dict[str, object]], dict[int, dict[str, object]]]:
    candidates: dict[int, dict[str, object]] = {}
    for row in source:
        if row.get("kind") in ("intent", "conditional_exit_intent"):
            seq = row.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq in candidates:
                raise ValueError("worker source sequence identity")
            candidates[seq] = row
    dispositions: dict[int, dict[str, object]] = {}
    for row in audit:
        if row.get("kind") != "worker_disposition":
            continue
        seq = row.get("intent_seq")
        if (
            not isinstance(seq, int) or isinstance(seq, bool)
            or seq not in candidates or seq in dispositions
        ):
            raise ValueError("worker disposition roster mismatch")
        original = candidates[seq]
        role = "EXIT" if original.get("kind") == "conditional_exit_intent" else (
            "CHASE" if original.get("chase") is True else "INITIAL"
        )
        if (
            row.get("source_record_sha256") != stable_hash(original)
            or row.get("call_id") != original.get("call_id") or row.get("role") != role
            or row.get("disposition") not in ("DURABLE_DISPATCH", "NOT_DISPATCHED")
        ):
            raise ValueError("worker disposition identity mismatch")
        dispositions[seq] = row
    if set(dispositions) != set(candidates):
        raise ValueError("worker disposition coverage incomplete")
    return candidates, dispositions


def calibrate_paper_worker(
    admission: PaperOutcomeAdmission, *, audit_path: Path, authorization_path: Path,
    authorization_pin: str, risk_path: Path, ledger_path: Path, final_head: str,
) -> WorkerCalibrationReport:
    """Reverify original signed inputs and compare matched, actually observed routes.

    This pilot requires the independent trust account_ref to equal the pinned
    paper account ID. final_head must be retained outside the worker.
    """
    source_receipt, source = read_verified_paper_records(admission.journal_path)
    audit_receipt, audit = read_verified_paper_records(audit_path)
    authorization = load_worker_authorization(authorization_path, authorization_pin)
    policy = load_paper_risk_policy(risk_path, authorization.risk_pin)
    status = PaperDeliveryLedger(ledger_path, policy).status(expected_head=final_head)
    signed = admission.verify()
    if signed.account_ref != policy.account_id or signed.journal_sha256 != source_receipt["sha256"]:
        raise ValueError("worker calibration account or journal mismatch")
    if (
        not source or source[0].get("kind") != "session_start"
        or source[-1].get("kind") != "session_end"
        or source[0].get("session_date") != policy.session_date.isoformat()
        or not audit or audit[0].get("kind") != "worker_start"
        or audit[-1].get("kind") != "worker_shutdown_final"
        or audit[0].get("authorization_sha256") != authorization.digest
        or audit[0].get("policy_sha256") != policy.digest
        or audit[0].get("ledger_head") != authorization.opening_head
        or audit[-1].get("ledger_head") != final_head
        or any(row.get("authority") != "RESEARCH_ONLY" for row in audit)
    ):
        raise ValueError("worker calibration lifecycle or pin mismatch")
    candidates, dispositions = _coverage(source, audit)
    orders = {order.request.intent_seq: order for order in status.orders}
    if len(orders) != len(status.orders) or set(orders) != {
        seq for seq, row in dispositions.items() if row["disposition"] == "DURABLE_DISPATCH"
    }:
        raise ValueError("worker audit and delivery ledger disagree")
    labels = {label.intent_seq: label for label in signed.labels}
    if len(labels) != len(signed.labels) or set(labels) != {
        seq for seq, row in dispositions.items() if row["role"] != "EXIT"
    }:
        raise ValueError("signed entry label roster mismatch")
    modeled = replay_equity_market_journal(admission.journal_path)
    if modeled.journal_sha256 != signed.journal_sha256:
        raise ValueError("journal changed during calibration")
    calls = {call.call_id: call for call in modeled.calls}
    fills: dict[int, int] = {}
    for fill in modeled.modeled_fills:
        fills[fill.intent_seq] = fills.get(fill.intent_seq, 0) + fill.quantity
    blockers: set[str] = set(modeled.blockers) - {
        "no_modeled_fills", "unresolved_modeled_inventory",
        "initial_window_unobserved", "chase_window_unobserved",
    }
    if (
        audit[-1].get("blockers") != [] or status.expected_positions
        or any(not (order.observation and order.observation.terminal) for order in status.orders)
        or any(row.get("kind") == "worker_fault" for row in audit)
    ):
        blockers.add("worker_not_cleanly_reconciled")
    if signed.feature_refusals or signed.extra_feature_ids:
        blockers.add("signed_outcome_coverage_refused")

    rows: list[WorkerCalibrationRow] = []
    observed_entries = 0
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        for seq, source_row in sorted(candidates.items()):
            row = dispositions[seq]
            role, state = str(row["role"]), str(row["disposition"])
            quantity = source_row.get("quantity")
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                raise ValueError("worker candidate quantity invalid")
            order = orders.get(seq)
            if order and (order.request.call_id, order.request.role) != (
                source_row.get("call_id"), role,
            ):
                raise ValueError("worker order identity mismatch")
            label = labels.get(seq)
            observed: int | None = None
            estimate: int | None = None
            error: Decimal | None = None
            reason = "exit_requires_signed_round_trip_evidence"
            if label is not None:
                if label.requested_quantity != quantity:
                    raise ValueError("signed requested quantity mismatch")
                if order is None:
                    if label.venue_order_id is not None:
                        raise ValueError("signed external order lacks worker dispatch")
                    reason = "not_dispatched_not_a_zero_fill"
                elif (
                    order.observation is None or label.venue_order_id is None
                    or label.observed_fill_target is None
                ):
                    reason = "external_fill_not_qualified"
                    blockers.add("dispatched_entry_outcome_unknown")
                else:
                    if (
                        label.venue_order_id != order.observation.venue_id
                        or label.observed_fill_target != order.filled
                        or label.average_fill_price != order.observation.average
                        or label.ticker != order.request.symbol
                    ):
                        raise ValueError("signed fill disagrees with delivery ledger")
                    observed = label.observed_fill_target
                    observed_entries += 1
                    call = calls.get(label.call_id)
                    if call and (
                        (role == "INITIAL" and call.initial_observations > 0)
                        or (role == "CHASE" and call.chase_observations > 0
                            and quantity <= call.initial_target)
                    ):
                        estimate = fills.get(seq, 0)
                        if not 0 <= estimate <= quantity:
                            raise ValueError("modeled quantity outside requested range")
                        error = Decimal(observed - estimate) / Decimal(quantity)
                        reason = "paired_external_paper_vs_model"
                    else:
                        reason = "modeled_route_unobserved_or_not_selected"
                        blockers.add("modeled_route_not_comparable")
            rows.append(WorkerCalibrationRow(
                seq, role, state, observed, estimate, quantity, error, reason,
            ))
        errors = [row.signed_fraction_error for row in rows if row.signed_fraction_error is not None]
        if not errors:
            blockers.add("no_comparable_entries")
        mean = sum(errors, Decimal(0)) / len(errors) if errors else None
        mae = sum(map(abs, errors), Decimal(0)) / len(errors) if errors else None
    material = (
        "worker-calibration-v1", source_receipt["sha256"], audit_receipt["sha256"],
        signed.report_sha256, modeled.report_sha256, final_head, rows, observed_entries,
        signed.reviewed_ts_utc, policy.session_date, mean, mae, sorted(blockers),
    )
    return WorkerCalibrationReport(
        str(source_receipt["sha256"]), str(audit_receipt["sha256"]),
        signed.report_sha256, modeled.report_sha256, final_head, policy.session_date.isoformat(),
        signed.reviewed_ts_utc, tuple(rows), len(candidates), len(orders), observed_entries,
        len(errors), mean, mae, tuple(sorted(blockers)), stable_hash(material),
    )
