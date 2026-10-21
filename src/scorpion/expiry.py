"""Expiry: a position whose contract has expired must stop holding a concurrency slot.

`max_open_positions` is a *live risk* limit, so a contract that no longer exists must not
count against it. Without this sweep one missed exit - the channel stops posting, the
message is edited, a reconnect swallows it - leaves a phantom position in `OPEN` forever
and the hone silently stops trading, with `max_open_positions` as the only clue.

The sweep is deliberately conservative about what it will release:

* an **ISO expiry** (`2026-09-08`) earlier than today -> released, because the contract
  provably cannot exist;
* anything the key cannot parse exactly -> **not** released, and reported, because freeing
  a live slot on a guess under-counts risk, which is worse than a stuck position.

A release is never silent: it marks the position CLOSED with a reason and emits a REVIEW
effect carrying an idempotent synthetic source id, so the operator sees it in the outbox
and the release survives replay.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from .domain import BookState, Effect, EffectKind, PositionStatus

LIVE_STATUSES = frozenset(
    {PositionStatus.PENDING_ENTRY, PositionStatus.OPEN, PositionStatus.CLOSING}
)


class ExpiryError(ValueError):
    """A position key that cannot be reasoned about. Never silently ignored."""


def expiry_of(contract_key: str) -> date | None:
    """The expiry encoded in a contract key (`TICKER|SIDE|STRIKE|YYYY-MM-DD`)."""
    parts = contract_key.split("|")
    if len(parts) != 4:
        return None
    try:
        return date.fromisoformat(parts[3])
    except ValueError:
        return None


def unparseable_keys(state: BookState) -> tuple[str, ...]:
    """Live positions whose expiry cannot be read - the ones a human must look at."""
    return tuple(
        sorted(
            key
            for key, position in state.positions.items()
            if position.status in LIVE_STATUSES and expiry_of(key) is None
        )
    )


def expire_due(
    state: BookState, market_day: date
) -> tuple[BookState, tuple[Effect, ...]]:
    """Close and report live positions whose expiry is strictly before `market_day`."""
    updated = state
    effects: list[Effect] = []

    for key in sorted(state.positions):
        position = state.positions[key]
        if position.status not in LIVE_STATUSES:
            continue
        expiry = expiry_of(key)
        if expiry is None or expiry >= market_day:
            continue
        updated = updated.with_position(
            replace(
                position,
                status=PositionStatus.CLOSED,
                quantity=0,
                average_price=position.average_price,
                last_reason=f"expired:{expiry.isoformat()}",
            )
        )
        effects.append(
            Effect(
                kind=EffectKind.REVIEW,
                contract_key=key,
                source_event_id=f"expiry:{key}:{expiry.isoformat()}",
                generation=position.generation,
                reason=f"expired_position_released:{expiry.isoformat()}",
            )
        )

    return updated, tuple(effects)


def expire_on_roll(
    state: BookState,
    previous_day: date | None,
    market_day: date,
) -> tuple[BookState, tuple[Effect, ...]]:
    """Sweep only when the market day actually advanced.

    Called from the pipeline for every message; a no-op unless the day changed, so the
    sweep cannot release a position mid-session by accident.
    """
    if previous_day is not None and market_day <= previous_day:
        return state, ()
    return expire_due(state, market_day)
