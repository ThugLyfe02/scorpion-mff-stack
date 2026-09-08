from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping


class OperationalMode(StrEnum):
    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"


class SignalSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class ResilienceThresholds:
    min_quality_samples: int = 20
    ambiguity_warning_rate: float = 0.30
    ambiguity_critical_rate: float = 0.60
    unresolved_warning_rate: float = 0.15
    unresolved_critical_rate: float = 0.35
    low_confidence_warning_rate: float = 0.20
    pending_raw_warning: int = 3
    pending_raw_critical: int = 10
    pending_review_warning: int = 25
    pipeline_stale_warning_seconds: float = 5.0
    pipeline_stale_critical_seconds: float = 15.0
    pipeline_p95_warning_us: float = 50_000.0
    parser_p95_warning_us: float = 5_000.0
    wal_warning_bytes: int = 128 * 1024 * 1024
    wal_critical_bytes: int = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResilienceSignal:
    code: str
    severity: SignalSeverity
    detail: str


@dataclass(frozen=True, slots=True)
class ResilienceAssessment:
    mode: OperationalMode
    signals: tuple[ResilienceSignal, ...]
    recommended_actions: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return self.mode is OperationalMode.NORMAL


def _float(mapping: Mapping[str, Any], key: str) -> float:
    value = mapping.get(key, 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key, 0)
    return int(value) if isinstance(value, (int, float)) else 0


def _pipeline_age_seconds(
    health: Mapping[str, Any],
    *,
    now: datetime,
) -> float | None:
    beats = health.get("heartbeats")
    if not isinstance(beats, Mapping):
        return None
    pipeline = beats.get("pipeline")
    if not isinstance(pipeline, Mapping):
        return None
    last_seen = pipeline.get("last_seen_ts_utc")
    if not isinstance(last_seen, str):
        return None
    try:
        timestamp = datetime.fromisoformat(last_seen).astimezone(UTC)
    except ValueError:
        return None
    return max(0.0, (now - timestamp).total_seconds())


