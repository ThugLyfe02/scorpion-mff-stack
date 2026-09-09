from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from .execution_forensics import CompletedTrade

_MARKET_TZ = ZoneInfo("America/New_York")


class PortfolioClusterStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class PortfolioClusterPolicy:
    minimum_days: int = 20
    maximum_same_ticker_premium_share: float = 0.65
    maximum_single_day_premium_share: float = 0.20
    maximum_daily_drawdown: float = 0.35
    maximum_loss_day_rate: float = 0.60
    bootstrap_trials: int = 3000
    bootstrap_block_days: int = 5
    bootstrap_horizon_days: int = 100
    maximum_research_risk_fraction: float = 0.10
    risk_step: float = 0.0025
    ruin_floor_fraction: float = 0.50
    maximum_ruin_probability: float = 0.01
    maximum_drawdown_breach_probability: float = 0.05
    drawdown_breach_level: float = 0.25
    random_seed: int = 90421

    def __post_init__(self) -> None:
        if self.minimum_days <= 0:
            raise ValueError("minimum_days must be positive")
        for name in (
            "maximum_same_ticker_premium_share",
            "maximum_single_day_premium_share",
            "maximum_daily_drawdown",
            "maximum_loss_day_rate",
            "maximum_research_risk_fraction",
            "risk_step",
            "ruin_floor_fraction",
            "maximum_ruin_probability",
            "maximum_drawdown_breach_probability",
            "drawdown_breach_level",
        ):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0,1]")
        if self.risk_step > self.maximum_research_risk_fraction:
            raise ValueError("risk_step cannot exceed maximum_research_risk_fraction")
        if self.bootstrap_trials < 100:
            raise ValueError("bootstrap_trials must be >=100")
        if self.bootstrap_block_days <= 0 or self.bootstrap_horizon_days <= 0:
            raise ValueError("bootstrap block/horizon must be positive")


@dataclass(frozen=True, slots=True)
class DailyPortfolioObservation:
    market_date: date
    trades: int
    gross_premium_in: Decimal
    pnl: Decimal
    return_fraction: Decimal
    max_concurrency: int
    max_same_ticker_premium_share: float


@dataclass(frozen=True, slots=True)
class PortfolioRiskSimulation:
    risk_fraction: float
    ruin_probability: float
    drawdown_breach_probability: float
    median_terminal_equity: float
    p05_terminal_equity: float
    median_max_drawdown: float


@dataclass(frozen=True, slots=True)
class PortfolioClusterReport:
    days: int
    trades: int
    max_concurrency: int
    max_same_ticker_premium_share: float
    max_single_day_premium_share: float
    mean_daily_return: float
    daily_cvar_10: float
    max_daily_path_drawdown: float
    loss_day_rate: float
    status: PortfolioClusterStatus
    failures: tuple[str, ...]
    max_research_risk_fraction: float
    selected_simulation: PortfolioRiskSimulation | None
    simulations: tuple[PortfolioRiskSimulation, ...]

    @property
    def passed(self) -> bool:
        return self.status is PortfolioClusterStatus.PASS


def _ticker(contract_key: str) -> str:
    return contract_key.split("|", 1)[0].upper()


def _max_concurrency(trades: list[CompletedTrade]) -> int:
    points: list[tuple[object, int]] = []
    for trade in trades:
        points.append((trade.opened_ts_utc, 1))
        points.append((trade.closed_ts_utc, -1))
    active = 0
    maximum = 0
    for _, delta in sorted(points, key=lambda item: (item[0], item[1])):
        active += delta
        maximum = max(maximum, active)
    return maximum


def build_daily_portfolio(
    trades: tuple[CompletedTrade, ...],
) -> tuple[DailyPortfolioObservation, ...]:
    grouped: dict[date, list[CompletedTrade]] = {}
    for trade in trades:
        market_day = trade.opened_ts_utc.astimezone(_MARKET_TZ).date()
        grouped.setdefault(market_day, []).append(trade)

    observations: list[DailyPortfolioObservation] = []
    for market_day in sorted(grouped):
        day_trades = grouped[market_day]
        premium = sum((trade.gross_premium_in for trade in day_trades), Decimal("0"))
        pnl = sum((trade.pnl for trade in day_trades), Decimal("0"))
        by_ticker: dict[str, Decimal] = {}
        for trade in day_trades:
            symbol = _ticker(trade.contract_key)
            by_ticker[symbol] = by_ticker.get(symbol, Decimal("0")) + trade.gross_premium_in
        max_ticker_share = (
            float(max(by_ticker.values()) / premium) if premium > 0 and by_ticker else 0.0
        )
        observations.append(
            DailyPortfolioObservation(
                market_date=market_day,
                trades=len(day_trades),
                gross_premium_in=premium,
                pnl=pnl,
                return_fraction=pnl / premium if premium > 0 else Decimal("0"),
                max_concurrency=_max_concurrency(day_trades),
                max_same_ticker_premium_share=max_ticker_share,
            )
        )
    return tuple(observations)


def _cvar(values: list[float], tail: float = 0.10) -> float:
    if not values:
        return 0.0
    count = max(1, math.ceil(len(values) * tail))
    return statistics.fmean(sorted(values)[:count])


