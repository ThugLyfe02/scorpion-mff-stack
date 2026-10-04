from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_DISCORD_EPOCH_MS = 1420070400000
_TIMESTAMP_SHIFT = 22


@dataclass(frozen=True, slots=True)
class SnowflakeAssessment:
    available: bool
    snowflake_ts_utc: datetime | None
    delta_from_api_ts: timedelta | None
    consistent: bool


def decode_snowflake_timestamp(message_id: str) -> datetime | None:
    """Decode Discord's timestamp-bearing snowflake ID without a network call.

    Synthetic/non-numeric IDs are common in tests and historical fixtures. Numeric fixtures such
    as ``1`` also exist; they contain no nonzero Discord timestamp field and are intentionally
    treated as unavailable rather than as messages created exactly at Discord's epoch.
    """
    if not message_id.isdigit():
        return None
    value = int(message_id)
    timestamp_component = value >> _TIMESTAMP_SHIFT
    if timestamp_component <= 0:
        return None
    timestamp_ms = timestamp_component + _DISCORD_EPOCH_MS
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def assess_snowflake_timestamp(
    message_id: str,
    api_ts_utc: datetime,
    *,
    tolerance: timedelta = timedelta(seconds=1),
) -> SnowflakeAssessment:
    if tolerance < timedelta(0):
        raise ValueError("tolerance cannot be negative")
    snowflake = decode_snowflake_timestamp(message_id)
    if snowflake is None:
        return SnowflakeAssessment(False, None, None, True)
    api_ts = api_ts_utc.astimezone(UTC)
    delta = api_ts - snowflake
    return SnowflakeAssessment(
        available=True,
        snowflake_ts_utc=snowflake,
        delta_from_api_ts=delta,
        consistent=abs(delta) <= tolerance,
    )
