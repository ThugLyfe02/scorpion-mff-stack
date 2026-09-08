from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from decimal import Decimal

from .config import DEFAULT_POLICY, MARKET_TZ, Policy
from .domain import (
    BookState,
    Effect,
    EffectKind,
    EventKind,
    PositionState,
    PositionStatus,
    SignalEvent,
)


def _open_count(state: BookState) -> int:
    return sum(
        position.status
        in {PositionStatus.PENDING_ENTRY, PositionStatus.OPEN, PositionStatus.CLOSING}
        for position in state.positions.values()
    )


def reduce_book(
    state: BookState,
    event: SignalEvent,
    policy: Policy = DEFAULT_POLICY,
) -> tuple[BookState, tuple[Effect, ...]]:
    if event.event_id in state.seen_event_ids:
        return state, ()

    state = replace(state, seen_event_ids=state.seen_event_ids | {event.event_id})

    if event.kind is EventKind.STOP:
        return replace(state, halted=True), (
            Effect(EffectKind.HALT, None, event.event_id, 0, "operator_stop"),
        )

    if event.kind is EventKind.IGNORE:
        return state, ()

    if event.kind is EventKind.AMBIGUOUS:
        return state, (
            Effect(EffectKind.REVIEW, event.contract_key, event.event_id, 0, event.reason),
        )

    if state.halted:
        return state, (
            Effect(EffectKind.REVIEW, event.contract_key, event.event_id, 0, "system_halted"),
        )

    key = event.contract_key
    if event.kind is EventKind.ENTRY:
        if key is None:
            return state, (
                Effect(EffectKind.REVIEW, None, event.event_id, 0, "entry_missing_contract"),
            )
        existing = state.positions.get(key)
        if existing and existing.status in {
            PositionStatus.PENDING_ENTRY,
            PositionStatus.OPEN,
            PositionStatus.CLOSING,
            PositionStatus.CLOSED,
        }:
            return state, (
                Effect(
                    EffectKind.REVIEW,
                    key,
                    event.event_id,
                    existing.generation,
                    "duplicate_or_stale_entry_generation",
                ),
            )
        market_day = event.source_ts_utc.astimezone(MARKET_TZ).date()
        if policy.skip_fridays and market_day.weekday() == 4:
            return state, (
                Effect(EffectKind.REVIEW, key, event.event_id, 0, "friday_live_hone_disabled"),
            )
        if _open_count(state) >= policy.max_open_positions:
            return state, (
                Effect(EffectKind.REVIEW, key, event.event_id, 0, "max_open_positions"),
            )
        generation = 1 if existing is None else existing.generation + 1
        position = PositionState(
            contract_key=key,
            status=PositionStatus.PENDING_ENTRY,
            generation=generation,
            source_entry_message_id=event.message_id,
            source_channel_id=event.channel_id,
            source_author_id=event.author_id,
            last_source_ts_utc=event.source_ts_utc.astimezone(UTC),
            last_reason="entry_proposed",
        )
        state = state.with_position(position)
        source_day = market_day
        is_first = state.first_entry_proposed_on != source_day
        if is_first:
            state = replace(state, first_entry_proposed_on=source_day)
        return state, (
            Effect(
                EffectKind.PROPOSE_OPEN,
                key,
                event.event_id,
                generation,
                "first_entry_pipe_test" if is_first else "entry_signal",
                quantity_hint=policy.first_entry_quantity if is_first else None,
            ),
        )

    if key is None:
        return state, (
            Effect(EffectKind.REVIEW, None, event.event_id, 0, "followup_unassociated"),
        )

    maybe_position = state.positions.get(key)
    if maybe_position is None:
        return state, (
            Effect(EffectKind.REVIEW, key, event.event_id, 0, "followup_without_position"),
        )
    position = maybe_position

    if position.last_source_ts_utc and event.source_ts_utc < position.last_source_ts_utc:
        return state, (
            Effect(
                EffectKind.REVIEW,
                key,
                event.event_id,
                position.generation,
                "out_of_order_source_event",
            ),
        )

    if position.status in {PositionStatus.CLOSED, PositionStatus.FLAT}:
        return state, (
            Effect(
                EffectKind.REVIEW,
                key,
                event.event_id,
                position.generation,
                "late_followup_for_closed_generation",
            ),
        )

    if event.kind is EventKind.ADD:
        if position.added_once:
            return state, (
                Effect(
                    EffectKind.REVIEW,
                    key,
                    event.event_id,
                    position.generation,
                    "second_add_disallowed",
                ),
            )
        new_position = replace(
            position,
            added_once=True,
            last_source_ts_utc=event.source_ts_utc,
            last_reason="add_proposed",
        )
        return state.with_position(new_position), (
            Effect(EffectKind.PROPOSE_ADD, key, event.event_id, position.generation, "source_add"),
        )

    if event.kind is EventKind.TRIM:
        new_position = replace(
            position,
            last_source_ts_utc=event.source_ts_utc,
            last_reason="trim_proposed",
        )
        return state.with_position(new_position), (
            Effect(
                EffectKind.PROPOSE_TRIM,
                key,
                event.event_id,
                position.generation,
                "source_trim",
            ),
        )

    if event.kind is EventKind.EXIT:
        new_position = replace(
            position,
            status=PositionStatus.CLOSING,
            last_source_ts_utc=event.source_ts_utc,
            last_reason="exit_proposed",
        )
        return state.with_position(new_position), (
            Effect(
                EffectKind.PROPOSE_CLOSE,
                key,
                event.event_id,
                position.generation,
                "source_exit",
            ),
        )

    return state, ()


def apply_fill(
    state: BookState,
    contract_key: str,
    generation: int,
    quantity_delta: int,
    fill_price: Decimal,
    final: bool = False,
) -> BookState:
    position = state.positions[contract_key]
    if generation != position.generation:
        raise ValueError("generation mismatch")
    if final:
        return state.with_position(
            replace(
                position,
                status=PositionStatus.CLOSED,
                quantity=0,
                average_price=Decimal("0"),
                last_reason="closed_fill",
            )
        )
    if quantity_delta == 0:
        return state

    old_qty = position.quantity
    old_cost = position.average_price * old_qty
    new_qty = old_qty + quantity_delta
    if new_qty < 0:
        raise ValueError("fill would create negative quantity")

    if quantity_delta > 0:
        new_cost = old_cost + fill_price * quantity_delta
        avg = new_cost / new_qty
    else:
        avg = position.average_price if new_qty else Decimal("0")

    status = PositionStatus.OPEN if new_qty else PositionStatus.CLOSED
    return state.with_position(
        replace(position, status=status, quantity=new_qty, average_price=avg)
    )
