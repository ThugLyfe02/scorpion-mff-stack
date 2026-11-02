from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .config import StalePolicy


class QuoteAges(Protocol):
    @property
    def last_message_mono_ns(self) -> int | None: ...

    def age_ms(self, symbol: str, *, now_mono_ns: int | None = None) -> float | None: ...


@dataclass(frozen=True, slots=True)
class HealthRule:
    component: str
    max_age: timedelta


@dataclass(frozen=True, slots=True)
class HealthStatus:
    ok: bool
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DecisionHealthRule:
    max_ambiguity_rate: float = 0.35
    max_low_confidence_rate: float = 0.20
    max_unresolved_association_rate: float = 0.20
    max_parser_p95_us: float = 2_000.0
    max_pipeline_p95_us: float = 20_000.0
    min_samples: int = 20


def evaluate_heartbeats(
    snapshot: Mapping[str, Mapping[str, object]],
    rules: tuple[HealthRule, ...],
    now: datetime | None = None,
) -> HealthStatus:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    failures: list[str] = []
    for rule in rules:
        item = snapshot.get(rule.component)
        if item is None:
            failures.append(f"{rule.component}:missing")
            continue
        raw = item.get("last_seen_ts_utc")
        if not isinstance(raw, str):
            failures.append(f"{rule.component}:invalid_timestamp")
            continue
        ts = datetime.fromisoformat(raw).astimezone(UTC)
        age = now - ts
        if age > rule.max_age:
            failures.append(f"{rule.component}:stale:{age.total_seconds():.3f}s")
    return HealthStatus(ok=not failures, failures=tuple(failures))


def evaluate_decision_health(
    metrics: Mapping[str, float | int],
    rule: DecisionHealthRule | None = None,
) -> HealthStatus:
    rule = rule or DecisionHealthRule()
    count = int(metrics.get("count", 0))
    if count < rule.min_samples:
        return HealthStatus(ok=True, failures=())

    failures: list[str] = []
    checks = (
        ("ambiguity_rate", rule.max_ambiguity_rate),
        ("low_confidence_rate", rule.max_low_confidence_rate),
        ("unresolved_association_rate", rule.max_unresolved_association_rate),
        ("parser_p95_us", rule.max_parser_p95_us),
        ("pipeline_p95_us", rule.max_pipeline_p95_us),
    )
    for key, maximum in checks:
        value = float(metrics.get(key, 0.0))
        if value > maximum:
            failures.append(f"{key}:{value:.6f}>{maximum:.6f}")
    return HealthStatus(ok=not failures, failures=tuple(failures))


# --------------------------------------------------------------------------------------------
# exec-core feed health (monotonic clock, milliseconds, session-scoped). Thresholds live only in
# scorpion.config.StalePolicy.
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeedHealth:
    latch_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    feed_age_ms: float | None

    @property
    def ok(self) -> bool:
        return not self.latch_reasons


class FeedHealthMonitor:
    """Turns quote-cache ages into latch reasons (feed silence, persistent skew) and warnings.

    Only symbols with an open paper position or a pending call are checked for per-symbol silence;
    a quiet small-cap with nothing working is normal and is not an alarm.
    """

    def __init__(self, cache: QuoteAges, policy: StalePolicy, *, started_mono_ns: int) -> None:
        self._cache = cache
        self._policy = policy
        self._started_mono_ns = started_mono_ns
        self._skew_since_mono_ns: int | None = None
        self._last_skew_ms = 0.0

    def observe_skew(self, skew_ms: float, now_mono_ns: int) -> None:
        self._last_skew_ms = skew_ms
        if abs(skew_ms) > self._policy.clock_skew_ms:
            if self._skew_since_mono_ns is None:
                self._skew_since_mono_ns = now_mono_ns
        else:
            self._skew_since_mono_ns = None

    def evaluate(
        self,
        *,
        now_mono_ns: int,
        session_open: bool,
        watched: Iterable[str] = (),
    ) -> FeedHealth:
        last = self._cache.last_message_mono_ns
        baseline = last if last is not None else self._started_mono_ns
        feed_age_ms = (now_mono_ns - baseline) / 1_000_000.0
        if not session_open:
            return FeedHealth((), (), feed_age_ms)
        latch: list[str] = []
        warn: list[str] = []
        if feed_age_ms > self._policy.feed_silence_ms:
            latch.append(f"feed_silence_ms={feed_age_ms:.0f}>{self._policy.feed_silence_ms}")
        for symbol in sorted(set(watched)):
            age = self._cache.age_ms(symbol, now_mono_ns=now_mono_ns)
            if age is None or age > self._policy.symbol_silence_ms:
                shown = "none" if age is None else f"{age:.0f}"
                warn.append(f"symbol_silence:{symbol}:age_ms={shown}")
        if self._skew_since_mono_ns is not None:
            persisted = (now_mono_ns - self._skew_since_mono_ns) / 1_000_000.0
            message = f"clock_skew_ms={self._last_skew_ms:.0f} for {persisted:.0f}ms"
            if persisted >= self._policy.clock_skew_persist_ms:
                latch.append(message)
            else:
                warn.append(message)
        return FeedHealth(tuple(latch), tuple(warn), feed_age_ms)
