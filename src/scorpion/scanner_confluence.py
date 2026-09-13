from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from statistics import mean, median
from zoneinfo import ZoneInfo

from .execution_forensics import CompletedTrade
from .scanner_batch import ScannerContextBatch, ScannerBatchRowMetadata
from .scanner_context import (
    ScannerContextObservation,
    latest_scanner_context_available_before,
    latest_scanner_context_before,
)

_ET = ZoneInfo("America/New_York")


class ConfluenceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class ScannerConfluencePolicy:
    max_context_age_seconds: float = 1800.0
    require_operational_availability: bool = False
    min_selected_trades: int = 20
    min_not_selected_trades: int = 20
    min_paired_days: int = 5
    bootstrap_trials: int = 2000
    bootstrap_alpha: float = 0.10
    min_day_lift_lower_pct: float = 0.0
    random_seed: int = 91127

    def __post_init__(self) -> None:
        if self.max_context_age_seconds <= 0:
            raise ValueError("max_context_age_seconds must be positive")
        if self.min_selected_trades <= 0 or self.min_not_selected_trades <= 0:
            raise ValueError("minimum trade counts must be positive")
        if self.min_paired_days < 2:
            raise ValueError("min_paired_days must be >=2")
        if self.bootstrap_trials < 100:
            raise ValueError("bootstrap_trials must be >=100")
        if not 0 < self.bootstrap_alpha < 0.5:
            raise ValueError("bootstrap_alpha must be between 0 and 0.5")


@dataclass(frozen=True, slots=True)
class ConfluenceTradeLink:
    entry_event_id: str
    symbol: str
    opened_ts_utc: object
    return_pct: float
    observation_id: str | None
    scanner_run_id: str | None
    context_age_seconds: float | None
    selection_disposition: str | None
    tape_flag: str | None
    ross_boxes_hit: int | None
    rvol: float | None
    full_universe_comparable: bool


@dataclass(frozen=True, slots=True)
class ScannerConfluenceReport:
    status: ConfluenceStatus
    total_trades: int
    causal_fresh_context_trades: int
    comparable_full_universe_trades: int
    context_coverage_rate: float
    selected_n: int
    not_selected_n: int
    selected_mean_return_pct: float | None
    not_selected_mean_return_pct: float | None
    selected_median_return_pct: float | None
    not_selected_median_return_pct: float | None
    paired_days: int
    mean_paired_day_lift_pct: float | None
    bootstrap_paired_day_lift_lower_pct: float | None
    day_lifts_pct: tuple[float, ...]
    failures: tuple[str, ...]
    links: tuple[ConfluenceTradeLink, ...]


def _bootstrap_lower(
    values: list[float],
    *,
    trials: int,
    alpha: float,
    seed: int,
) -> float:
    rng = random.Random(seed)
    n = len(values)
    draws: list[float] = []
    for _ in range(trials):
        draws.append(mean(values[rng.randrange(n)] for _ in range(n)))
    draws.sort()
    index = max(0, min(len(draws) - 1, math.floor(alpha * len(draws))))
    return draws[index]


def _ticker(contract_key: str) -> str:
    return contract_key.split("|", 1)[0].strip().upper()


def _return_pct(trade: CompletedTrade) -> float:
    return float(trade.return_fraction) * 100.0


def _unique_observations(
    batches: tuple[ScannerContextBatch, ...],
) -> tuple[ScannerContextObservation, ...]:
    by_id: dict[str, ScannerContextObservation] = {}
    for batch in batches:
        for item in batch.observations:
            prior = by_id.get(item.observation_id)
            if prior is not None and prior != item:
                raise ValueError("conflicting scanner observation identity across batches")
            by_id[item.observation_id] = item
    return tuple(by_id.values())


def _comparable_metadata(
    batches: tuple[ScannerContextBatch, ...],
) -> tuple[
    tuple[ScannerContextObservation, ...],
    dict[str, ScannerBatchRowMetadata],
]:
    observations: dict[str, ScannerContextObservation] = {}
    metadata: dict[str, ScannerBatchRowMetadata] = {}
    for batch in batches:
        if not batch.absence_is_interpretable:
            continue
        for item in batch.observations:
            observations[item.observation_id] = item
        for meta in batch.row_metadata:
            prior = metadata.get(meta.observation_id)
            if prior is not None and prior != meta:
                raise ValueError("conflicting scanner row metadata across batches")
            metadata[meta.observation_id] = meta
    return tuple(observations.values()), metadata


def _select_context(
    observations: tuple[ScannerContextObservation, ...],
    trade: CompletedTrade,
    *,
    symbol: str,
    policy: ScannerConfluencePolicy,
    entry_received_at_by_event_id: Mapping[str, object] | None,
) -> ScannerContextObservation | None:
    if policy.require_operational_availability:
        if entry_received_at_by_event_id is None:
            return None
        raw_received = entry_received_at_by_event_id.get(trade.entry_event_id)
        from datetime import datetime

        if not isinstance(raw_received, datetime):
            return None
        context = latest_scanner_context_available_before(
            observations,
            symbol=symbol,
            source_ts_utc=trade.opened_ts_utc,
            received_ts_utc=raw_received,
            require_causal_safe=True,
        )
    else:
        context = latest_scanner_context_before(
            observations,
            symbol=symbol,
            source_ts_utc=trade.opened_ts_utc,
            require_causal_safe=True,
        )
    if context is None:
        return None
    age = (trade.opened_ts_utc - context.observed_at_utc).total_seconds()
    if age < 0 or age > policy.max_context_age_seconds:
        return None
    return context


