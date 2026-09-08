from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .domain import Effect, PositionState


@dataclass(frozen=True, slots=True)
class ApprovalEnvelope:
    effect: Effect
    approved_by: str
    approved_ts_utc: datetime
    observed_quote_ts_utc: datetime


def validate_approval(
    envelope: ApprovalEnvelope,
    position: PositionState,
    *,
    max_quote_age: timedelta = timedelta(seconds=3),
    now: datetime | None = None,
) -> None:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if not envelope.approved_by.strip():
        raise ValueError("approver identity required")
    if envelope.effect.generation != position.generation:
        raise ValueError("stale generation")
    if envelope.approved_ts_utc > now:
        raise ValueError("approval timestamp is in the future")
    quote_age = now - envelope.observed_quote_ts_utc.astimezone(UTC)
    if quote_age < timedelta(0) or quote_age > max_quote_age:
        raise ValueError("quote is stale")
