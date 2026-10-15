"""Latency budget and deadman heartbeat.

The 9/8 failure was a 31-minute queue lag and a deadman job that starved the
queue for 579s while only 2 of 15 five-minute slots fired. Both are *detectable*
in an event-driven loop, so both are measured here rather than assumed.

  * every handled event records receive->decision latency; the budget is
    sub-10s (the hone is a fast-moving 0DTE desk)
  * a heartbeat file is touched on every event and every poll, so an external
    watchdog can see staleness without an LLM in the path
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_BUDGET_MS = 10_000          # sub-10s, per the handoff
DEADMAN_GAP_S = 30.0                # the */5 deadman cadence, tightened


@dataclass
class LatencyBudget:
    budget_ms: int = DEFAULT_BUDGET_MS
    samples: list[int] = field(default_factory=list)
    breaches: int = 0

    def record(self, latency_ms: int) -> bool:
        """Returns True when the sample is within budget."""
        self.samples.append(latency_ms)
        ok = latency_ms <= self.budget_ms
        if not ok:
            self.breaches += 1
        return ok

    @property
    def worst_ms(self) -> int:
        return max(self.samples) if self.samples else 0

    def percentile(self, pct: float) -> int:
        if not self.samples:
            return 0
        ordered = sorted(self.samples)
        idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
        return ordered[idx]

    def summary(self) -> dict[str, int]:
        return {
            "n": len(self.samples),
            "worst_ms": self.worst_ms,
            "p95_ms": self.percentile(95),
            "budget_ms": self.budget_ms,
            "breaches": self.breaches,
        }


@dataclass
class Heartbeat:
    """Touches a file so an external watchdog can tell live from wedged."""

    path: Path = field(default_factory=lambda: Path(
        os.environ.get("MFF_HEARTBEAT", "live/heartbeat.json")))
    gap_s: float = DEADMAN_GAP_S
    clock: Callable[[], float] = time.time
    last_event_ts: float = field(default_factory=lambda: time.time())
    beats: int = 0

    def beat(self, now: float | None = None, note: str = "") -> None:
        self.last_event_ts = self.clock() if now is None else now
        self.beats += 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                f'{{"ts": {self.last_event_ts}, "beats": {self.beats}, "note": "{note}"}}\n',
                encoding="utf-8",
            )
        except OSError:
            pass        # a watchdog file that cannot be written must not stop trading

    def stale(self, now: float | None = None, gap_s: float | None = None) -> bool:
        gap = self.gap_s if gap_s is None else gap_s
        return (self.clock() if now is None else now) - self.last_event_ts > gap

    def staleness_s(self, now: float | None = None) -> float:
        return (self.clock() if now is None else now) - self.last_event_ts
