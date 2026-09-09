from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from .execution_forensics import CompletedTrade
from .tail_dependence import TailDependenceReport, TailRiskCluster

_MARKET_TZ = ZoneInfo("America/New_York")


class StressClusterStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class StressClusterPolicy:
    minimum_days: int = 30
    tail_fraction: float = 0.10
    maximum_cluster_premium_share: float = 0.55
    maximum_cluster_es_share: float = 0.65
    maximum_research_cluster_risk_fraction: float = 0.06

    def __post_init__(self) -> None:
        if self.minimum_days <= 0:
            raise ValueError("minimum_days must be positive")
        if not 0 < self.tail_fraction < 0.5:
            raise ValueError("tail_fraction must be in (0,0.5)")
        for name in (
            "maximum_cluster_premium_share",
            "maximum_cluster_es_share",
            "maximum_research_cluster_risk_fraction",
        ):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0,1]")


@dataclass(frozen=True, slots=True)
class ClusterStressContribution:
    cluster_id: str
    members: tuple[str, ...]
    premium_share: float
    tail_loss: float
    expected_shortfall_contribution: float
    expected_shortfall_share: float


@dataclass(frozen=True, slots=True)
class StressClusterRiskReport:
    days: int
    tail_days: int
    clusters: tuple[ClusterStressContribution, ...]
    maximum_cluster_premium_share: float
    maximum_cluster_es_share: float
    portfolio_expected_shortfall: float
    concentration_scale: float
    max_research_cluster_risk_fraction: float
    status: StressClusterStatus
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.status is StressClusterStatus.PASS


def _ticker(contract_key: str) -> str:
    return contract_key.split("|", 1)[0].upper()


