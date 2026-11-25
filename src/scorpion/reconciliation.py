from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from pathlib import Path

from .config import MARKET_TZ
from .domain import BookState, PositionStatus
from .execution_attribution import _nonnegative_money
from .execution_meta_labels import (
    PaperOutcome,
    PaperOutcomeAdmission,
    PaperOutcomeError,
    PaperOutcomeLabel,
    _bounded_bytes,
    _count,
    _fields,
    _hex,
    _parse_paper_fills,
    _price,
    _stamp,
    _text,
    _unique_json,
)
from .paper_equity import EquityPaperReport, _replay
from .provenance import stable_hash
from .session_calendar import session_hours
from .session_service import read_verified_paper_records


class ReconciliationSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class ExternalPositionObservation:
    contract_key: str
    quantity: int
    average_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    code: str
    severity: ReconciliationSeverity
    contract_key: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    findings: tuple[ReconciliationFinding, ...]

    @property
    def clean(self) -> bool:
        return not self.findings

    @property
    def critical(self) -> bool:
        return any(
            finding.severity is ReconciliationSeverity.CRITICAL for finding in self.findings
        )


def reconcile_positions(
    state: BookState,
    observations: Sequence[ExternalPositionObservation],
) -> ReconciliationReport:
    findings: list[ReconciliationFinding] = []
    external: dict[str, ExternalPositionObservation] = {}
    for observation in observations:
        if not observation.contract_key:
            findings.append(
                ReconciliationFinding(
                    "invalid_external_contract",
                    ReconciliationSeverity.CRITICAL,
                    "",
                    "external observation is missing contract_key",
                )
            )
            continue
        if observation.contract_key in external:
            findings.append(
                ReconciliationFinding(
                    "duplicate_external_observation",
                    ReconciliationSeverity.CRITICAL,
                    observation.contract_key,
                    "multiple observations supplied for the same contract",
                )
            )
            continue
        if observation.quantity < 0:
            findings.append(
                ReconciliationFinding(
                    "unsupported_external_short_position",
                    ReconciliationSeverity.CRITICAL,
                    observation.contract_key,
                    (
                        f"external quantity={observation.quantity}; "
                        "Scorpion execution state is long-only"
                    ),
                )
            )
        if (
            observation.quantity > 0
            and observation.average_price is not None
            and observation.average_price <= 0
        ):
            findings.append(
                ReconciliationFinding(
                    "invalid_external_average_price",
                    ReconciliationSeverity.CRITICAL,
                    observation.contract_key,
                    f"external average_price={observation.average_price}",
                )
            )
        external[observation.contract_key] = observation

    for contract_key, position in state.positions.items():
        observed = external.pop(contract_key, None)
        if position.quantity > 0:
            if observed is None or observed.quantity <= 0:
                findings.append(
                    ReconciliationFinding(
                        "missing_external_position",
                        ReconciliationSeverity.CRITICAL,
                        contract_key,
                        f"execution quantity={position.quantity}, external quantity=0",
                    )
                )
                continue
            if observed.quantity != position.quantity:
                findings.append(
                    ReconciliationFinding(
                        "quantity_mismatch",
                        ReconciliationSeverity.CRITICAL,
                        contract_key,
                        f"execution={position.quantity}, external={observed.quantity}",
                    )
                )
            if (
                observed.average_price is not None
                and position.average_price > 0
                and observed.average_price != position.average_price
            ):
                findings.append(
                    ReconciliationFinding(
                        "average_price_mismatch",
                        ReconciliationSeverity.WARNING,
                        contract_key,
                        f"execution={position.average_price}, external={observed.average_price}",
                    )
                )
        elif observed is not None and observed.quantity > 0:
            severity = (
                ReconciliationSeverity.WARNING
                if position.status is PositionStatus.PENDING_ENTRY
                else ReconciliationSeverity.CRITICAL
            )
            findings.append(
                ReconciliationFinding(
                    "external_position_not_reflected_in_state",
                    severity,
                    contract_key,
                    f"status={position.status.value}, external quantity={observed.quantity}",
                )
            )

    for contract_key, observed in external.items():
        if observed.quantity <= 0:
            continue
        findings.append(
            ReconciliationFinding(
                "unexpected_external_position",
                ReconciliationSeverity.CRITICAL,
                contract_key,
                f"external quantity={observed.quantity} has no deterministic source state",
            )
        )

    return ReconciliationReport(tuple(findings))


