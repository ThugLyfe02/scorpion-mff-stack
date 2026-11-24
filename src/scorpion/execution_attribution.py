from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path
from typing import TYPE_CHECKING

from .causal_features import PaperIntentFeatureRow, load_paper_intent_features
from .config import MARKET_TZ
from .domain import EventKind
from .microstructure import OptionMicrostructureTape, ns_from_datetime
from .microstructure_forensics import MicroForensicLeg, MicrostructureForensicsReport
from .provenance import stable_hash
from .session_calendar import CalendarNotCovered, session_hours

if TYPE_CHECKING:
    from .execution_meta_labels import PaperOutcomeAdmission, PaperOutcomeLabel

_BPS = Decimal("10000")
_DEFAULT_HORIZONS_MS = (100, 500, 1000, 5000)


@dataclass(frozen=True, slots=True)
class MarkoutPoint:
    horizon_ms: int
    signed_markout_bps: float | None
    midpoint: Decimal | None


@dataclass(frozen=True, slots=True)
class ExecutionAttributionLeg:
    event_id: str
    contract_key: str
    side: str
    quantity: int
    source_to_decision_ms: float
    decision_to_arrival_ms: float
    decision_quote_age_ms: float | None
    decision_spread_bps: float | None
    slippage_vs_decision_mid_bps: float | None
    markouts: tuple[MarkoutPoint, ...]


@dataclass(frozen=True, slots=True)
class MarkoutAggregate:
    horizon_ms: int
    samples: int
    mean_signed_markout_bps: float
    adverse_selection_rate: float


@dataclass(frozen=True, slots=True)
class ExecutionAttributionReport:
    filled_legs: int
    attributed_legs: int
    mean_source_to_decision_ms: float
    mean_decision_to_arrival_ms: float
    mean_decision_quote_age_ms: float
    mean_decision_spread_bps: float
    mean_slippage_vs_decision_mid_bps: float
    markouts: tuple[MarkoutAggregate, ...]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _side(kind: EventKind) -> str | None:
    if kind in {EventKind.ENTRY, EventKind.ADD}:
        return "BUY"
    if kind in {EventKind.TRIM, EventKind.EXIT}:
        return "SELL"
    return None


