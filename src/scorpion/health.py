from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


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