# Signed external-paper accounting. This is not the deterministic BookState comparator
# above: it compares actual attested venue activity against intent and account snapshots.
@dataclass(frozen=True, slots=True)
class PaperRoundTripCall:
    call_id: str
    ticker: str
    entry_quantity: int | None
    exit_quantity: int | None
    remaining_quantity: int | None
    entry_notional_usd: Decimal | None
    exit_notional_usd: Decimal | None
    net_fees_usd: Decimal | None
    net_round_trip_usd: Decimal | None
    modeled_entry_quantity: int | None
    modeled_exit_quantity: int | None
    modeled_net_round_trip_usd: Decimal | None
    venue_minus_model_net_usd: Decimal | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperRoundTripReport:
    journal_sha256: str
    outcome_report_sha256: str
    round_trip_export_sha256: str
    round_trip_source_sha256: str
    modeled_report_sha256: str
    account_ref: str
    session_date: date
    as_of_ts_utc: datetime
    calls: tuple[PaperRoundTripCall, ...]
    expected_closing_positions: tuple[tuple[str, int], ...]
    reported_closing_positions: tuple[tuple[str, int], ...]
    opening_cash_usd: Decimal
    closing_cash_usd: Decimal
    reconstructed_cash_usd: Decimal | None
    cash_difference_usd: Decimal | None
    matched_quantity_calls: int
    observed_net_round_trip_usd: Decimal | None
    blockers: tuple[str, ...]
    report_sha256: str
    authority: str = "RESEARCH_ONLY"
    evidence_kind: str = "DUAL_ATTESTED_EXTERNAL_PAPER"
    certifies_live_fills: bool = False
    authorizes_training: bool = False
    authorizes_promotion: bool = False
    resume_authorized: bool = False


@dataclass(frozen=True, slots=True)
class _Exit:
    call_id: str
    ticker: str
    intent_seq: int
    venue_order_id: str
    quantity: int
    state: str
    submitted: datetime
    terminal: datetime | None
    limit: Decimal | None
    fills: tuple[tuple[str, int, Decimal, datetime], ...]


def _parse_exit(value: object) -> _Exit:
    if not isinstance(value, dict):
        raise PaperOutcomeError("invalid_exit_order")
    _fields(value, {
        "call_id", "ticker", "intent_seq", "venue_order_id", "requested_quantity",
        "state", "submitted_ts_utc", "terminal_ts_utc", "limit", "fills",
        "asset_class", "side", "currency", "quantity_unit",
    })
    if (value["asset_class"], value["side"], value["currency"], value["quantity_unit"]) != (
        "EQUITY", "SELL", "USD", "SHARES",
    ):
        raise PaperOutcomeError("invalid_exit_scope")
    state = _text(value["state"])
    if state not in ("FILLED", "CANCELLED", "EXPIRED", "REJECTED", "WORKING", "PARTIAL", "UNKNOWN"):
        raise PaperOutcomeError("invalid_exit_state")
    return _Exit(
        _text(value["call_id"]), _text(value["ticker"]), _count(value["intent_seq"]),
        _text(value["venue_order_id"]), _count(value["requested_quantity"]), state,
        _stamp(value["submitted_ts_utc"]),
        None if value["terminal_ts_utc"] is None else _stamp(value["terminal_ts_utc"]),
        None if value["limit"] is None else _price(value["limit"]),
        _parse_paper_fills(value["fills"]),
    )