def _midpoint(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / Decimal("2")


def _signed_bps(numerator: Decimal, denominator: Decimal) -> float:
    if denominator <= 0:
        return 0.0
    return float((numerator / denominator) * _BPS)


def attribute_execution_leg(
    leg: MicroForensicLeg,
    tape: OptionMicrostructureTape,
    *,
    horizons_ms: tuple[int, ...] = _DEFAULT_HORIZONS_MS,
    future_quote_max_age: timedelta = timedelta(seconds=1),
) -> ExecutionAttributionLeg | None:
    side = _side(leg.event_kind)
    if (
        side is None
        or leg.contract_key is None
        or leg.fill_price is None
        or leg.conservative_fill_quantity <= 0
        or leg.decision_ts_utc is None
        or leg.order_arrival_ts_utc is None
        or leg.fill_evidence_event_ns is None
    ):
        return None
    if any(horizon <= 0 for horizon in horizons_ms):
        raise ValueError("markout horizons must be positive")

    source_to_decision_ms = (
        leg.decision_ts_utc - leg.source_ts_utc
    ).total_seconds() * 1000.0
    decision_to_arrival_ms = (
        leg.order_arrival_ts_utc - leg.decision_ts_utc
    ).total_seconds() * 1000.0
    decision_ns = ns_from_datetime(leg.decision_ts_utc)
    quote_age_ms = (
        (decision_ns - leg.decision_quote_recv_ns) / 1_000_000.0
        if leg.decision_quote_recv_ns is not None
        else None
    )
    decision_mid = _midpoint(leg.decision_quote_bid, leg.decision_quote_ask)
    spread_bps: float | None = None
    slippage_bps: float | None = None
    if (
        decision_mid is not None
        and leg.decision_quote_bid is not None
        and leg.decision_quote_ask is not None
    ):
        spread_bps = _signed_bps(
            leg.decision_quote_ask - leg.decision_quote_bid,
            decision_mid,
        )
        slippage_numerator = (
            leg.fill_price - decision_mid
            if side == "BUY"
            else decision_mid - leg.fill_price
        )
        slippage_bps = _signed_bps(slippage_numerator, decision_mid)

    markouts: list[MarkoutPoint] = []
    for horizon_ms in horizons_ms:
        target_ns = leg.fill_evidence_event_ns + horizon_ms * 1_000_000
        quote = tape.market_quote_at(
            leg.contract_key,
            target_ns,
            max_age=future_quote_max_age,
        )
        midpoint = _midpoint(
            quote.bid if quote is not None else None,
            quote.ask if quote is not None else None,
        )
        if midpoint is None:
            signed = None
        else:
            move = (
                midpoint - leg.fill_price
                if side == "BUY"
                else leg.fill_price - midpoint
            )
            signed = _signed_bps(move, leg.fill_price)
        markouts.append(MarkoutPoint(horizon_ms, signed, midpoint))

    return ExecutionAttributionLeg(
        event_id=leg.event_id,
        contract_key=leg.contract_key,
        side=side,
        quantity=leg.conservative_fill_quantity,
        source_to_decision_ms=source_to_decision_ms,
        decision_to_arrival_ms=decision_to_arrival_ms,
        decision_quote_age_ms=quote_age_ms,
        decision_spread_bps=spread_bps,
        slippage_vs_decision_mid_bps=slippage_bps,
        markouts=tuple(markouts),
    )


def build_execution_attribution_report(
    report: MicrostructureForensicsReport,
    tape: OptionMicrostructureTape,
    *,
    horizons_ms: tuple[int, ...] = _DEFAULT_HORIZONS_MS,
    future_quote_max_age: timedelta = timedelta(seconds=1),
) -> tuple[ExecutionAttributionReport, tuple[ExecutionAttributionLeg, ...]]:
    attributed = tuple(
        item
        for leg in report.legs
        if (
            item := attribute_execution_leg(
                leg,
                tape,
                horizons_ms=horizons_ms,
                future_quote_max_age=future_quote_max_age,
            )
        )
        is not None
    )
    filled_legs = sum(leg.conservative_fill_quantity > 0 for leg in report.legs)
    source_latencies = [item.source_to_decision_ms for item in attributed]
    order_latencies = [item.decision_to_arrival_ms for item in attributed]
    quote_ages = [
        item.decision_quote_age_ms
        for item in attributed
        if item.decision_quote_age_ms is not None
    ]
    spreads = [
        item.decision_spread_bps
        for item in attributed
        if item.decision_spread_bps is not None
    ]
    slippages = [
        item.slippage_vs_decision_mid_bps
        for item in attributed
        if item.slippage_vs_decision_mid_bps is not None
    ]

    aggregates: list[MarkoutAggregate] = []
    for horizon_ms in horizons_ms:
        values = [
            point.signed_markout_bps
            for item in attributed
            for point in item.markouts
            if point.horizon_ms == horizon_ms and point.signed_markout_bps is not None
        ]
        aggregates.append(
            MarkoutAggregate(
                horizon_ms=horizon_ms,
                samples=len(values),
                mean_signed_markout_bps=_mean(values),
                adverse_selection_rate=(
                    sum(value < 0 for value in values) / len(values) if values else 0.0
                ),
            )
        )

    return (
        ExecutionAttributionReport(
            filled_legs=filled_legs,
            attributed_legs=len(attributed),
            mean_source_to_decision_ms=_mean(source_latencies),
            mean_decision_to_arrival_ms=_mean(order_latencies),
            mean_decision_quote_age_ms=_mean(quote_ages),
            mean_decision_spread_bps=_mean(spreads),
            mean_slippage_vs_decision_mid_bps=_mean(slippages),
            markouts=tuple(aggregates),
        ),
        attributed,
    )


# Forward equity paper implementation shortfall. Historical options markout attribution
# above is unchanged. This class is deliberately not a policy-value or P&L label.
@dataclass(frozen=True, slots=True)
class PaperCostPolicy:
    registered_ts_utc: datetime
    horizon_seconds: int
    max_mark_age_ms: int
    market_source_id: str


@dataclass(frozen=True, slots=True)
class PaperOrderCost:
    feature_sha256: str
    label_sha256: str
    call_id: str
    role: str
    venue_order_id: str | None
    filled_quantity: int | None
    filled_notional_usd: Decimal | None
    leg_midpoint: Decimal
    gross_slippage_usd: Decimal | None
    fees_usd: Decimal | None
    rebates_usd: Decimal | None
    net_filled_cost_usd: Decimal | None
    reason: str
    venue_fill_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PaperRouteCost:
    call_id: str
    ticker: str
    initial_feature_sha256: str
    initial_label_sha256: str
    chase_label_sha256: str | None
    target_quantity: int
    decision_midpoint: Decimal
    horizon_ts_utc: datetime
    horizon_midpoint: Decimal | None
    mark_quote_id: str | None
    filled_quantity: int | None
    initial_filled_quantity: int | None
    chase_filled_quantity: int | None
    filled_notional_usd: Decimal | None
    timing_cost_usd: Decimal | None
    within_leg_slippage_usd: Decimal | None
    net_fees_usd: Decimal | None
    opportunity_cost_usd: Decimal | None
    net_shortfall_usd: Decimal | None
    net_shortfall_bps: Decimal | None
    initial_net_fees_usd: Decimal | None
    chase_net_fees_usd: Decimal | None
    chase_notional_usd: Decimal | None
    available_ts_utc: datetime | None
    reason: str
    chase_order_observed: bool = False
    initial_ts_utc: datetime | None = None
    source_ts_utc: datetime | None = None
    initial_spread_bps: Decimal | None = None
    initial_quote_age_ms: Decimal | None = None
    initial_signal_age_ms: Decimal | None = None
    revision_id: str = ""

    @property
    def complete(self) -> bool:
        return self.net_shortfall_usd is not None


@dataclass(frozen=True, slots=True)
class PaperSessionCoverage:
    session_date: date
    covered_from_ts_utc: datetime
    covered_through_ts_utc: datetime
    expected_initial_routes: int
    journal_complete: bool
    ingress_complete: bool
    no_feed_gaps: bool
    valid: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperExecutionCostReport:
    journal_sha256: str
    outcome_export_sha256: str
    outcome_report_sha256: str
    trust_sha256: str
    cost_policy_sha256: str
    cost_export_sha256: str
    market_artifact_sha256: str
    market_source_id: str
    as_of_ts_utc: datetime
    orders: tuple[PaperOrderCost, ...]
    routes: tuple[PaperRouteCost, ...]
    expected_routes: int
    complete_routes: int
    complete_route_net_shortfall_usd: Decimal | None
    complete_route_notional_weighted_bps: Decimal | None
    blockers: tuple[str, ...]
    report_sha256: str
    authority: str = "RESEARCH_ONLY"
    certifies_live_fills: bool = False
    authorizes_training: bool = False
    authorizes_promotion: bool = False
    source_id: str = ""
    account_ref: str = ""
    source_artifact_sha256: str = ""
    session_coverage: PaperSessionCoverage | None = None


@dataclass(frozen=True, slots=True)
class _Fee:
    feature_hash: str
    venue_order_id: str
    fees: Decimal
    rebates: Decimal


@dataclass(frozen=True, slots=True)
class _Mark:
    call_id: str
    initial_feature_hash: str
    ticker: str
    quote_id: str
    target: datetime
    event: datetime
    received: datetime
    bid: Decimal
    ask: Decimal
    route_closed: bool


def _nonnegative_money(value: object) -> Decimal:
    from .execution_meta_labels import PaperOutcomeError

    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,8})?", value):
        raise PaperOutcomeError("invalid_cost_money")
    return Decimal(value)