def assess_resilience(
    health: Mapping[str, Any],
    *,
    wal_bytes: int = 0,
    now: datetime | None = None,
    thresholds: ResilienceThresholds | None = None,
) -> ResilienceAssessment:
    thresholds = thresholds or ResilienceThresholds()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    signals: list[ResilienceSignal] = []

    halt = health.get("halt")
    if isinstance(halt, Mapping) and halt.get("halted") is True:
        signals.append(
            ResilienceSignal(
                "explicit_halt",
                SignalSeverity.CRITICAL,
                str(halt.get("reason") or "explicit runtime halt"),
            )
        )

    pending_raw = _int(health, "pending_raw_revisions")
    if pending_raw >= thresholds.pending_raw_critical:
        signals.append(
            ResilienceSignal(
                "raw_backlog_critical",
                SignalSeverity.CRITICAL,
                f"pending raw revisions={pending_raw}",
            )
        )
    elif pending_raw >= thresholds.pending_raw_warning:
        signals.append(
            ResilienceSignal(
                "raw_backlog",
                SignalSeverity.WARNING,
                f"pending raw revisions={pending_raw}",
            )
        )

    pending_review = _int(health, "pending_review_effects")
    if pending_review >= thresholds.pending_review_warning:
        signals.append(
            ResilienceSignal(
                "review_backlog",
                SignalSeverity.WARNING,
                f"pending review effects={pending_review}",
            )
        )

    decision = health.get("decision_health")
    if isinstance(decision, Mapping):
        count = _int(decision, "count")
        if count >= thresholds.min_quality_samples:
            ambiguity = _float(decision, "ambiguity_rate")
            unresolved = _float(decision, "unresolved_association_rate")
            low_confidence = _float(decision, "low_confidence_rate")
            if ambiguity >= thresholds.ambiguity_critical_rate:
                signals.append(
                    ResilienceSignal(
                        "ambiguity_spike_critical",
                        SignalSeverity.CRITICAL,
                        f"ambiguity_rate={ambiguity:.3f}",
                    )
                )
            elif ambiguity >= thresholds.ambiguity_warning_rate:
                signals.append(
                    ResilienceSignal(
                        "ambiguity_spike",
                        SignalSeverity.WARNING,
                        f"ambiguity_rate={ambiguity:.3f}",
                    )
                )
            if unresolved >= thresholds.unresolved_critical_rate:
                signals.append(
                    ResilienceSignal(
                        "association_failure_critical",
                        SignalSeverity.CRITICAL,
                        f"unresolved_association_rate={unresolved:.3f}",
                    )
                )
            elif unresolved >= thresholds.unresolved_warning_rate:
                signals.append(
                    ResilienceSignal(
                        "association_failure",
                        SignalSeverity.WARNING,
                        f"unresolved_association_rate={unresolved:.3f}",
                    )
                )
            if low_confidence >= thresholds.low_confidence_warning_rate:
                signals.append(
                    ResilienceSignal(
                        "low_confidence_spike",
                        SignalSeverity.WARNING,
                        f"low_confidence_rate={low_confidence:.3f}",
                    )
                )
            parser_p95 = _float(decision, "parser_p95_us")
            pipeline_p95 = _float(decision, "pipeline_p95_us")
            if parser_p95 >= thresholds.parser_p95_warning_us:
                signals.append(
                    ResilienceSignal(
                        "parser_latency",
                        SignalSeverity.WARNING,
                        f"parser_p95_us={parser_p95:.0f}",
                    )
                )
            if pipeline_p95 >= thresholds.pipeline_p95_warning_us:
                signals.append(
                    ResilienceSignal(
                        "pipeline_latency",
                        SignalSeverity.WARNING,
                        f"pipeline_p95_us={pipeline_p95:.0f}",
                    )
                )

    pipeline_age = _pipeline_age_seconds(health, now=now)
    if pipeline_age is not None:
        if pipeline_age >= thresholds.pipeline_stale_critical_seconds:
            signals.append(
                ResilienceSignal(
                    "pipeline_heartbeat_stale_critical",
                    SignalSeverity.CRITICAL,
                    f"pipeline heartbeat age={pipeline_age:.1f}s",
                )
            )
        elif pipeline_age >= thresholds.pipeline_stale_warning_seconds:
            signals.append(
                ResilienceSignal(
                    "pipeline_heartbeat_stale",
                    SignalSeverity.WARNING,
                    f"pipeline heartbeat age={pipeline_age:.1f}s",
                )
            )

    if wal_bytes >= thresholds.wal_critical_bytes:
        signals.append(
            ResilienceSignal(
                "wal_growth_critical",
                SignalSeverity.CRITICAL,
                f"wal_bytes={wal_bytes}",
            )
        )
    elif wal_bytes >= thresholds.wal_warning_bytes:
        signals.append(
            ResilienceSignal(
                "wal_growth",
                SignalSeverity.WARNING,
                f"wal_bytes={wal_bytes}",
            )
        )

    severities = {signal.severity for signal in signals}
    if SignalSeverity.CRITICAL in severities:
        mode = OperationalMode.HALTED
    elif SignalSeverity.WARNING in severities:
        mode = OperationalMode.DEGRADED
    else:
        mode = OperationalMode.NORMAL

    actions: list[str] = []
    if mode is OperationalMode.HALTED:
        actions.extend(
            (
                "block new reviewed effects until operator acknowledgement",
                "preserve raw ingestion and audit logging",
                "surface the critical resilience signals to the operator",
            )
        )
    elif mode is OperationalMode.DEGRADED:
        actions.extend(
            (
                "force actionable events through explicit review",
                "prioritize ambiguity and backlog adjudication",
            )
        )
    return ResilienceAssessment(mode, tuple(signals), tuple(actions))
