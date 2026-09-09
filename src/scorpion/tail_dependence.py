from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import combinations
from zoneinfo import ZoneInfo

from .execution_forensics import CompletedTrade

_MARKET_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class TailDependencePolicy:
    tail_fraction: float = 0.20
    minimum_overlap_days: int = 20
    minimum_tail_events: int = 4
    cluster_dependence_threshold: float = 0.60

    def __post_init__(self) -> None:
        if not 0 < self.tail_fraction < 0.5:
            raise ValueError("tail_fraction must be in (0,0.5)")
        if self.minimum_overlap_days <= 0 or self.minimum_tail_events <= 0:
            raise ValueError("sample thresholds must be positive")
        if not 0 <= self.cluster_dependence_threshold <= 1:
            raise ValueError("cluster_dependence_threshold must be in [0,1]")


@dataclass(frozen=True, slots=True)
class TailDependencePair:
    left: str
    right: str
    overlap_days: int
    left_tail_events: int
    right_tail_events: int
    joint_tail_events: int
    right_given_left_tail: float
    left_given_right_tail: float
    symmetric_tail_dependence: float
    cluster_link: bool


@dataclass(frozen=True, slots=True)
class TailRiskCluster:
    cluster_id: str
    members: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TailDependenceReport:
    groups: int
    pairs: tuple[TailDependencePair, ...]
    clusters: tuple[TailRiskCluster, ...]
    maximum_cluster_size: int


def _ticker(contract_key: str) -> str:
    return contract_key.split("|", 1)[0].upper()


def _daily_group_returns(
    trades: tuple[CompletedTrade, ...],
) -> dict[str, dict[date, float]]:
    premium: dict[tuple[str, date], Decimal] = defaultdict(lambda: Decimal("0"))
    pnl: dict[tuple[str, date], Decimal] = defaultdict(lambda: Decimal("0"))
    for trade in trades:
        group = _ticker(trade.contract_key)
        market_day = trade.opened_ts_utc.astimezone(_MARKET_TZ).date()
        premium[(group, market_day)] += trade.gross_premium_in
        pnl[(group, market_day)] += trade.pnl
    output: dict[str, dict[date, float]] = defaultdict(dict)
    for (group, market_day), gross in premium.items():
        output[group][market_day] = float(pnl[(group, market_day)] / gross) if gross > 0 else 0.0
    return dict(output)


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(fraction * (len(ordered) - 1))))
    return ordered[index]


def _pair(
    left: str,
    right: str,
    series: dict[str, dict[date, float]],
    policy: TailDependencePolicy,
) -> TailDependencePair:
    common = sorted(set(series[left]) & set(series[right]))
    if len(common) < policy.minimum_overlap_days:
        return TailDependencePair(left, right, len(common), 0, 0, 0, 0.0, 0.0, 0.0, False)
    left_values = [series[left][day] for day in common]
    right_values = [series[right][day] for day in common]
    left_threshold = _quantile(left_values, policy.tail_fraction)
    right_threshold = _quantile(right_values, policy.tail_fraction)
    left_tail = {day for day in common if series[left][day] <= left_threshold}
    right_tail = {day for day in common if series[right][day] <= right_threshold}
    joint = left_tail & right_tail
    right_given_left = len(joint) / len(left_tail) if left_tail else 0.0
    left_given_right = len(joint) / len(right_tail) if right_tail else 0.0
    symmetric = min(right_given_left, left_given_right)
    enough = (
        len(left_tail) >= policy.minimum_tail_events
        and len(right_tail) >= policy.minimum_tail_events
    )
    return TailDependencePair(
        left=left,
        right=right,
        overlap_days=len(common),
        left_tail_events=len(left_tail),
        right_tail_events=len(right_tail),
        joint_tail_events=len(joint),
        right_given_left_tail=right_given_left,
        left_given_right_tail=left_given_right,
        symmetric_tail_dependence=symmetric,
        cluster_link=enough and symmetric >= policy.cluster_dependence_threshold,
    )


def _clusters(groups: tuple[str, ...], pairs: tuple[TailDependencePair, ...]) -> tuple[TailRiskCluster, ...]:
    parent = {group: group for group in groups}

    def find(value: str) -> str:
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            next_value = parent[value]
            parent[value] = root
            value = next_value
        return root

    def union(left: str, right: str) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[max(root_left, root_right)] = min(root_left, root_right)

    for item in pairs:
        if item.cluster_link:
            union(item.left, item.right)
    grouped: dict[str, list[str]] = defaultdict(list)
    for group in groups:
        grouped[find(group)].append(group)
    return tuple(
        TailRiskCluster(
            cluster_id=f"tail-{index + 1}",
            members=tuple(sorted(members)),
        )
        for index, (_, members) in enumerate(sorted(grouped.items()))
    )


def evaluate_tail_dependence(
    trades: tuple[CompletedTrade, ...],
    *,
    policy: TailDependencePolicy | None = None,
) -> TailDependenceReport:
    """Cluster nominally different underlyings that share the same lower-tail failure days."""
    policy = policy or TailDependencePolicy()
    series = _daily_group_returns(trades)
    groups = tuple(sorted(series))
    pairs = tuple(
        _pair(left, right, series, policy)
        for left, right in combinations(groups, 2)
    )
    clusters = _clusters(groups, pairs)
    return TailDependenceReport(
        groups=len(groups),
        pairs=pairs,
        clusters=clusters,
        maximum_cluster_size=max((len(item.members) for item in clusters), default=0),
    )