def _load_cost_policy(raw: bytes, expected_hash: str, trust_hash: str) -> PaperCostPolicy:
    from .execution_meta_labels import (
        PaperOutcomeError,
        _count,
        _fields,
        _hex,
        _stamp,
        _text,
        _unique_json,
    )

    _hex(expected_hash, 32)
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise PaperOutcomeError("cost_policy_pin_mismatch")
    data = _unique_json(raw)
    _fields(data, {
        "schema", "authority", "trust_sha256", "registered_ts_utc", "horizon_seconds",
        "max_mark_age_ms", "market_source_id", "benchmark", "fee_scope",
    })
    if (
        data["schema"] != "scorpion.paper-cost-policy.v1"
        or data["authority"] != "RESEARCH_ONLY" or data["trust_sha256"] != trust_hash
        or data["benchmark"] != "INITIAL_DECISION_MID"
        or data["fee_scope"] != "ALL_ORDER_CHARGES_AND_REBATES_USD"
    ):
        raise PaperOutcomeError("cost_policy_scope_mismatch")
    horizon, age = _count(data["horizon_seconds"]), _count(data["max_mark_age_ms"])
    if horizon > 86400 or age > 3000:
        raise PaperOutcomeError("cost_policy_time_bound")
    return PaperCostPolicy(
        _stamp(data["registered_ts_utc"]), horizon, age, _text(data["market_source_id"]),
    )


