from __future__ import annotations

from dataclasses import dataclass

from .execution_forensics import CompletedTrade


@dataclass(frozen=True, slots=True)
class StrategyOverlapPolicy:
    minimum_jaccard_for_equivalence: float = 0.75
    minimum_containment_for_equivalence: float = 0.90

    def __post_init__(self) -> None:
        for value in (
            self.minimum_jaccard_for_equivalence,
            self.minimum_containment_for_equivalence,
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError("overlap thresholds must be in [0,1]")


@dataclass(frozen=True, slots=True)
class StrategyOverlapPair:
    left: str
    right: str
    left_trades: int
    right_trades: int
    shared_trades: int
    jaccard: float
    containment: float
    equivalent: bool


@dataclass(frozen=True, slots=True)
class StrategyOverlapCluster:
    cluster_id: int
    members: tuple[str, ...]
    representative: str
    union_trades: int


@dataclass(frozen=True, slots=True)
class StrategyOverlapReport:
    strategies: int
    effective_hypotheses: int
    duplicate_strategies: int
    pairs: tuple[StrategyOverlapPair, ...]
    clusters: tuple[StrategyOverlapCluster, ...]

    @property
    def compression_ratio(self) -> float:
        return self.effective_hypotheses / self.strategies if self.strategies else 0.0


def _trade_ids(trades: tuple[CompletedTrade, ...]) -> set[str]:
    return {trade.entry_event_id for trade in trades}


def _pair(
    left: str,
    right: str,
    left_ids: set[str],
    right_ids: set[str],
    policy: StrategyOverlapPolicy,
) -> StrategyOverlapPair:
    shared = len(left_ids & right_ids)
    union = len(left_ids | right_ids)
    smaller = min(len(left_ids), len(right_ids))
    jaccard = shared / union if union else 1.0
    containment = shared / smaller if smaller else 0.0
    equivalent = (
        jaccard >= policy.minimum_jaccard_for_equivalence
        or containment >= policy.minimum_containment_for_equivalence
    )
    return StrategyOverlapPair(
        left=left,
        right=right,
        left_trades=len(left_ids),
        right_trades=len(right_ids),
        shared_trades=shared,
        jaccard=jaccard,
        containment=containment,
        equivalent=equivalent,
    )


def _components(
    names: tuple[str, ...],
    equivalent_pairs: tuple[StrategyOverlapPair, ...],
) -> tuple[tuple[str, ...], ...]:
    adjacency = {name: set[str]() for name in names}
    for pair in equivalent_pairs:
        adjacency[pair.left].add(pair.right)
        adjacency[pair.right].add(pair.left)
    visited: set[str] = set()
    components: list[tuple[str, ...]] = []
    for name in names:
        if name in visited:
            continue
        stack = [name]
        members: list[str] = []
        visited.add(name)
        while stack:
            current = stack.pop()
            members.append(current)
            for neighbor in sorted(adjacency[current], reverse=True):
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        components.append(tuple(sorted(members)))
    return tuple(components)


def _representative(
    members: tuple[str, ...],
    segments: dict[str, tuple[CompletedTrade, ...]],
) -> str:
    """Prefer the most specific strategy with enough evidence, deterministically.

    Smaller trade sets are preferred inside an equivalence cluster because parent aggregates such
    as ``all`` can contain a more specific channel/ticker hypothesis. Ties are lexical for stable
    manifests.
    """

    return min(members, key=lambda name: (len(segments[name]), name))


def analyze_strategy_overlap(
    segments: dict[str, tuple[CompletedTrade, ...]],
    *,
    policy: StrategyOverlapPolicy | None = None,
) -> StrategyOverlapReport:
    policy = policy or StrategyOverlapPolicy()
    names = tuple(sorted(segments))
    id_sets = {name: _trade_ids(segments[name]) for name in names}
    pairs = tuple(
        _pair(left, right, id_sets[left], id_sets[right], policy)
        for left_index, left in enumerate(names)
        for right in names[left_index + 1 :]
    )
    components = _components(names, tuple(pair for pair in pairs if pair.equivalent))
    clusters: list[StrategyOverlapCluster] = []
    for cluster_id, members in enumerate(components):
        union_ids = set[str]()
        for member in members:
            union_ids.update(id_sets[member])
        clusters.append(
            StrategyOverlapCluster(
                cluster_id=cluster_id,
                members=members,
                representative=_representative(members, segments),
                union_trades=len(union_ids),
            )
        )
    effective = len(clusters)
    return StrategyOverlapReport(
        strategies=len(names),
        effective_hypotheses=effective,
        duplicate_strategies=max(0, len(names) - effective),
        pairs=pairs,
        clusters=tuple(clusters),
    )


def deduplicate_strategy_universe(
    segments: dict[str, tuple[CompletedTrade, ...]],
    *,
    policy: StrategyOverlapPolicy | None = None,
) -> tuple[dict[str, tuple[CompletedTrade, ...]], StrategyOverlapReport]:
    report = analyze_strategy_overlap(segments, policy=policy)
    representatives = {cluster.representative for cluster in report.clusters}
    universe = {
        name: segments[name]
        for name in sorted(representatives)
    }
    return universe, report
