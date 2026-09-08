from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Mapping


@dataclass(frozen=True, slots=True)
class HealthRule:
    component: str
    max_age: timedelta


@dataclass(frozen=True, slots=True)
class HealthStatus:
    ok: bool
    failures: tuple[str, ...]


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
