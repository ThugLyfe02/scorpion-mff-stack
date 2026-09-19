from datetime import UTC, date, datetime
from decimal import Decimal

from scorpion.domain import EventKind, SignalEvent
from scorpion.semantic_replay import compare_replay_semantics


def event(
    kind: EventKind,
    event_id: str,
    *,
    message_id: str | None = None,
) -> SignalEvent:
    ts = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    return SignalEvent(
        event_id=event_id,
        message_id=message_id or event_id,
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


def test_version_only_event_identity_change_is_not_a_semantic_delta():
    baseline = [event(EventKind.ENTRY, "parser-v1-id", message_id="same-discord-message")]
    candidate = [event(EventKind.ENTRY, "parser-v2-id", message_id="same-discord-message")]
    diff = compare_replay_semantics(baseline, candidate)
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


def test_repeated_effect_multiplicity_is_preserved():
    baseline = [
        event(EventKind.ENTRY, "e1"),
        event(EventKind.TRIM, "t1"),
    ]
    candidate = [
        event(EventKind.ENTRY, "e1"),
        event(EventKind.TRIM, "t1"),
        event(EventKind.TRIM, "t2"),
    ]
    diff = compare_replay_semantics(baseline, candidate)
    added_trims = [signature for signature in diff.added_effects if signature[0] == "PROPOSE_TRIM"]
    assert len(added_trims) == 1
