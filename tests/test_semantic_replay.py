from datetime import UTC, date, datetime
from decimal import Decimal

from scorpion.domain import EventKind, SignalEvent
from scorpion.semantic_replay import compare_replay_semantics


def event(kind: EventKind, event_id: str) -> SignalEvent:
    ts = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    return SignalEvent(
        event_id=event_id,
        message_id=event_id,
        kind=kind,
        channel_id="1231301953972207667",
        author_id="author",
        source_ts_utc=ts,
        received_ts_utc=ts,
        ticker="QQQ",
        option_side="CALL",
        strike=Decimal("719"),
        expiry=date(2026, 9, 8),
    )


def test_identical_replay_has_no_semantic_delta():
    events = [event(EventKind.ENTRY, "e1")]
    diff = compare_replay_semantics(events, events)
    assert diff.state_changed is False
    assert diff.added_effects == ()
    assert diff.removed_effects == ()


def test_downstream_effect_change_is_visible():
    baseline = [event(EventKind.ENTRY, "e1")]
    candidate = [event(EventKind.ENTRY, "e1"), event(EventKind.EXIT, "e2")]
    diff = compare_replay_semantics(baseline, candidate)
    assert diff.state_changed is True
    assert diff.added_effects
    assert diff.position_changes