def _max_drawdown(values: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    maximum = 0.0
    for value in values:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        if peak > 0:
            maximum = max(maximum, (peak - equity) / peak)
    return maximum


def _block_sample(
    values: list[float],
    length: int,
    rng: random.Random,
    block_days: int,
) -> list[float]:
    if len(values) == 1:
        return [values[0]] * length
    block = min(block_days, len(values))
    output: list[float] = []
    while len(output) < length:
        start = rng.randrange(len(values))
        for offset in range(block):
            output.append(values[(start + offset) % len(values)])
            if len(output) >= length:
                break
    return output


def _simulate(
    values: list[float],
    risk_fraction: float,
    policy: PortfolioClusterPolicy,
    seed_offset: int,
) -> PortfolioRiskSimulation:
    rng = random.Random(policy.random_seed + seed_offset)
    terminals: list[float] = []
    drawdowns: list[float] = []
    ruin = 0
    breaches = 0
    for _ in range(policy.bootstrap_trials):
        path = _block_sample(
            values,
            policy.bootstrap_horizon_days,
            rng,
            policy.bootstrap_block_days,
        )
        equity = 1.0
        peak = 1.0
        maximum = 0.0
        ruined = False
        for value in path:
            equity *= max(0.0, 1.0 + risk_fraction * value)
            peak = max(peak, equity)
            if peak > 0:
                maximum = max(maximum, (peak - equity) / peak)
            if equity <= policy.ruin_floor_fraction:
                ruined = True
        terminals.append(equity)
        drawdowns.append(maximum)
        ruin += int(ruined)
        breaches += int(maximum > policy.drawdown_breach_level)
    terminals.sort()
    drawdowns.sort()
    p05_index = min(len(terminals) - 1, max(0, int(0.05 * (len(terminals) - 1))))
    return PortfolioRiskSimulation(
        risk_fraction=risk_fraction,
        ruin_probability=ruin / policy.bootstrap_trials,
        drawdown_breach_probability=breaches / policy.bootstrap_trials,
        median_terminal_equity=statistics.median(terminals),
        p05_terminal_equity=terminals[p05_index],
        median_max_drawdown=statistics.median(drawdowns),
    )


def evaluate_portfolio_clusters(
    trades: tuple[CompletedTrade, ...],
    *,
    policy: PortfolioClusterPolicy | None = None,
) -> PortfolioClusterReport:
    policy = policy or PortfolioClusterPolicy()
    days = build_daily_portfolio(trades)
    if len(days) < policy.minimum_days:
        return PortfolioClusterReport(
            days=len(days),
            trades=len(trades),
            max_concurrency=max((item.max_concurrency for item in days), default=0),
            max_same_ticker_premium_share=max(
                (item.max_same_ticker_premium_share for item in days), default=0.0
            ),
            max_single_day_premium_share=1.0 if days else 0.0,
            mean_daily_return=0.0,
            daily_cvar_10=0.0,
            max_daily_path_drawdown=0.0,
            loss_day_rate=0.0,
            status=PortfolioClusterStatus.INSUFFICIENT,
            failures=(f"insufficient_days:{len(days)}<{policy.minimum_days}",),
            max_research_risk_fraction=0.0,
            selected_simulation=None,
            simulations=(),
        )

    total_premium = sum((item.gross_premium_in for item in days), Decimal("0"))
    day_shares = [
        float(item.gross_premium_in / total_premium) if total_premium > 0 else 0.0
        for item in days
    ]
    values = [float(item.return_fraction) for item in days]
    max_same_ticker = max(item.max_same_ticker_premium_share for item in days)
    max_day_share = max(day_shares, default=0.0)
    drawdown = _max_drawdown(values)
    loss_day_rate = sum(value < 0 for value in values) / len(values)
    failures: list[str] = []
    if max_same_ticker > policy.maximum_same_ticker_premium_share:
        failures.append(
            "same_ticker_premium_concentration:"
            f"{max_same_ticker:.4f}>{policy.maximum_same_ticker_premium_share:.4f}"
        )
    if max_day_share > policy.maximum_single_day_premium_share:
        failures.append(
            f"single_day_premium_concentration:{max_day_share:.4f}>"
            f"{policy.maximum_single_day_premium_share:.4f}"
        )
    if drawdown > policy.maximum_daily_drawdown:
        failures.append(
            f"daily_path_drawdown:{drawdown:.4f}>{policy.maximum_daily_drawdown:.4f}"
        )
    if loss_day_rate > policy.maximum_loss_day_rate:
        failures.append(
            f"loss_day_rate:{loss_day_rate:.4f}>{policy.maximum_loss_day_rate:.4f}"
        )

    simulations: list[PortfolioRiskSimulation] = []
    selected: PortfolioRiskSimulation | None = None
    steps = int(policy.maximum_research_risk_fraction / policy.risk_step)
    for index in range(1, steps + 1):
        fraction = round(index * policy.risk_step, 10)
        simulation = _simulate(values, fraction, policy, index * 104729)
        simulations.append(simulation)
        if (
            simulation.ruin_probability <= policy.maximum_ruin_probability
            and simulation.drawdown_breach_probability
            <= policy.maximum_drawdown_breach_probability
        ):
            selected = simulation

    if selected is None:
        failures.append("no_portfolio_risk_fraction_survived_clustered_bootstrap")
    status = PortfolioClusterStatus.PASS if not failures else PortfolioClusterStatus.FAIL
    return PortfolioClusterReport(
        days=len(days),
        trades=len(trades),
        max_concurrency=max(item.max_concurrency for item in days),
        max_same_ticker_premium_share=max_same_ticker,
        max_single_day_premium_share=max_day_share,
        mean_daily_return=statistics.fmean(values),
        daily_cvar_10=_cvar(values),
        max_daily_path_drawdown=drawdown,
        loss_day_rate=loss_day_rate,
        status=status,
        failures=tuple(failures),
        max_research_risk_fraction=selected.risk_fraction if selected is not None else 0.0,
        selected_simulation=selected,
        simulations=tuple(simulations),
    )
