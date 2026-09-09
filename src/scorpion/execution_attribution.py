from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .domain import EventKind
from .microstructure import OptionMicrostructureTape, ns_from_datetime, timedelta_to_ns
from .microstructure_forensics import MicroForensicLeg, MicrostructureForensicsReport

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