def _parse_cost_rows(
    data: dict[str, object],
) -> tuple[dict[str, _Fee], dict[str, _Mark]]:
    from .execution_meta_labels import PaperOutcomeError, _fields, _hex, _price, _stamp, _text

    fee_values, mark_values = data["fees"], data["marks"]
    if not isinstance(fee_values, list) or not isinstance(mark_values, list):
        raise PaperOutcomeError("invalid_cost_rows")
    fees: dict[str, _Fee] = {}
    fee_order_ids: set[str] = set()
    for row in fee_values:
        if not isinstance(row, dict):
            raise PaperOutcomeError("invalid_fee_row")
        _fields(row, {"feature_sha256", "venue_order_id", "fees_usd", "rebates_usd"})
        fee = _Fee(
            _hex(row["feature_sha256"], 32).hex(), _text(row["venue_order_id"]),
            _nonnegative_money(row["fees_usd"]), _nonnegative_money(row["rebates_usd"]),
        )
        if fee.feature_hash in fees or fee.venue_order_id in fee_order_ids:
            raise PaperOutcomeError("duplicate_cost_fee_identity")
        fees[fee.feature_hash] = fee
        fee_order_ids.add(fee.venue_order_id)
    marks: dict[str, _Mark] = {}
    quote_ids: dict[str, tuple[str, datetime, datetime, Decimal, Decimal]] = {}
    for row in mark_values:
        if not isinstance(row, dict):
            raise PaperOutcomeError("invalid_mark_row")
        _fields(row, {
            "call_id", "initial_feature_sha256", "ticker", "quote_id", "target_ts_utc",
            "event_ts_utc", "received_ts_utc", "bid", "ask", "route_closed",
        })
        if type(row["route_closed"]) is not bool:
            raise PaperOutcomeError("invalid_route_closure")
        mark = _Mark(
            _text(row["call_id"]), _hex(row["initial_feature_sha256"], 32).hex(),
            _text(row["ticker"]), _text(row["quote_id"]), _stamp(row["target_ts_utc"]),
            _stamp(row["event_ts_utc"]), _stamp(row["received_ts_utc"]),
            _price(row["bid"]), _price(row["ask"]), row["route_closed"] is True,
        )
        if mark.call_id in marks:
            raise PaperOutcomeError("duplicate_cost_route_identity")
        quote_payload = (mark.ticker, mark.event, mark.received, mark.bid, mark.ask)
        prior = quote_ids.get(mark.quote_id)
        if prior is not None and prior != quote_payload:
            raise PaperOutcomeError("conflicting_cost_quote_identity")
        quote_ids[mark.quote_id] = quote_payload
        marks[mark.call_id] = mark
    return fees, marks


def _feature_mid(feature: PaperIntentFeatureRow) -> Decimal:
    bid = Decimal(str(feature.features["bid"]))
    ask = Decimal(str(feature.features["ask"]))
    mid = _midpoint(bid, ask)
    if mid is None or ask < bid:
        raise ValueError("invalid sealed benchmark")
    return mid


