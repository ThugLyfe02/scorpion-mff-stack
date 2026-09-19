from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .config import ALLOWED_CHANNEL_IDS
from .resilience import (
    OperationalMode,
    ResilienceAssessment,
    ResilienceSignal,
    SignalSeverity,
    assess_resilience,
)
from .source_intelligence import (
    SourceBehaviorShift,
    compare_source_behavior,
    load_source_behavior_profile,
)
from .stage_trace import storage_snapshot
from .store import Store


def _normal() -> ResilienceAssessment:
    return ResilienceAssessment(OperationalMode.NORMAL, (), ())


@dataclass(slots=True)
class ResilienceController:
    store: Store
    allowed_author_ids: frozenset[str] | None = None
    interval_seconds: float = 1.0
    assessment: ResilienceAssessment = field(default_factory=_normal)
    _source_shifts: dict[tuple[str, str], SourceBehaviorShift] = field(default_factory=dict)

    def current(self) -> ResilienceAssessment:
        return self.assessment

    def source_shift(self, author_id: str, channel_id: str) -> SourceBehaviorShift | None:
        return self._source_shifts.get((author_id, channel_id))

    def refresh(self) -> ResilienceAssessment:
        health = self.store.health_snapshot()
        snapshot = storage_snapshot(self.store.path)
        assessment = assess_resilience(health, wal_bytes=snapshot.wal_bytes)
        shifts: dict[tuple[str, str], SourceBehaviorShift] = {}
        suspicious: list[str] = []
        for author_id in sorted(self.allowed_author_ids or ()):
            for channel_id in sorted(ALLOWED_CHANNEL_IDS):
                recent = load_source_behavior_profile(
                    self.store.path,
                    author_id=author_id,
                    channel_id=channel_id,
                    limit=40,
                )
                baseline = load_source_behavior_profile(
                    self.store.path,
                    author_id=author_id,
                    channel_id=channel_id,
                    limit=200,
                    offset=40,
                )
                shift = compare_source_behavior(recent, baseline)
                shifts[(author_id, channel_id)] = shift
                if shift.suspicious:
                    suspicious.append(f"{author_id}:{channel_id}:{shift.score:.2f}")
        self._source_shifts = shifts

        if suspicious and assessment.mode is OperationalMode.NORMAL:
            assessment = ResilienceAssessment(
                OperationalMode.DEGRADED,
                assessment.signals
                + (
                    ResilienceSignal(
                        "source_behavior_shift",
                        SignalSeverity.WARNING,
                        ",".join(suspicious),
                    ),
                ),
                assessment.recommended_actions
                + ("force shifted source events through explicit review",),
            )
        self.assessment = assessment
        self.store.heartbeat(
            "resilience-watchdog",
            status="ok",
            mode=assessment.mode.value,
            signal_count=len(assessment.signals),
            suspicious_sources=len(suspicious),
            wal_bytes=snapshot.wal_bytes,
        )
        return assessment

    async def run(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        while True:
            try:
                self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.assessment = ResilienceAssessment(
                    OperationalMode.DEGRADED,
                    (
                        ResilienceSignal(
                            "resilience_watchdog_error",
                            SignalSeverity.WARNING,
                            type(exc).__name__,
                        ),
                    ),
                    ("force actionable events through explicit review",),
                )
                self.store.heartbeat(
                    "resilience-watchdog",
                    status="error",
                    error=type(exc).__name__,
                )
            await asyncio.sleep(self.interval_seconds)