def _snapshot(value: object) -> dict[str, int]:
    if not isinstance(value, list):
        raise PaperOutcomeError("invalid_paper_position_snapshot")
    result: dict[str, int] = {}
    for row in value:
        if not isinstance(row, dict):
            raise PaperOutcomeError("invalid_paper_position_row")
        _fields(row, {"ticker", "quantity"})
        ticker, qty = _text(row["ticker"]), _count(row["quantity"])
        if ticker in result:
            raise PaperOutcomeError("duplicate_paper_position")
        result[ticker] = qty
    return result


def _fees(value: object) -> dict[str, Decimal]:
    if not isinstance(value, list):
        raise PaperOutcomeError("invalid_round_trip_fees")
    result: dict[str, Decimal] = {}
    for row in value:
        if not isinstance(row, dict):
            raise PaperOutcomeError("invalid_round_trip_fee")
        _fields(row, {"venue_order_id", "fees_usd", "rebates_usd"})
        key = _text(row["venue_order_id"])
        if key in result:
            raise PaperOutcomeError("duplicate_round_trip_fee")
        result[key] = _nonnegative_money(row["fees_usd"]) - _nonnegative_money(row["rebates_usd"])
    return result


def _check_exit(
    order: _Exit, intent: dict[str, object], entries: list[PaperOutcomeLabel],
    *, ticker: str, bought: int, session_close: datetime, through: datetime,
) -> tuple[int, Decimal] | None:
    intent_at = _stamp(intent.get("ts_utc"))
    scheduled = _stamp(intent.get("scheduled_exit_ts_utc"))
    deadline = min(scheduled + timedelta(seconds=60), session_close)
    if (
        order.ticker != ticker or order.intent_seq != intent.get("seq")
        or intent.get("status") != "PAPER_INTENT_ONLY"
        or intent.get("conditional_on") != "confirmed_entry_fill"
        or order.quantity != bought or order.quantity > _count(intent.get("quantity"))
        or not scheduled <= intent_at <= order.submitted < deadline
        or any(label.terminal_ts_utc is None or label.terminal_ts_utc > order.submitted
               for label in entries)
    ):
        raise PaperOutcomeError("exit_intent_or_inventory_binding_mismatch")
    terminal = order.state in ("FILLED", "CANCELLED", "EXPIRED", "REJECTED")
    if terminal != (order.terminal is not None):
        raise PaperOutcomeError("exit_terminal_state_conflict")
    if order.terminal is not None and not order.submitted <= order.terminal <= through:
        raise PaperOutcomeError("exit_terminal_clock_conflict")
    total = sum(fill[1] for fill in order.fills)
    if (
        total > order.quantity or (order.state == "FILLED") != (total == order.quantity)
        or (order.state in ("REJECTED", "WORKING") and total)
        or (order.state == "PARTIAL" and not total)
    ):
        raise PaperOutcomeError("exit_fill_state_conflict")
    for _, qty, px, when in order.fills:
        if (
            not order.submitted <= when <= (order.terminal or through)
            or qty <= 0 or (order.limit is not None and px < order.limit)
        ):
            raise PaperOutcomeError("exit_fill_clock_or_limit_conflict")
    if order.terminal is None or order.terminal > deadline or through < deadline:
        return None
    return total, sum((qty * px for _, qty, px, _ in order.fills), Decimal(0))


def _route_entries(
    rows: list[PaperOutcomeLabel], suppressed: set[str],
) -> tuple[list[PaperOutcomeLabel], tuple[str, ...]]:
    initial = [row for row in rows if row.role == "INITIAL"]
    chase = [row for row in rows if row.role == "CHASE"]
    if len(initial) != 1 or len(chase) > 1:
        raise PaperOutcomeError("round_trip_route_identity_conflict")
    first = initial[0]
    reasons: list[str] = []
    active = [first]
    if chase:
        other = chase[0]
        if other.feature_sha256 in suppressed:
            if (
                other.outcome is not PaperOutcome.UNOBSERVED or other.venue_order_id is not None
                or not first.filled_quantity or first.observed_fill_target is None
            ):
                raise PaperOutcomeError("unsupported_chase_suppression")
        else:
            active.append(other)
    if any(
        row.filled_quantity is None or row.filled_notional_usd is None
        or row.terminal_ts_utc is None or row.venue_order_id is None
        for row in active
    ):
        reasons.append("entry_outcome_incomplete")
    # An explicit rejection is a terminal zero for cash accounting only, not liquidity evidence.
    if any(row.outcome in (PaperOutcome.CENSORED, PaperOutcome.QUARANTINED,
                           PaperOutcome.UNOBSERVED) for row in active):
        reasons.append("entry_outcome_unqualified")
    return active, tuple(reasons)