def _order_cost(
    feature: PaperIntentFeatureRow, label: PaperOutcomeLabel, fee: _Fee | None,
) -> PaperOrderCost:
    mid = _feature_mid(feature)
    qty = label.observed_fill_target
    notional = label.filled_notional_usd if qty is not None else None
    gross = notional - qty * mid if notional is not None and qty is not None else None
    valid_fee = fee is not None and fee.venue_order_id == label.venue_order_id
    fee_value = fee.fees if valid_fee and fee is not None else None
    rebate = fee.rebates if valid_fee and fee is not None else None
    net = (
        gross + fee_value - rebate
        if gross is not None and fee_value is not None and rebate is not None else None
    )
    return PaperOrderCost(
        feature.feature_sha256, label.label_sha256, feature.call_id, feature.role,
        label.venue_order_id, qty, notional, mid, gross, fee_value, rebate, net,
        "OBSERVED" if net is not None else (
            "OUTCOME_UNAVAILABLE" if qty is None else "FEE_UNAVAILABLE_OR_MISMATCHED"
        ),
        venue_fill_ids=label.venue_fill_ids,
    )


def _route_cost(
    initial: PaperIntentFeatureRow, initial_label: PaperOutcomeLabel,
    initial_cost: PaperOrderCost, chase_label: PaperOutcomeLabel | None,
    chase_cost: PaperOrderCost | None, mark: _Mark | None, policy: PaperCostPolicy,
    through: datetime, outcome_through: datetime, available: datetime,
    *, globally_blocked: bool,
) -> PaperRouteCost:
    from .execution_meta_labels import PaperOutcome, _stamp

    benchmark = initial_cost.leg_midpoint
    target = initial_label.requested_quantity
    initial_time = _stamp(initial.features["intent_ts_utc"])
    deadline = initial_time + timedelta(seconds=policy.horizon_seconds)
    source_time = _stamp(initial.features["source_ts_utc"])
    spread = Decimal(str(initial.features["spread_bps"]))
    quote_age = Decimal(str(initial.features["quote_age_ns"])) / 1_000_000
    signal_age = Decimal(str(initial.features["source_age_ms"]))
    revision = str(initial.features["revision_id"])

    def withheld(reason: str) -> PaperRouteCost:
        return PaperRouteCost(
            call_id=initial.call_id, ticker=initial.ticker,
            initial_feature_sha256=initial.feature_sha256,
            initial_label_sha256=initial_label.label_sha256,
            chase_label_sha256=chase_label.label_sha256 if chase_label else None,
            target_quantity=target, decision_midpoint=benchmark, horizon_ts_utc=deadline,
            horizon_midpoint=None, mark_quote_id=None, filled_quantity=None,
            initial_filled_quantity=None, chase_filled_quantity=None,
            filled_notional_usd=None, timing_cost_usd=None, within_leg_slippage_usd=None,
            net_fees_usd=None, opportunity_cost_usd=None, net_shortfall_usd=None,
            net_shortfall_bps=None, initial_net_fees_usd=None, chase_net_fees_usd=None,
            chase_notional_usd=None, available_ts_utc=None, reason=reason,
            initial_ts_utc=initial_time, source_ts_utc=source_time, initial_spread_bps=spread,
            initial_quote_age_ms=quote_age, initial_signal_age_ms=signal_age, revision_id=revision,
        )

    if globally_blocked:
        return withheld("EXPORT_OR_FEATURE_COVERAGE_CONFLICT")
    if policy.registered_ts_utc > _stamp(initial.features["source_ts_utc"]):
        return withheld("POLICY_REGISTERED_AFTER_SIGNAL")
    try:
        hours = session_hours(initial_time.astimezone(MARKET_TZ).date())
    except CalendarNotCovered:
        return withheld("MARKET_CALENDAR_UNAVAILABLE")
    if hours is None or not hours[0] <= initial_time <= deadline < hours[1]:
        return withheld("HORIZON_OUTSIDE_REGULAR_SESSION")
    if (
        mark is None or not mark.route_closed or mark.initial_feature_hash != initial.feature_sha256
        or mark.ticker != initial.ticker or mark.target != deadline
    ):
        return withheld("ROUTE_CLOSURE_OR_MARK_UNAVAILABLE")
    if through < deadline or outcome_through < deadline:
        return withheld("ROUTE_HORIZON_INCOMPLETE")
    if (
        not initial_time <= mark.event <= mark.received <= deadline
        or deadline - mark.event > timedelta(milliseconds=policy.max_mark_age_ms)
        or mark.ask < mark.bid
    ):
        return withheld("NONCAUSAL_OR_STALE_HORIZON_MARK")
    if (
        initial_cost.net_filled_cost_usd is None or initial_label.terminal_ts_utc is None
        or initial_label.terminal_ts_utc > deadline
    ):
        return withheld("INITIAL_OUTCOME_OR_FEES_UNAVAILABLE")
    active = [(initial_label, initial_cost)]
    # A proposed chase may never be submitted after a positive initial fill. A missing
    # chase after a zero-fill initial is not equivalent to a certified no-chase route.
    if chase_label is not None:
        if chase_label.venue_order_id is None:
            if initial_label.observed_fill_target == 0:
                return withheld("CHASE_ROUTE_UNOBSERVED")
        else:
            if (
                initial_label.outcome not in (
                    PaperOutcome.CANCELLED_NO_FILL, PaperOutcome.EXPIRED_NO_FILL,
                )
                or chase_cost is None or chase_cost.net_filled_cost_usd is None
                or chase_label.terminal_ts_utc is None or chase_label.terminal_ts_utc > deadline
            ):
                return withheld("CHASE_OUTCOME_OR_FEES_UNAVAILABLE")
            active.append((chase_label, chase_cost))
    # Preserve the original parent target even if the chase proposes fewer shares.
    qty = 0
    notional = timing = within = fees = Decimal(0)
    initial_net_fees = chase_net_fees = chase_notional = Decimal(0)
    initial_qty = chase_qty = 0
    chase_observed = False
    for label, cost in active:
        q, n = cost.filled_quantity, cost.filled_notional_usd
        gross, charge, rebate = cost.gross_slippage_usd, cost.fees_usd, cost.rebates_usd
        if q is None or n is None or gross is None or charge is None or rebate is None:
            return withheld("ORDER_COST_INCOMPLETE")
        qty += q
        notional += n
        timing += q * (cost.leg_midpoint - benchmark)
        within += gross
        fees += charge - rebate
        if label.role == "INITIAL":
            initial_qty, initial_net_fees = q, charge - rebate
        else:
            chase_qty, chase_net_fees, chase_notional = q, charge - rebate, n
            chase_observed = True
    if qty > target:
        return withheld("PARENT_TARGET_OVERFILL")
    horizon_mid = (mark.bid + mark.ask) / 2
    opportunity = (target - qty) * (horizon_mid - benchmark)
    shortfall = timing + within + fees + opportunity
    # This identity also catches any later double counting in the decomposition.
    direct = notional - qty * benchmark + fees + opportunity
    if shortfall != direct:
        raise ValueError("paper shortfall decomposition mismatch")
    return PaperRouteCost(
        initial.call_id, initial.ticker, initial.feature_sha256, initial_label.label_sha256,
        chase_label.label_sha256 if chase_label else None, target, benchmark, deadline,
        horizon_mid, mark.quote_id, qty, initial_qty, chase_qty, notional, timing, within,
        fees, opportunity, shortfall, shortfall / (target * benchmark) * _BPS,
        initial_net_fees, chase_net_fees, chase_notional, available, "OBSERVED_PAPER_ROUTE",
        chase_order_observed=chase_observed,
        initial_ts_utc=initial_time, source_ts_utc=source_time, initial_spread_bps=spread,
        initial_quote_age_ms=quote_age, initial_signal_age_ms=signal_age, revision_id=revision,
    )


