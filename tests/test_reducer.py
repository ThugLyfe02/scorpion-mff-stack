from datetime import UTC, date, datetime
from decimal import Decimal

from scorpion.domain import BookState, EffectKind, EventKind, SignalEvent
from scorpion.reducer import apply_fill, reduce_book


def event(kind: EventKind, eid: str = "e1") -> SignalEvent:
    ts = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    return SignalEvent(
        event_id=eid,
        message_id=eid,
        kind=kind,
        channel_id="1231301953972207667",
        author_id="a",
        source_ts_utc=ts,
        received_ts_utc=ts,
        ticker="QQQ",
        option_side="CALL",
        strike=Decimal("719"),
        expiry=date(2026, 9, 8),
    )


def test_duplicate_event_is_idempotent():
    state, effects1 = reduce_book(BookState(), event(EventKind.ENTRY))
    state2, effects2 = reduce_book(state, event(EventKind.ENTRY))
    assert effects1[0].kind is EffectKind.PROPOSE_OPEN
    assert effects2 == ()
    assert state2 == state


def test_closed_generation_rejects_late_entry():
    state, _ = reduce_book(BookState(), event(EventKind.ENTRY))
    key = event(EventKind.ENTRY).contract_key
    assert key
    state = apply_fill(state, key, 1, 1, Decimal("1.00"))
    state = apply_fill(state, key, 1, -1, Decimal("1.10"), final=True)
    state2, effects = reduce_book(state, event(EventKind.ENTRY, "late"))
    assert state2.positions[key].status.value == "CLOSED"
    assert effects[0].kind is EffectKind.REVIEW


def test_reduction_preserves_average_cost():
    state, _ = reduce_book(BookState(), event(EventKind.ENTRY))
    key = event(EventKind.ENTRY).contract_key
    assert key
    state = apply_fill(state, key, 1, 10, Decimal("1.00"))
    state = apply_fill(state, key, 1, -5, Decimal("1.50"))
    assert state.positions[key].quantity == 5
    assert state.positions[key].average_price == Decimal("1.00")
    state = apply_fill(state, key, 1, 5, Decimal("0.80"))
    assert state.positions[key].quantity == 10
    assert state.positions[key].average_price == Decimal("0.90")
