from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from .execution_forensics import CompletedTrade


class SizingReadiness(StrEnum):
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    NON_POSITIVE_CONSERVATIVE_EDGE = "NON_POSITIVE_CONSERVATIVE_EDGE"
    MULTIPLE_TESTING_NOT_SIGNIFICANT = "MULTIPLE_TESTING_NOT_SIGNIFICANT"
    READY_FOR_RESEARCH = "READY_FOR_RESEARCH"


@dataclass(frozen=True, slots=True)
class SegmentMetrics:
    segment: str
    samples: int
    mean_return: float
    median_return: float
    win_rate: float
    win_rate_lower_90: float
    profit_factor: float
    cvar_10: float
    max_drawdown: float
    positive_fold_ratio: float
    bootstrap_mean_lower_90: float
    bootstrap_edge_p_value: float
    fdr_q_value: float
    shrunk_mean_return: float
    conservative_edge: float
    readiness: SizingReadiness
    edge_score: float


@dataclass(frozen=True, slots=True)
class SizingConstraints:
    min_samples: int = 30
    prior_strength: float = 20.0
    max_fdr_q_value: float = 0.10
    max_risk_fraction: float = 0.10
    candidate_step: float = 0.0025
    horizon_trades: int = 100
    trials: int = 3000
    bootstrap_resamples: int = 2500
    block_length: int = 5
    ruin_floor_fraction: float = 0.50
    max_ruin_probability: float = 0.01
    max_drawdown_limit: float = 0.25
    max_drawdown_breach_probability: float = 0.05
    random_seed: int = 73021

    def __post_init__(self) -> None:
        if self.min_samples < 1:
            raise ValueError("min_samples must be positive")
        if not 0 < self.max_fdr_q_value <= 1:
            raise ValueError("max_fdr_q_value must be in (0,1]")
        if not 0 < self.max_risk_fraction <= 1:
            raise ValueError("max_risk_fraction must be in (0,1]")
        if not 0 < self.candidate_step <= self.max_risk_fraction:
            raise ValueError("candidate_step is invalid")
        if self.horizon_trades <= 0 or self.trials <= 0:
            raise ValueError("simulation horizon and trials must be positive")
        if self.bootstrap_resamples < 100:
            raise ValueError("bootstrap_resamples must be >=100")
        if self.block_length <= 0:
            raise ValueError("block_length must be positive")


@dataclass(frozen=True, slots=True)
class RiskSimulation:
    risk_fraction: float
    ruin_probability: float
    drawdown_breach_probability: float
    median_terminal_equity: float
    p05_terminal_equity: float
    median_max_drawdown: float


@dataclass(frozen=True, slots=True)
class SizingEnvelope:
    segment: str
    readiness: SizingReadiness
    max_research_risk_fraction: float
    selected_simulation: RiskSimulation | None
    simulations: tuple[RiskSimulation, ...]
    reason: str


def _returns(trades: tuple[CompletedTrade, ...]) -> list[float]:
    return [float(trade.return_fraction) for trade in trades]


def _wilson_lower(wins: int, total: int, z: float = 1.6448536269514722) -> float:
    if total <= 0:
        return 0.0
    p = wins / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    center = p + z2 / (2.0 * total)
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return max(0.0, (center - margin) / denominator)


def _profit_factor(values: list[float]) -> float:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses == 0:
        return math.inf if gains > 0 else 0.0
    return gains / losses


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


def _positive_fold_ratio(values: list[float], folds: int = 4) -> float:
    if not values:
        return 0.0
    effective = min(folds, len(values))
    positive = 0
    for index in range(effective):
        start = index * len(values) // effective
        end = (index + 1) * len(values) // effective
        fold = values[start:end]
        positive += int(bool(fold) and statistics.fmean(fold) > 0)
    return positive / effective


def _bootstrap_edge_stats(
    values: list[float],
    *,
    quantile: float,
    resamples: int,
    seed: int,
) -> tuple[float, float]:
    if not values:
        return 0.0, 1.0
    rng = random.Random(seed)
    means = [
        statistics.fmean(rng.choice(values) for _ in range(len(values)))
        for _ in range(resamples)
    ]
    means.sort()
    index = min(len(means) - 1, max(0, int(quantile * (len(means) - 1))))
    lower = means[index]
    non_positive = sum(value <= 0 for value in means)
    p_value = (non_positive + 1) / (len(means) + 1)
    return lower, p_value


