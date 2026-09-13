from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median

from .scanner_context import (
    ScannerContextObservation,
    latest_scanner_context_available_before,
    latest_scanner_context_before,
)


@dataclass(frozen=True, slots=True)
class MffContextProbe:
    """Minimal MFF event identity for scanner-context research audits.

    Intentionally contains no order/effect fields and does not import the MFF
    reducer/domain. This keeps the audit outside execution authority.
    """

    event_id: str
    symbol: str
    source_ts_utc: datetime
    received_ts_utc: datetime

    def __post_init__(self) -> None:
        for name in ("source_ts_utc", "received_ts_utc"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.received_ts_utc < self.source_ts_utc:
            raise ValueError("received_ts_utc cannot precede source_ts_utc")


@dataclass(frozen=True, slots=True)
class ScannerContextAuditRow:
    event_id: str
    symbol: str
    source_context_observation_id: str | None
    causal_safe_observation_id: str | None
    operational_observation_id: str | None
    source_context_age_seconds: float | None
    operational_context_age_seconds: float | None
    availability_lag_seconds: float | None
    backfill_only: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScannerContextAuditReport:
    events: int
    source_context_events: int
    causal_safe_events: int
    operational_available_events: int
    backfill_only_events: int
    source_context_rate: float
    causal_safe_rate: float
    operational_available_rate: float
    median_source_age_seconds: float | None
    p95_source_age_seconds: float | None
    median_operational_age_seconds: float | None
    p95_operational_age_seconds: float | None
    median_availability_lag_seconds: float | None
    p95_availability_lag_seconds: float | None
    blocker_counts: tuple[tuple[str, int], ...]
    rows: tuple[ScannerContextAuditRow, ...]


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    # Deterministic nearest-rank p95, suitable for operational evidence summaries.
    rank = max(1, (95 * len(ordered) + 99) // 100)
    return ordered[min(rank - 1, len(ordered) - 1)]


def _age_seconds(event_ts: datetime, observation_ts: datetime) -> float:
    return max(
        0.0,
        (
            event_ts.astimezone(UTC)
            - observation_ts.astimezone(UTC)
        ).total_seconds(),
    )


def audit_scanner_context_coverage(
    observations: tuple[ScannerContextObservation, ...],
    probes: tuple[MffContextProbe, ...],
) -> ScannerContextAuditReport:
    """Measure scanner usefulness without changing MFF runtime behavior.

    Three layers are intentionally separate:

    * source context: past-only observation exists, even if provenance is weak;
    * causal-safe: evidence gates pass for source-time research;
    * operational: causal-safe context had actually arrived before MFF received
      the event.

    A high source-context rate with a low operational rate is evidence of a
    backfill/latency problem, not evidence that the live system had confluence.
    """

    rows: list[ScannerContextAuditRow] = []
    source_ages: list[float] = []
    operational_ages: list[float] = []
    availability_lags: list[float] = []
    blockers: Counter[str] = Counter()
    source_count = 0
    safe_count = 0
    operational_count = 0
    backfill_count = 0

    for probe in probes:
        any_context = latest_scanner_context_before(
            observations,
            symbol=probe.symbol,
            source_ts_utc=probe.source_ts_utc,
            require_causal_safe=False,
        )
        safe_context = latest_scanner_context_before(
            observations,
            symbol=probe.symbol,
            source_ts_utc=probe.source_ts_utc,
            require_causal_safe=True,
        )
        operational = latest_scanner_context_available_before(
            observations,
            symbol=probe.symbol,
            source_ts_utc=probe.source_ts_utc,
            received_ts_utc=probe.received_ts_utc,
            require_causal_safe=True,
        )

        source_age: float | None = None
        operational_age: float | None = None
        availability_lag: float | None = None
        row_blockers: tuple[str, ...] = ()

        if any_context is not None:
            source_count += 1
            source_age = _age_seconds(
                probe.source_ts_utc,
                any_context.observed_at_utc,
            )
            source_ages.append(source_age)
            row_blockers = any_context.causal_blocking_reasons
            blockers.update(row_blockers)

        if safe_context is not None:
            safe_count += 1

        if operational is not None:
            operational_count += 1
            operational_age = _age_seconds(
                probe.source_ts_utc,
                operational.observed_at_utc,
            )
            operational_ages.append(operational_age)
            assert operational.received_at_utc is not None
            availability_lag = max(
                0.0,
                (
                    operational.received_at_utc
                    - operational.observed_at_utc
                ).total_seconds(),
            )
            availability_lags.append(availability_lag)

        backfill_only = safe_context is not None and operational is None
        if backfill_only:
            backfill_count += 1

        rows.append(
            ScannerContextAuditRow(
                event_id=probe.event_id,
                symbol=probe.symbol.strip().upper(),
                source_context_observation_id=(
                    any_context.observation_id if any_context is not None else None
                ),
                causal_safe_observation_id=(
                    safe_context.observation_id if safe_context is not None else None
                ),
                operational_observation_id=(
                    operational.observation_id if operational is not None else None
                ),
                source_context_age_seconds=source_age,
                operational_context_age_seconds=operational_age,
                availability_lag_seconds=availability_lag,
                backfill_only=backfill_only,
                blockers=row_blockers,
            )
        )

    total = len(probes)

    def rate(count: int) -> float:
        return count / total if total else 0.0

    return ScannerContextAuditReport(
        events=total,
        source_context_events=source_count,
        causal_safe_events=safe_count,
        operational_available_events=operational_count,
        backfill_only_events=backfill_count,
        source_context_rate=rate(source_count),
        causal_safe_rate=rate(safe_count),
        operational_available_rate=rate(operational_count),
        median_source_age_seconds=median(source_ages) if source_ages else None,
        p95_source_age_seconds=_p95(source_ages),
        median_operational_age_seconds=(
            median(operational_ages) if operational_ages else None
        ),
        p95_operational_age_seconds=_p95(operational_ages),
        median_availability_lag_seconds=(
            median(availability_lags) if availability_lags else None
        ),
        p95_availability_lag_seconds=_p95(availability_lags),
        blocker_counts=tuple(sorted(blockers.items())),
        rows=tuple(rows),
    )