def _session_coverage(
    raw: object, *, routes: tuple[PaperRouteCost, ...], through: datetime,
    outcome_through: datetime, journal_path: Path, journal_sha256: str,
) -> PaperSessionCoverage:
    """Verify a signed coverage declaration against the sealed local journal.

    Completeness of the external feed is attested by the signers, not inferred
    merely from a clean file receipt. A missing declaration never means no activity.
    """
    from .execution_meta_labels import PaperOutcomeError, _fields, _stamp, _text
    from .session_service import read_verified_paper_records

    if not isinstance(raw, dict):
        raise PaperOutcomeError("invalid_session_coverage")
    _fields(raw, {
        "session_date", "covered_from_ts_utc", "covered_through_ts_utc",
        "expected_initial_routes", "journal_complete", "ingress_complete", "no_feed_gaps",
    })
    day = date.fromisoformat(_text(raw["session_date"]))
    start, end = _stamp(raw["covered_from_ts_utc"]), _stamp(raw["covered_through_ts_utc"])
    count = raw["expected_initial_routes"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise PaperOutcomeError("invalid_coverage_route_count")
    if any(type(raw[name]) is not bool for name in (
        "journal_complete", "ingress_complete", "no_feed_gaps",
    )):
        raise PaperOutcomeError("invalid_coverage_flags")
    reasons: list[str] = []
    try:
        hours = session_hours(day)
    except CalendarNotCovered:
        hours = None
    if hours is None or not start <= hours[0] < hours[1] <= end <= min(through, outcome_through):
        reasons.append("regular_session_coverage_incomplete")
    if start.astimezone(MARKET_TZ).date() != day or end.astimezone(MARKET_TZ).date() != day:
        reasons.append("coverage_session_date_conflict")
    if not all(raw[name] is True for name in (
        "journal_complete", "ingress_complete", "no_feed_gaps",
    )):
        reasons.append("coverage_not_attested_complete")
    receipt, records = read_verified_paper_records(journal_path)
    if receipt["sha256"] != journal_sha256:
        raise PaperOutcomeError("journal_changed_during_coverage_verification")
    initial_records = [
        row for row in records if row.get("kind") == "intent" and row.get("chase") is False
    ]
    if count != len(initial_records) or count != len(routes):
        reasons.append("coverage_route_count_mismatch")
    if any(
        route.initial_ts_utc is None
        or route.initial_ts_utc.astimezone(MARKET_TZ).date() != day
        or hours is None or not hours[0] <= route.initial_ts_utc < hours[1]
        for route in routes
    ):
        reasons.append("coverage_route_session_conflict")
    return PaperSessionCoverage(
        day, start, end, count, raw["journal_complete"] is True,
        raw["ingress_complete"] is True, raw["no_feed_gaps"] is True,
        not reasons, tuple(reasons),
    )


def build_paper_execution_cost_report(
    admission: PaperOutcomeAdmission, *, cost_policy_path: Path, expected_policy_sha256: str,
    cost_export_path: Path, market_artifact_path: Path,
    collector_signature: str, reviewer_signature: str,
) -> PaperExecutionCostReport:
    """Re-verify Stage 1 and signed cost evidence; never accept caller-fabricated labels."""
    from .execution_meta_labels import (
        PaperOutcomeError,
        _bounded_bytes,
        _fields,
        _stamp,
        _unique_json,
    )

    # Stage 1 feature/label identity uses its original canonical 28-digit precision.
    # The money decomposition below has a separate, fixed high-precision context.
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        outcome = admission.verify()
        evidence = load_paper_intent_features(admission.journal_path)
        if evidence.journal_sha256 != outcome.journal_sha256:
            raise PaperOutcomeError("journal_changed_during_cost_admission")
        policy = _load_cost_policy(
            _bounded_bytes(cost_policy_path, 65536), expected_policy_sha256,
            outcome.trust_sha256,
        )
        raw = _bounded_bytes(cost_export_path)
        admission.verify_cost_supplement(raw, collector_signature, reviewer_signature)
        data = _unique_json(raw)
        expected_fields = {
            "schema", "authority", "trust_sha256", "cost_policy_sha256", "journal_sha256",
            "outcome_export_sha256", "market_source_id", "market_artifact_sha256",
            "complete_fee_export", "observed_through_ts_utc", "collected_ts_utc",
            "reviewed_ts_utc", "fees", "marks",
        }
        if "session_coverage" in data:
            expected_fields.add("session_coverage")
        _fields(data, expected_fields)
        artifact = _bounded_bytes(market_artifact_path)
        market_hash = hashlib.sha256(artifact).hexdigest()
        export_hash = hashlib.sha256(raw).hexdigest()
        if (
            not artifact or market_hash in (
                outcome.journal_sha256, outcome.export_sha256, outcome.source_artifact_sha256,
                export_hash,
            )
            or data["schema"] != "scorpion.paper-cost-export.v1"
            or data["authority"] != "PAPER_ONLY"
            or data["trust_sha256"] != outcome.trust_sha256
            or data["cost_policy_sha256"] != expected_policy_sha256
            or data["journal_sha256"] != outcome.journal_sha256
            or data["outcome_export_sha256"] != outcome.export_sha256
            or data["market_source_id"] != policy.market_source_id
            or policy.market_source_id == outcome.source_id
            or data["market_artifact_sha256"] != market_hash
            or data["complete_fee_export"] is not True
        ):
            raise PaperOutcomeError("cost_export_scope_mismatch")
        through = _stamp(data["observed_through_ts_utc"])
        collected = _stamp(data["collected_ts_utc"])
        reviewed = _stamp(data["reviewed_ts_utc"])
        as_of = admission.as_of.astimezone(UTC)
        if (
            not policy.registered_ts_utc <= through <= collected <= reviewed <= as_of
            or outcome.observed_through_ts_utc is None or outcome.reviewed_ts_utc is None
        ):
            raise PaperOutcomeError("cost_export_availability_mismatch")
        fees, marks = _parse_cost_rows(data)
        labels = {label.feature_sha256: label for label in outcome.labels}
        by_role = {(row.call_id, row.role): row for row in evidence.rows}
        if set(labels) != {row.feature_sha256 for row in evidence.rows}:
            raise PaperOutcomeError("cost_feature_identity_mismatch")
        blockers: list[str] = []
        if evidence.refusals or outcome.extra_feature_ids:
            blockers.append("outcome_or_feature_coverage_conflict")
        if any(
            key not in labels or fee.venue_order_id != labels[key].venue_order_id
            for key, fee in fees.items()
        ):
            blockers.append("unexpected_or_mismatched_fee")
        initials = {key for key, role in by_role if role == "INITIAL"}
        if marks.keys() - initials:
            blockers.append("unexpected_route_mark")
        order_costs = {
            row.feature_sha256: _order_cost(
                row, labels[row.feature_sha256], fees.get(row.feature_sha256),
            )
            for row in evidence.rows
        }
        routes: list[PaperRouteCost] = []
        for key in sorted(initials):
            first = by_role[(key, "INITIAL")]
            second = by_role.get((key, "CHASE"))
            routes.append(_route_cost(
                first, labels[first.feature_sha256], order_costs[first.feature_sha256],
                labels[second.feature_sha256] if second else None,
                order_costs[second.feature_sha256] if second else None,
                marks.get(key), policy, through, outcome.observed_through_ts_utc,
                max(reviewed, outcome.reviewed_ts_utc), globally_blocked=bool(blockers),
            ))
        complete = [route for route in routes if route.net_shortfall_usd is not None]
        if len(complete) != len(routes):
            blockers.append("incomplete_route_cost_coverage")
        if not complete:
            blockers.append("no_complete_routes")
        total = sum(
            (route.net_shortfall_usd for route in complete if route.net_shortfall_usd is not None),
            Decimal(0),
        ) if complete else None
        denominator = sum((row.target_quantity * row.decision_midpoint for row in complete),
                          Decimal(0))
        bps = total / denominator * _BPS if total is not None and denominator else None
        order_rows = tuple(order_costs[row.feature_sha256] for row in evidence.rows)
        coverage = (
            _session_coverage(
                data["session_coverage"], routes=tuple(routes), through=through,
                outcome_through=outcome.observed_through_ts_utc,
                journal_path=admission.journal_path, journal_sha256=outcome.journal_sha256,
            ) if "session_coverage" in data else None
        )
        material = {
            "version": "paper-execution-cost-report-v1", "journal_sha256": outcome.journal_sha256,
            "outcome_report_sha256": outcome.report_sha256, "policy_sha256": expected_policy_sha256,
            "cost_export_sha256": export_hash, "market_artifact_sha256": market_hash,
            "as_of": as_of, "orders": order_rows, "routes": tuple(routes), "blockers": blockers,
            "session_coverage": coverage,
        }
        return PaperExecutionCostReport(
            outcome.journal_sha256, outcome.export_sha256, outcome.report_sha256,
            outcome.trust_sha256, expected_policy_sha256, export_hash, market_hash,
            policy.market_source_id, as_of, order_rows, tuple(routes), len(initials),
            len(complete), total, bps, tuple(blockers), stable_hash(material),
            source_id=outcome.source_id, account_ref=outcome.account_ref,
            source_artifact_sha256=outcome.source_artifact_sha256, session_coverage=coverage,
        )