def score_segment(
    segment: str,
    trades: tuple[CompletedTrade, ...],
    *,
    constraints: SizingConstraints | None = None,
) -> SegmentMetrics:
    constraints = constraints or SizingConstraints()
    values = _returns(trades)
    samples = len(values)
    mean_return = statistics.fmean(values) if values else 0.0
    median_return = statistics.median(values) if values else 0.0
    wins = sum(value > 0 for value in values)
    win_rate = wins / samples if samples else 0.0
    lower = _wilson_lower(wins, samples)
    bootstrap_lower, edge_p = _bootstrap_edge_stats(
        values,
        quantile=0.10,
        resamples=constraints.bootstrap_resamples,
        seed=constraints.random_seed + sum(ord(char) for char in segment),
    )
    shrinkage = samples / (samples + constraints.prior_strength) if samples else 0.0
    shrunk_mean = mean_return * shrinkage
    conservative_edge = min(shrunk_mean, bootstrap_lower)
    folds = _positive_fold_ratio(values)
    drawdown = _max_drawdown(values)
    tail = _cvar(values)

    if samples < constraints.min_samples:
        readiness = SizingReadiness.INSUFFICIENT_SAMPLE
    elif conservative_edge <= 0:
        readiness = SizingReadiness.NON_POSITIVE_CONSERVATIVE_EDGE
    else:
        readiness = SizingReadiness.READY_FOR_RESEARCH

    edge_score = (
        max(0.0, conservative_edge)
        * math.sqrt(max(samples, 1))
        * (0.50 + 0.50 * folds)
        * (0.50 + 0.50 * lower)
        / (1.0 + drawdown + abs(min(0.0, tail)))
    )
    return SegmentMetrics(
        segment=segment,
        samples=samples,
        mean_return=mean_return,
        median_return=median_return,
        win_rate=win_rate,
        win_rate_lower_90=lower,
        profit_factor=_profit_factor(values),
        cvar_10=tail,
        max_drawdown=drawdown,
        positive_fold_ratio=folds,
        bootstrap_mean_lower_90=bootstrap_lower,
        bootstrap_edge_p_value=edge_p,
        fdr_q_value=1.0,
        shrunk_mean_return=shrunk_mean,
        conservative_edge=conservative_edge,
        readiness=readiness,
        edge_score=edge_score,
    )


def _benjamini_hochberg(metrics: list[SegmentMetrics]) -> list[SegmentMetrics]:
    if not metrics:
        return []
    ordered = sorted(
        enumerate(metrics),
        key=lambda pair: pair[1].bootstrap_edge_p_value,
    )
    total = len(metrics)
    q_values = [1.0] * total
    running = 1.0
    for reverse_rank in range(total - 1, -1, -1):
        original_index, metric = ordered[reverse_rank]
        rank = reverse_rank + 1
        raw_q = metric.bootstrap_edge_p_value * total / rank
        running = min(running, raw_q)
        q_values[original_index] = min(1.0, running)
    return [replace(metric, fdr_q_value=q_values[index]) for index, metric in enumerate(metrics)]


def rank_segments(
    segments: dict[str, tuple[CompletedTrade, ...]],
    *,
    constraints: SizingConstraints | None = None,
) -> tuple[SegmentMetrics, ...]:
    constraints = constraints or SizingConstraints()
    metrics = _benjamini_hochberg(
        [
            score_segment(name, trades, constraints=constraints)
            for name, trades in segments.items()
        ]
    )
    adjusted = [
        replace(
            metric,
            readiness=SizingReadiness.MULTIPLE_TESTING_NOT_SIGNIFICANT,
        )
        if metric.readiness is SizingReadiness.READY_FOR_RESEARCH
        and metric.fdr_q_value > constraints.max_fdr_q_value
        else metric
        for metric in metrics
    ]
    return tuple(
        sorted(
            adjusted,
            key=lambda item: (
                item.readiness is SizingReadiness.READY_FOR_RESEARCH,
                item.edge_score,
                item.samples,
            ),
            reverse=True,
        )
    )


def _block_sample(
    values: list[float],
    length: int,
    rng: random.Random,
    block_length: int,
) -> list[float]:
    if len(values) <= 1:
        return [values[0]] * length
    block = min(block_length, len(values))
    result: list[float] = []
    while len(result) < length:
        start = rng.randrange(len(values))
        for offset in range(block):
            result.append(values[(start + offset) % len(values)])
            if len(result) >= length:
                break
    return result


