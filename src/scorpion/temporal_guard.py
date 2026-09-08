from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .domain import RawDiscordMessage


class TemporalSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class TemporalPolicy:
    source_future_tolerance: timedelta = timedelta(milliseconds=250)
    maximum_receive_lag: timedelta = timedelta(seconds=5)
    maximum_quote_lag: timedelta = timedelta(seconds=3)
    maximum_source_regression: timedelta = timedelta(milliseconds=250)

    def __post_init__(self) -> None:
        if min(
            self.source_future_tolerance,
            self.maximum_receive_lag,
            self.maximum_quote_lag,
            self.maximum_source_regression,
        ) < timedelta(0):
            raise ValueError("temporal tolerances cannot be negative")


@dataclass(frozen=True, slots=True)
class TemporalFinding:
    code: str
    severity: TemporalSeverity
    detail: str


@dataclass(frozen=True, slots=True)
class TemporalAssessment:
    findings: tuple[TemporalFinding, ...]

    @property
    def critical(self) -> bool:
        return any(item.severity is TemporalSeverity.CRITICAL for item in self.findings)

    @property
    def requires_review(self) -> bool:
        return any(
            item.severity in {TemporalSeverity.WARNING, TemporalSeverity.CRITICAL}
            for item in self.findings
        )


def assess_message_time(
    raw: RawDiscordMessage,
    *,
    policy: TemporalPolicy | None = None,
) -> TemporalAssessment:
    policy = policy or TemporalPolicy()
    findings: list[TemporalFinding] = []
    source = raw.source_ts_utc.astimezone(UTC)
    received = raw.received_ts_utc.astimezone(UTC)
    receive_lag = received - source
    if receive_lag < -policy.source_future_tolerance:
        findings.append(
            TemporalFinding(
                "source_timestamp_in_future",
                TemporalSeverity.CRITICAL,
                f"source appears {-receive_lag.total_seconds():.6f}s after receive clock",
            )
        )
    elif receive_lag > policy.maximum_receive_lag:
        findings.append(
            TemporalFinding(
                "discord_receive_lag_high",
                TemporalSeverity.WARNING,
                f"receive lag {receive_lag.total_seconds():.6f}s",
            )
        )
    if raw.edited_ts_utc is not None:
        edited = raw.edited_ts_utc.astimezone(UTC)
        if edited < source:
            findings.append(
                TemporalFinding(
                    "edit_precedes_create",
                    TemporalSeverity.CRITICAL,
                    "Discord edit timestamp precedes source creation timestamp",
                )
            )
    return TemporalAssessment(tuple(findings))


def assess_quote_time(
    *,
    decision_ts_utc: datetime,
    quote_ts_utc: datetime,
    policy: TemporalPolicy | None = None,
) -> TemporalAssessment:
    policy = policy or TemporalPolicy()
    decision = decision_ts_utc.astimezone(UTC)
    quote = quote_ts_utc.astimezone(UTC)
    lag = quote - decision
    if lag < timedelta(0):
        return TemporalAssessment(
            (
                TemporalFinding(
                    "quote_precedes_decision",
                    TemporalSeverity.CRITICAL,
                    f"quote precedes decision by {-lag.total_seconds():.6f}s",
                ),
            )
        )
    if lag > policy.maximum_quote_lag:
        return TemporalAssessment(
            (
                TemporalFinding(
                    "quote_lag_high",
                    TemporalSeverity.WARNING,
                    f"quote lag {lag.total_seconds():.6f}s",
                ),
            )
        )
    return TemporalAssessment(())


@dataclass(frozen=True, slots=True)
class TemporalStreamReport:
    messages: int
    future_source_events: int
    high_receive_lag_events: int
    edit_order_errors: int
    source_regressions: int
    receive_lag_p95_ms: float
    critical: bool


def _p95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(0.95 * (len(ordered) - 1))))
    return ordered[index]


def load_temporal_stream_report(
    path: str | Path,
    *,
    limit: int = 5000,
    policy: TemporalPolicy | None = None,
) -> TemporalStreamReport:
    if limit <= 0:
        raise ValueError("limit must be positive")
    policy = policy or TemporalPolicy()
    with sqlite3.connect(str(path)) as db:
        rows = db.execute(
            """
            SELECT message_id,channel_id,author_id,source_ts_utc,received_ts_utc,edited_ts_utc
            FROM raw_discord_events
            ORDER BY received_ts_utc DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    rows.reverse()
    last_source: dict[tuple[str, str], datetime] = {}
    receive_lags_ms: list[float] = []
    future = 0
    high_lag = 0
    edit_errors = 0
    regressions = 0
    for row in rows:
        channel_id = str(row[1])
        author_id = str(row[2])
        source = datetime.fromisoformat(str(row[3])).astimezone(UTC)
        received = datetime.fromisoformat(str(row[4])).astimezone(UTC)
        lag = received - source
        receive_lags_ms.append(lag.total_seconds() * 1000.0)
        future += int(lag < -policy.source_future_tolerance)
        high_lag += int(lag > policy.maximum_receive_lag)
        if row[5] is not None:
            edited = datetime.fromisoformat(str(row[5])).astimezone(UTC)
            edit_errors += int(edited < source)
        key = (author_id, channel_id)
        previous = last_source.get(key)
        if previous is not None and source + policy.maximum_source_regression < previous:
            regressions += 1
        last_source[key] = max(previous, source) if previous is not None else source
    return TemporalStreamReport(
        messages=len(rows),
        future_source_events=future,
        high_receive_lag_events=high_lag,
        edit_order_errors=edit_errors,
        source_regressions=regressions,
        receive_lag_p95_ms=_p95(receive_lags_ms),
        critical=bool(future or edit_errors or regressions),
    )
