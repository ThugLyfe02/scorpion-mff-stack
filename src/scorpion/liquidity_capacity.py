from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from .domain import EventKind
from .execution_forensics import CompletedTrade
from .microstructure import OptionMarketEvent, OptionMicrostructureTape
from .microstructure_forensics import MicroForensicLeg, MicrostructureForensicsReport


class CapacityStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class LiquidityCapacityPolicy:
    clip_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0, 3.0)
    displayed_depth_haircuts: tuple[float, ...] = (0.25, 0.50, 0.75, 1.0)
    minimum_supported_trades: int = 20
    minimum_lifecycle_coverage: float = 0.80
    impact_spread_multiplier: float = 0.75
    impact_participation_exponent: float = 0.50
    maximum_participation_rate: float = 0.85
    maximum_mean_impact_fraction: float = 0.15
    minimum_stressed_mean_return: float = 0.0
    minimum_stressed_positive_trade_ratio: float = 0.50

    def __post_init__(self) -> None:
        if not self.clip_multipliers or any(value <= 0 for value in self.clip_multipliers):
            raise ValueError("clip multipliers must be positive")
        if any(value <= 0 or value > 1 for value in self.displayed_depth_haircuts):
            raise ValueError("depth haircuts must be in (0,1]")
        if self.minimum_supported_trades <= 0:
            raise ValueError("minimum_supported_trades must be positive")
        if not 0 <= self.minimum_lifecycle_coverage <= 1:
            raise ValueError("minimum_lifecycle_coverage must be in [0,1]")
        if self.impact_spread_multiplier < 0:
            raise ValueError("impact_spread_multiplier cannot be negative")
        if not 0 < self.impact_participation_exponent <= 2:
            raise ValueError("impact_participation_exponent must be in (0,2]")
        if not 0 < self.maximum_participation_rate <= 1:
            raise ValueError("maximum_participation_rate must be in (0,1]")
        if self.maximum_mean_impact_fraction < 0:
            raise ValueError("maximum_mean_impact_fraction cannot be negative")
        if not 0 <= self.minimum_stressed_positive_trade_ratio <= 1:
            raise ValueError("minimum_stressed_positive_trade_ratio must be in [0,1]")


@dataclass(frozen=True, slots=True)
class CapacityScenario:
    clip_multiplier: float
    depth_haircut: float
    total_trades: int
    supported_trades: int
    lifecycle_coverage: float
    mean_return: float
    status: CapacityStatus
    stressed_mean_return: float = 0.0
    stressed_positive_trade_ratio: float = 0.0
    mean_impact_fraction: float = 0.0
    max_participation_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class CapacityFrontierPoint:
    clip_multiplier: float
    worst_depth_haircut: float
    worst_lifecycle_coverage: float
    minimum_supported_trades: int
    worst_mean_return: float
    robust: bool
    worst_stressed_mean_return: float = 0.0
    worst_stressed_positive_trade_ratio: float = 0.0
    worst_mean_impact_fraction: float = 0.0
    worst_max_participation_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class LiquidityCapacityReport:
    scenarios: tuple[CapacityScenario, ...]
    frontier: tuple[CapacityFrontierPoint, ...]
    max_robust_clip_multiplier: float
    robust: bool


@dataclass(frozen=True, slots=True)
class _TradeCapacityStress:
    supported: bool
    stressed_return: float
    impact_fraction: float
    max_participation_rate: float


def _quote_for_leg(
    leg: MicroForensicLeg,
    tape: OptionMicrostructureTape,
) -> OptionMarketEvent | None:
    if leg.contract_key is None or leg.fill_evidence_event_ns is None:
        return None
    return tape.market_quote_at(
        leg.contract_key,
        leg.fill_evidence_event_ns,
        max_age=timedelta(seconds=1),
    )


def _side_depth(leg: MicroForensicLeg, quote: OptionMarketEvent) -> int | None:
    if leg.event_kind in {EventKind.ENTRY, EventKind.ADD}:
        return quote.ask_size
    if leg.event_kind in {EventKind.TRIM, EventKind.EXIT}:
        return quote.bid_size
    return None


def _trade_legs(
    trade: CompletedTrade,
    legs: tuple[MicroForensicLeg, ...],
) -> tuple[MicroForensicLeg, ...]:
    return tuple(
        leg
        for leg in legs
        if leg.contract_key == trade.contract_key
        and trade.opened_ts_utc <= leg.source_ts_utc <= trade.closed_ts_utc
        and leg.event_kind in {EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT}
        and leg.requested_quantity > 0
        and leg.conservative_fill_quantity > 0
    )


def _stress_trade(
    trade: CompletedTrade,
    legs: tuple[MicroForensicLeg, ...],
    tape: OptionMicrostructureTape,
    *,
    clip_multiplier: float,
    depth_haircut: float,
    policy: LiquidityCapacityPolicy,
) -> _TradeCapacityStress:
    lifecycle_legs = _trade_legs(trade, legs)
    if not lifecycle_legs:
        return _TradeCapacityStress(False, 0.0, 0.0, 0.0)

    impact_price_units = 0.0
    entry_price_units = 0.0
    maximum_participation = 0.0
    for leg in lifecycle_legs:
        quote = _quote_for_leg(leg, tape)
        if quote is None or quote.bid is None or quote.ask is None or quote.ask < quote.bid:
            return _TradeCapacityStress(False, 0.0, 0.0, maximum_participation)
        depth = _side_depth(leg, quote)
        if depth is None:
            return _TradeCapacityStress(False, 0.0, 0.0, maximum_participation)
        usable_depth = math.floor(depth * depth_haircut)
        required = math.ceil(leg.requested_quantity * clip_multiplier)
        if usable_depth <= 0 or usable_depth < required:
            return _TradeCapacityStress(False, 0.0, 0.0, maximum_participation)
        participation = required / usable_depth
        maximum_participation = max(maximum_participation, participation)
        if participation > policy.maximum_participation_rate:
            return _TradeCapacityStress(False, 0.0, 0.0, maximum_participation)

        spread = float(quote.ask - quote.bid)
        impact_price = (
            spread
            * policy.impact_spread_multiplier
            * participation**policy.impact_participation_exponent
        )
        impact_price_units += impact_price * required
        if leg.event_kind in {EventKind.ENTRY, EventKind.ADD}:
            entry_price_units += float(quote.ask) * required

    if entry_price_units <= 0:
        return _TradeCapacityStress(False, 0.0, 0.0, maximum_participation)
    impact_fraction = impact_price_units / entry_price_units
    stressed_return = float(trade.return_fraction) - impact_fraction
    return _TradeCapacityStress(
        True,
        stressed_return,
        impact_fraction,
        maximum_participation,
    )