def reconcile_paper_round_trips(
    admission: PaperOutcomeAdmission, *, supplement_path: Path, source_artifact_path: Path,
    collector_signature: str, reviewer_signature: str,
) -> PaperRoundTripReport:
    """Reverify Stage 1 and a separately signed complete-account activity supplement.

    Only a dedicated opening-flat USD equity paper account is supported. Exact
    fee/cash/inventory reconciliation is required; missing exits are never zero fills.
    External authenticity, independent key custody and the completeness declaration
    still require operator review. No external request, state mutation or training.
    """
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        return _reconcile_round_trips(
            admission, supplement_path=supplement_path, source_artifact_path=source_artifact_path,
            collector_signature=collector_signature, reviewer_signature=reviewer_signature,
        )


def _reconcile_round_trips(
    admission: PaperOutcomeAdmission, *, supplement_path: Path, source_artifact_path: Path,
    collector_signature: str, reviewer_signature: str,
) -> PaperRoundTripReport:
    outcome = admission.verify()
    receipt, records = read_verified_paper_records(admission.journal_path)
    if receipt["sha256"] != outcome.journal_sha256:
        raise PaperOutcomeError("journal_changed_during_round_trip")
    modeled: EquityPaperReport = _replay(receipt, records, recovered=False)
    raw = _bounded_bytes(supplement_path)
    admission.verify_round_trip_supplement(raw, collector_signature, reviewer_signature)
    data = _unique_json(raw)
    _fields(data, {
        "schema", "authority", "source_id", "account_ref", "trust_sha256", "journal_sha256",
        "outcome_export_sha256", "source_artifact_sha256", "session_date",
        "opening_ts_utc", "closing_ts_utc", "observed_through_ts_utc", "collected_ts_utc",
        "reviewed_ts_utc", "complete_account_activity", "no_nontrade_cash_movements",
        "opening_cash_usd", "closing_cash_usd", "opening_positions", "closing_positions",
        "suppressed_chase_feature_ids", "exit_orders", "fees",
    })
    artifact = _bounded_bytes(source_artifact_path)
    source_hash, export_hash = hashlib.sha256(artifact).hexdigest(), hashlib.sha256(raw).hexdigest()
    if (
        not artifact or source_hash in (
            outcome.source_artifact_sha256, outcome.export_sha256, outcome.journal_sha256,
            export_hash,
        )
        or data["schema"] != "scorpion.paper-round-trip.v1" or data["authority"] != "PAPER_ONLY"
        or data["source_id"] != outcome.source_id or data["account_ref"] != outcome.account_ref
        or data["trust_sha256"] != outcome.trust_sha256
        or data["journal_sha256"] != outcome.journal_sha256
        or data["outcome_export_sha256"] != outcome.export_sha256
        or data["source_artifact_sha256"] != source_hash
        or data["complete_account_activity"] is not True
        or data["no_nontrade_cash_movements"] is not True
    ):
        raise PaperOutcomeError("round_trip_scope_or_completeness_mismatch")
    day = date.fromisoformat(_text(data["session_date"]))
    hours = session_hours(day)
    opened, closed = _stamp(data["opening_ts_utc"]), _stamp(data["closing_ts_utc"])
    through = _stamp(data["observed_through_ts_utc"])
    collected, reviewed = _stamp(data["collected_ts_utc"]), _stamp(data["reviewed_ts_utc"])
    if (
        hours is None or not records or records[0].get("session_date") != day.isoformat()
        or opened.astimezone(MARKET_TZ).date() != day or closed.astimezone(MARKET_TZ).date() != day
        or not opened <= hours[0] < hours[1] <= closed <= through <= collected <= reviewed
        <= admission.as_of
        or outcome.observed_through_ts_utc is None or outcome.reviewed_ts_utc is None
        or outcome.observed_through_ts_utc < hours[1] or outcome.reviewed_ts_utc > admission.as_of
    ):
        raise PaperOutcomeError("round_trip_session_clock_mismatch")
    if _snapshot(data["opening_positions"]):
        raise PaperOutcomeError("paper_account_not_opening_flat")
    closing = _snapshot(data["closing_positions"])
    opening_cash = _nonnegative_money(data["opening_cash_usd"])
    closing_cash = _nonnegative_money(data["closing_cash_usd"])
    fees = _fees(data["fees"])
    raw_suppressed = data["suppressed_chase_feature_ids"]
    if not isinstance(raw_suppressed, list):
        raise PaperOutcomeError("invalid_suppressed_chase_list")
    suppressed = {_hex(value, 32).hex() for value in raw_suppressed}
    if len(suppressed) != len(raw_suppressed) or suppressed - {
        label.feature_sha256 for label in outcome.labels if label.role == "CHASE"
    }:
        raise PaperOutcomeError("invalid_suppressed_chase_identity")
    orders = data["exit_orders"]
    if not isinstance(orders, list):
        raise PaperOutcomeError("invalid_exit_orders")
    seen_orders = {label.venue_order_id for label in outcome.labels if label.venue_order_id}
    seen_fills = {identity for label in outcome.labels for identity in label.venue_fill_ids}
    exits: dict[str, _Exit] = {}
    for value in orders:
        order = _parse_exit(value)
        if order.call_id in exits or order.venue_order_id in seen_orders:
            raise PaperOutcomeError("duplicate_round_trip_order")
        exits[order.call_id] = order
        seen_orders.add(order.venue_order_id)
        for identity, *_ in order.fills:
            if identity in seen_fills:
                raise PaperOutcomeError("duplicate_round_trip_fill")
            seen_fills.add(identity)
    intents: dict[str, dict[str, object]] = {}
    for row in records:
        if row.get("kind") != "conditional_exit_intent":
            continue
        key = _text(row.get("call_id"))
        if key in intents:
            raise PaperOutcomeError("duplicate_conditional_exit")
        intents[key] = row
    grouped: dict[str, list[PaperOutcomeLabel]] = {}
    for label in outcome.labels:
        grouped.setdefault(label.call_id, []).append(label)
    if set(exits) - set(grouped) or set(fees) - seen_orders:
        raise PaperOutcomeError("unexpected_exit_or_fee_identity")
    blockers: set[str] = set()
    if outcome.extra_feature_ids or outcome.feature_refusals:
        blockers.add("entry_evidence_conflict")
    if records[-1].get("kind") != "session_end" or _stamp(records[-1].get("ts_utc")) > through:
        blockers.add("session_lifecycle_unsealed")
    modeled_by_call = {row.call_id: row for row in modeled.calls}
    rows: list[PaperRoundTripCall] = []
    positions: dict[str, int] = {}
    cash = opening_cash
    cash_complete = not blockers
    matched = 0
    for key, labels in sorted(grouped.items()):
        active, entry_reasons = _route_entries(labels, suppressed)
        reasons = list(entry_reasons)
        first = active[0]
        ticker = first.ticker
        if any(row.ticker != ticker for row in active):
            raise PaperOutcomeError("round_trip_ticker_conflict")
        qty: int | None = None
        entry_cash: Decimal | None = None
        if not reasons:
            qty = sum(row.filled_quantity or 0 for row in active)
            entry_cash = sum((row.filled_notional_usd or Decimal(0) for row in active), Decimal(0))
            if qty > first.requested_quantity:
                raise PaperOutcomeError("round_trip_parent_overfill")
        sold: int | None = None
        proceeds: Decimal | None = None
        exit_order = exits.get(key)
        if exit_order is not None:
            if qty is None:
                reasons.append("exit_inventory_unsubstantiated")
            elif not qty or key not in intents:
                raise PaperOutcomeError("unexpected_exit_without_confirmed_inventory")
            else:
                result = _check_exit(
                    exit_order, intents[key], active, ticker=ticker, bought=qty,
                    session_close=hours[1], through=through,
                )
                if result is None:
                    reasons.append("exit_outcome_censored")
                else:
                    sold, proceeds = result
        elif qty == 0:
            sold, proceeds = 0, Decimal(0)
        else:
            reasons.append("external_exit_missing")
        fee_ids = [row.venue_order_id for row in active if row.venue_order_id is not None]
        if exit_order is not None:
            fee_ids.append(exit_order.venue_order_id)
        fee = sum((fees[identity] for identity in fee_ids), Decimal(0)) if (
            len(fee_ids) == len(active) + int(exit_order is not None)
            and all(identity in fees for identity in fee_ids)
        ) else None
        if fee is None:
            reasons.append("explicit_fees_missing")
        remaining = qty - sold if qty is not None and sold is not None else None
        if remaining is not None and remaining < 0:
            raise PaperOutcomeError("negative_round_trip_inventory")
        if remaining:
            positions[ticker] = positions.get(ticker, 0) + remaining
            reasons.append("open_external_inventory")
        net = None
        if entry_cash is not None and proceeds is not None and fee is not None:
            cash += proceeds - entry_cash - fee
            if remaining == 0:
                net = proceeds - entry_cash - fee
        else:
            cash_complete = False
        model = modeled_by_call.get(key)
        comparable = (
            model is not None and not modeled.blockers and qty == model.bought
            and sold == model.sold and net is not None and qty is not None and qty > 0
            and model.modeled_net_round_trip_usd is not None
        )
        if comparable:
            matched += 1
        delta = (
            net - model.modeled_net_round_trip_usd
            if comparable and net is not None and model is not None
            and model.modeled_net_round_trip_usd is not None else None
        )
        blockers.update(reasons)
        rows.append(PaperRoundTripCall(
            key, ticker, qty, sold, remaining, entry_cash, proceeds, fee, net,
            model.bought if model else None, model.sold if model else None,
            model.modeled_net_round_trip_usd if model else None, delta, tuple(reasons),
        ))
    if not grouped:
        blockers.add("no_observed_entry_routes")
    if not cash_complete:
        blockers.add("account_activity_unresolved")
    elif cash != closing_cash:
        blockers.add("cash_reconciliation_mismatch")
    if not cash_complete or positions != closing:
        blockers.add("position_reconciliation_unresolved_or_mismatch")
    if closing:
        blockers.add("account_not_closing_flat")
    net_total = sum((row.net_round_trip_usd or Decimal(0) for row in rows), Decimal(0)) if (
        not blockers and any(row.entry_quantity for row in rows)
    ) else None
    material = {
        "schema": "paper-round-trip-report-v1", "journal": outcome.journal_sha256,
        "outcome": outcome.report_sha256, "export": export_hash, "artifact": source_hash,
        "model": modeled.report_sha256, "as_of": admission.as_of, "calls": rows,
        "expected_positions": positions, "reported_positions": closing,
        "opening_cash": opening_cash, "closing_cash": closing_cash,
        "reconstructed_cash": cash if cash_complete else None,
        "blockers": sorted(blockers),
    }
    return PaperRoundTripReport(
        outcome.journal_sha256, outcome.report_sha256, export_hash, source_hash,
        modeled.report_sha256, outcome.account_ref, day, admission.as_of, tuple(rows),
        tuple(sorted(positions.items())), tuple(sorted(closing.items())), opening_cash,
        closing_cash, cash if cash_complete else None, closing_cash - cash if cash_complete else None,
        matched, net_total, tuple(sorted(blockers)), stable_hash(material),
    )