def evaluate_scanner_mff_confluence(
    trades: tuple[CompletedTrade, ...],
    batches: tuple[ScannerContextBatch, ...],
    *,
    policy: ScannerConfluencePolicy | None = None,
    entry_received_at_by_event_id: Mapping[str, object] | None = None,
) -> ScannerConfluenceReport:
    """Measure scanner information value on quote-supported MFF completed trades.

    Selection lift is admitted only from complete FULL_UNIVERSE scanner batches,
    because only those runs contain explicit same-run NOT_SELECTED controls.
    The comparison is clustered by ET trading day before bootstrap so one busy
    session cannot masquerade as many independent confirmations.
    """

    policy = policy or ScannerConfluencePolicy()
    all_observations = _unique_observations(batches)
    comparable_observations, metadata = _comparable_metadata(batches)

    links: list[ConfluenceTradeLink] = []
    context_count = 0
    comparable_count = 0
    selected_returns: list[float] = []
    not_selected_returns: list[float] = []
    by_day: dict[object, dict[str, list[float]]] = defaultdict(
        lambda: {"SELECTED": [], "NOT_SELECTED": []}
    )

    for trade in sorted(trades, key=lambda item: (item.opened_ts_utc, item.entry_event_id)):
        symbol = _ticker(trade.contract_key)
        any_context = _select_context(
            all_observations,
            trade,
            symbol=symbol,
            policy=policy,
            entry_received_at_by_event_id=entry_received_at_by_event_id,
        )
        comparable = _select_context(
            comparable_observations,
            trade,
            symbol=symbol,
            policy=policy,
            entry_received_at_by_event_id=entry_received_at_by_event_id,
        )
        chosen = comparable or any_context
        return_pct = _return_pct(trade)
        disposition: str | None = None
        full_universe = False
        if any_context is not None:
            context_count += 1
        if comparable is not None:
            meta = metadata.get(comparable.observation_id)
            if meta is not None:
                disposition = meta.selection_disposition
                if disposition in {"SELECTED", "NOT_SELECTED"}:
                    comparable_count += 1
                    full_universe = True
                    day = trade.opened_ts_utc.astimezone(_ET).date()
                    by_day[day][disposition].append(return_pct)
                    if disposition == "SELECTED":
                        selected_returns.append(return_pct)
                    else:
                        not_selected_returns.append(return_pct)

        links.append(
            ConfluenceTradeLink(
                entry_event_id=trade.entry_event_id,
                symbol=symbol,
                opened_ts_utc=trade.opened_ts_utc,
                return_pct=return_pct,
                observation_id=(chosen.observation_id if chosen is not None else None),
                scanner_run_id=(chosen.run_id if chosen is not None else None),
                context_age_seconds=(
                    (trade.opened_ts_utc - chosen.observed_at_utc).total_seconds()
                    if chosen is not None
                    else None
                ),
                selection_disposition=disposition,
                tape_flag=(chosen.tape_flag if chosen is not None else None),
                ross_boxes_hit=(chosen.ross_boxes_hit if chosen is not None else None),
                rvol=(chosen.rvol if chosen is not None else None),
                full_universe_comparable=full_universe,
            )
        )

    day_lifts: list[float] = []
    for day in sorted(by_day):
        selected = by_day[day]["SELECTED"]
        not_selected = by_day[day]["NOT_SELECTED"]
        if selected and not_selected:
            day_lifts.append(mean(selected) - mean(not_selected))

    failures: list[str] = []
    if len(selected_returns) < policy.min_selected_trades:
        failures.append(
            f"selected_depth:{len(selected_returns)}<{policy.min_selected_trades}"
        )
    if len(not_selected_returns) < policy.min_not_selected_trades:
        failures.append(
            "not_selected_depth:"
            f"{len(not_selected_returns)}<{policy.min_not_selected_trades}"
        )
    if len(day_lifts) < policy.min_paired_days:
        failures.append(f"paired_days:{len(day_lifts)}<{policy.min_paired_days}")

    bootstrap_lower: float | None = None
    if day_lifts:
        bootstrap_lower = _bootstrap_lower(
            day_lifts,
            trials=policy.bootstrap_trials,
            alpha=policy.bootstrap_alpha,
            seed=policy.random_seed,
        )
        if bootstrap_lower < policy.min_day_lift_lower_pct:
            failures.append(
                "paired_day_lift_lower:"
                f"{bootstrap_lower:.4f}<{policy.min_day_lift_lower_pct:.4f}"
            )

    insufficient = (
        len(selected_returns) < policy.min_selected_trades
        or len(not_selected_returns) < policy.min_not_selected_trades
        or len(day_lifts) < policy.min_paired_days
    )
    status = (
        ConfluenceStatus.INSUFFICIENT
        if insufficient
        else ConfluenceStatus.FAIL
        if failures
        else ConfluenceStatus.PASS
    )
    total = len(trades)
    return ScannerConfluenceReport(
        status=status,
        total_trades=total,
        causal_fresh_context_trades=context_count,
        comparable_full_universe_trades=comparable_count,
        context_coverage_rate=(context_count / total if total else 0.0),
        selected_n=len(selected_returns),
        not_selected_n=len(not_selected_returns),
        selected_mean_return_pct=(mean(selected_returns) if selected_returns else None),
        not_selected_mean_return_pct=(
            mean(not_selected_returns) if not_selected_returns else None
        ),
        selected_median_return_pct=(
            median(selected_returns) if selected_returns else None
        ),
        not_selected_median_return_pct=(
            median(not_selected_returns) if not_selected_returns else None
        ),
        paired_days=len(day_lifts),
        mean_paired_day_lift_pct=(mean(day_lifts) if day_lifts else None),
        bootstrap_paired_day_lift_lower_pct=bootstrap_lower,
        day_lifts_pct=tuple(day_lifts),
        failures=tuple(failures),
        links=tuple(links),
    )