def _cluster_map(clusters: tuple[TailRiskCluster, ...]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for cluster in clusters:
        for member in cluster.members:
            if member in mapping:
                raise ValueError(f"ticker appears in multiple tail clusters: {member}")
            mapping[member] = cluster.cluster_id
    return mapping


def _cluster_members(report: TailDependenceReport) -> dict[str, tuple[str, ...]]:
    return {cluster.cluster_id: cluster.members for cluster in report.clusters}


def _daily_cluster_pnl(
    trades: tuple[CompletedTrade, ...],
    mapping: dict[str, str],
) -> tuple[
    dict[date, Decimal],
    dict[date, Decimal],
    dict[tuple[date, str], Decimal],
    dict[str, Decimal],
]:
    day_premium: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    day_pnl: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    cluster_pnl: dict[tuple[date, str], Decimal] = defaultdict(lambda: Decimal("0"))
    cluster_premium: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for trade in trades:
        ticker = _ticker(trade.contract_key)
        cluster_id = mapping.get(ticker)
        if cluster_id is None:
            continue
        market_day = trade.opened_ts_utc.astimezone(_MARKET_TZ).date()
        day_premium[market_day] += trade.gross_premium_in
        day_pnl[market_day] += trade.pnl
        cluster_pnl[(market_day, cluster_id)] += trade.pnl
        cluster_premium[cluster_id] += trade.gross_premium_in
    return dict(day_premium), dict(day_pnl), dict(cluster_pnl), dict(cluster_premium)


def _tail_days(
    day_premium: dict[date, Decimal],
    day_pnl: dict[date, Decimal],
    fraction: float,
) -> tuple[date, ...]:
    rows = [
        (day, float(day_pnl[day] / premium))
        for day, premium in day_premium.items()
        if premium > 0
    ]
    if not rows:
        return ()
    count = max(1, math.ceil(len(rows) * fraction))
    return tuple(day for day, _ in sorted(rows, key=lambda item: item[1])[:count])


def evaluate_stress_cluster_risk(
    trades: tuple[CompletedTrade, ...],
    tail_dependence: TailDependenceReport,
    *,
    policy: StressClusterPolicy | None = None,
) -> StressClusterRiskReport:
    """Measure latent-cluster concentration on the portfolio's worst historical days.

    Expected-shortfall contribution is additive P&L attribution on the same portfolio tail days,
    not a forecast of future losses. The output is a research constraint and never a live sizing
    instruction.
    """
    policy = policy or StressClusterPolicy()
    mapping = _cluster_map(tail_dependence.clusters)
    members = _cluster_members(tail_dependence)
    day_premium, day_pnl, cluster_pnl, cluster_premium = _daily_cluster_pnl(trades, mapping)
    usable_days = tuple(sorted(day for day, premium in day_premium.items() if premium > 0))
    if len(usable_days) < policy.minimum_days or not members:
        return StressClusterRiskReport(
            days=len(usable_days),
            tail_days=0,
            clusters=(),
            maximum_cluster_premium_share=0.0,
            maximum_cluster_es_share=0.0,
            portfolio_expected_shortfall=0.0,
            concentration_scale=0.0,
            max_research_cluster_risk_fraction=0.0,
            status=StressClusterStatus.INSUFFICIENT,
            failures=(
                f"insufficient_days:{len(usable_days)}<{policy.minimum_days}"
                if len(usable_days) < policy.minimum_days
                else "no_tail_clusters"
            ,),
        )

    tails = _tail_days(day_premium, day_pnl, policy.tail_fraction)
    total_premium = sum(cluster_premium.values(), Decimal("0"))
    portfolio_tail_losses = [max(0.0, -float(day_pnl[day])) for day in tails]
    portfolio_es = (
        sum(portfolio_tail_losses) / len(portfolio_tail_losses)
        if portfolio_tail_losses
        else 0.0
    )

    cluster_tail_loss: dict[str, float] = {}
    for cluster_id in members:
        losses = [max(0.0, -float(cluster_pnl.get((day, cluster_id), Decimal("0")))) for day in tails]
        cluster_tail_loss[cluster_id] = sum(losses) / len(losses) if losses else 0.0
    total_cluster_tail_loss = sum(cluster_tail_loss.values())

    contributions: list[ClusterStressContribution] = []
    for cluster_id in sorted(members):
        premium_share = (
            float(cluster_premium.get(cluster_id, Decimal("0")) / total_premium)
            if total_premium > 0
            else 0.0
        )
        contribution = cluster_tail_loss[cluster_id]
        es_share = contribution / total_cluster_tail_loss if total_cluster_tail_loss > 0 else 0.0
        contributions.append(
            ClusterStressContribution(
                cluster_id=cluster_id,
                members=members[cluster_id],
                premium_share=premium_share,
                tail_loss=contribution,
                expected_shortfall_contribution=contribution,
                expected_shortfall_share=es_share,
            )
        )

    max_premium = max((item.premium_share for item in contributions), default=0.0)
    max_es = max((item.expected_shortfall_share for item in contributions), default=0.0)
    premium_scale = (
        min(1.0, policy.maximum_cluster_premium_share / max_premium)
        if max_premium > 0
        else 1.0
    )
    es_scale = (
        min(1.0, policy.maximum_cluster_es_share / max_es)
        if max_es > 0
        else 1.0
    )
    scale = min(premium_scale, es_scale)
    failures: list[str] = []
    if max_premium > policy.maximum_cluster_premium_share:
        failures.append(
            "latent_cluster_premium_concentration:"
            f"{max_premium:.6f}>{policy.maximum_cluster_premium_share:.6f}"
        )
    if max_es > policy.maximum_cluster_es_share:
        failures.append(
            "latent_cluster_expected_shortfall_concentration:"
            f"{max_es:.6f}>{policy.maximum_cluster_es_share:.6f}"
        )
    status = StressClusterStatus.PASS if not failures else StressClusterStatus.FAIL
    return StressClusterRiskReport(
        days=len(usable_days),
        tail_days=len(tails),
        clusters=tuple(contributions),
        maximum_cluster_premium_share=max_premium,
        maximum_cluster_es_share=max_es,
        portfolio_expected_shortfall=portfolio_es,
        concentration_scale=scale,
        max_research_cluster_risk_fraction=(
            policy.maximum_research_cluster_risk_fraction * scale
        ),
        status=status,
        failures=tuple(failures),
    )