def _simulate_fraction(
    values: list[float],
    risk_fraction: float,
    constraints: SizingConstraints,
    *,
    seed_offset: int,
) -> RiskSimulation:
    rng = random.Random(constraints.random_seed + seed_offset)
    terminals: list[float] = []
    max_drawdowns: list[float] = []
    ruin_count = 0
    drawdown_breaches = 0

    for _ in range(constraints.trials):
        equity = 1.0
        peak = 1.0
        maximum_drawdown = 0.0
        ruined = False
        path = _block_sample(
            values,
            constraints.horizon_trades,
            rng,
            constraints.block_length,
        )
        for sampled_return in path:
            equity *= max(0.0, 1.0 + risk_fraction * sampled_return)
            peak = max(peak, equity)
            if peak > 0:
                maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)
            if equity <= constraints.ruin_floor_fraction:
                ruined = True
        terminals.append(equity)
        max_drawdowns.append(maximum_drawdown)
        ruin_count += int(ruined)
        drawdown_breaches += int(maximum_drawdown > constraints.max_drawdown_limit)

    terminals.sort()
    max_drawdowns.sort()
    p05_index = min(len(terminals) - 1, max(0, int(0.05 * (len(terminals) - 1))))
    return RiskSimulation(
        risk_fraction=risk_fraction,
        ruin_probability=ruin_count / constraints.trials,
        drawdown_breach_probability=drawdown_breaches / constraints.trials,
        median_terminal_equity=statistics.median(terminals),
        p05_terminal_equity=terminals[p05_index],
        median_max_drawdown=statistics.median(max_drawdowns),
    )


def build_sizing_envelope(
    segment: str,
    trades: tuple[CompletedTrade, ...],
    *,
    constraints: SizingConstraints | None = None,
    metrics: SegmentMetrics | None = None,
) -> SizingEnvelope:
    constraints = constraints or SizingConstraints()
    metrics = metrics or score_segment(segment, trades, constraints=constraints)
    if metrics.readiness is not SizingReadiness.READY_FOR_RESEARCH:
        return SizingEnvelope(
            segment=segment,
            readiness=metrics.readiness,
            max_research_risk_fraction=0.0,
            selected_simulation=None,
            simulations=(),
            reason=(
                f"segment not sizing-ready: {metrics.readiness.value}; "
                f"samples={metrics.samples}, conservative_edge={metrics.conservative_edge:.4f}, "
                f"fdr_q={metrics.fdr_q_value:.4f}"
            ),
        )

    values = _returns(trades)
    simulations: list[RiskSimulation] = []
    selected: RiskSimulation | None = None
    steps = int(constraints.max_risk_fraction / constraints.candidate_step)
    for index in range(1, steps + 1):
        fraction = round(index * constraints.candidate_step, 10)
        simulation = _simulate_fraction(
            values,
            fraction,
            constraints,
            seed_offset=index * 7919,
        )
        simulations.append(simulation)
        if (
            simulation.ruin_probability <= constraints.max_ruin_probability
            and simulation.drawdown_breach_probability
            <= constraints.max_drawdown_breach_probability
        ):
            selected = simulation

    if selected is None:
        return SizingEnvelope(
            segment=segment,
            readiness=metrics.readiness,
            max_research_risk_fraction=0.0,
            selected_simulation=None,
            simulations=tuple(simulations),
            reason="no tested risk fraction satisfies the configured ruin/drawdown constraints",
        )
    return SizingEnvelope(
        segment=segment,
        readiness=metrics.readiness,
        max_research_risk_fraction=selected.risk_fraction,
        selected_simulation=selected,
        simulations=tuple(simulations),
        reason=(
            "largest tested research risk fraction satisfying streak-preserving bootstrap "
            "ruin and drawdown constraints; not a guarantee or live sizing instruction"
        ),
    )


def segment_completed_trades(
    trades: tuple[CompletedTrade, ...],
) -> dict[str, tuple[CompletedTrade, ...]]:
    grouped: dict[str, list[CompletedTrade]] = {}
    for trade in trades:
        grouped.setdefault(f"channel:{trade.channel_id}", []).append(trade)
        grouped.setdefault(f"bucket:{trade.bucket.value}", []).append(trade)
        ticker = trade.contract_key.split("|", 1)[0]
        grouped.setdefault(f"ticker:{ticker}", []).append(trade)
    grouped["all"] = list(trades)
    return {name: tuple(items) for name, items in grouped.items()}


def decimal_dollars(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"