def evaluate_liquidity_capacity(
    report: MicrostructureForensicsReport,
    tape: OptionMicrostructureTape,
    *,
    policy: LiquidityCapacityPolicy | None = None,
) -> LiquidityCapacityReport:
    """Estimate the clip frontier after depth, participation, and adverse price impact.

    Historical return is not reused unchanged at larger clip sizes. Every supported lifecycle
    receives an adverse incremental impact penalty derived from quoted spread and participation
    in haircutted displayed depth. The model is intentionally conservative and is a research
    gate only; it does not determine live size.
    """
    policy = policy or LiquidityCapacityPolicy()
    trades = tuple(
        trade for trade in report.completed_trades if trade.depth_evidence_complete
    )
    scenarios: list[CapacityScenario] = []
    for multiplier in policy.clip_multipliers:
        for haircut in policy.displayed_depth_haircuts:
            supported: list[tuple[CompletedTrade, _TradeCapacityStress]] = []
            for trade in trades:
                stress = _stress_trade(
                    trade,
                    report.legs,
                    tape,
                    clip_multiplier=multiplier,
                    depth_haircut=haircut,
                    policy=policy,
                )
                if stress.supported:
                    supported.append((trade, stress))

            coverage = len(supported) / len(trades) if trades else 0.0
            raw_returns = [float(trade.return_fraction) for trade, _ in supported]
            stressed_returns = [stress.stressed_return for _, stress in supported]
            impact_fractions = [stress.impact_fraction for _, stress in supported]
            participations = [stress.max_participation_rate for _, stress in supported]
            mean_return = statistics.fmean(raw_returns) if raw_returns else 0.0
            stressed_mean = (
                statistics.fmean(stressed_returns) if stressed_returns else 0.0
            )
            positive_ratio = (
                sum(value > 0.0 for value in stressed_returns) / len(stressed_returns)
                if stressed_returns
                else 0.0
            )
            mean_impact = (
                statistics.fmean(impact_fractions) if impact_fractions else 0.0
            )
            max_participation = max(participations, default=0.0)

            if len(trades) < policy.minimum_supported_trades:
                status = CapacityStatus.INSUFFICIENT
            elif (
                len(supported) < policy.minimum_supported_trades
                or coverage < policy.minimum_lifecycle_coverage
                or stressed_mean <= policy.minimum_stressed_mean_return
                or positive_ratio < policy.minimum_stressed_positive_trade_ratio
                or mean_impact > policy.maximum_mean_impact_fraction
            ):
                status = CapacityStatus.FAIL
            else:
                status = CapacityStatus.PASS
            scenarios.append(
                CapacityScenario(
                    clip_multiplier=multiplier,
                    depth_haircut=haircut,
                    total_trades=len(trades),
                    supported_trades=len(supported),
                    lifecycle_coverage=coverage,
                    mean_return=mean_return,
                    status=status,
                    stressed_mean_return=stressed_mean,
                    stressed_positive_trade_ratio=positive_ratio,
                    mean_impact_fraction=mean_impact,
                    max_participation_rate=max_participation,
                )
            )

    frontier: list[CapacityFrontierPoint] = []
    robust_multipliers: list[float] = []
    for multiplier in policy.clip_multipliers:
        relevant = [item for item in scenarios if item.clip_multiplier == multiplier]
        if not relevant:
            continue
        worst = min(relevant, key=lambda item: (item.lifecycle_coverage, item.supported_trades))
        minimum_supported = min(item.supported_trades for item in relevant)
        worst_mean = min(item.mean_return for item in relevant)
        worst_stressed_mean = min(item.stressed_mean_return for item in relevant)
        worst_positive_ratio = min(item.stressed_positive_trade_ratio for item in relevant)
        worst_mean_impact = max(item.mean_impact_fraction for item in relevant)
        worst_participation = max(item.max_participation_rate for item in relevant)
        robust = all(item.status is CapacityStatus.PASS for item in relevant)
        if robust:
            robust_multipliers.append(multiplier)
        frontier.append(
            CapacityFrontierPoint(
                clip_multiplier=multiplier,
                worst_depth_haircut=worst.depth_haircut,
                worst_lifecycle_coverage=worst.lifecycle_coverage,
                minimum_supported_trades=minimum_supported,
                worst_mean_return=worst_mean,
                robust=robust,
                worst_stressed_mean_return=worst_stressed_mean,
                worst_stressed_positive_trade_ratio=worst_positive_ratio,
                worst_mean_impact_fraction=worst_mean_impact,
                worst_max_participation_rate=worst_participation,
            )
        )
    maximum = max(robust_multipliers, default=0.0)
    return LiquidityCapacityReport(
        scenarios=tuple(scenarios),
        frontier=tuple(frontier),
        max_robust_clip_multiplier=maximum,
        robust=maximum > 0,
    )
