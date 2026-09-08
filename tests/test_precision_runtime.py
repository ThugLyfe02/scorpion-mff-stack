import asyncio

from scorpion.domain import EventKind
from scorpion.pipeline import Pipeline
from scorpion.replay import state_fingerprint
from scorpion.store import Store


def test_raw_revision_id_changes_on_edit(raw_factory):
    original = raw_factory("QQQ 719C TODAY @ 1.01", message_id="m1")
    edited = raw_factory("QQQ 719C TODAY @ 1.05", message_id="m1", edited=True)
    assert original.revision_id != edited.revision_id


def test_parser_event_id_changes_on_edit(raw_factory):
    from scorpion.parser import parse_message

    original = parse_message(raw_factory("QQQ 719C TODAY @ 1.01", message_id="m2"))
    edited = parse_message(
        raw_factory("QQQ 719C TODAY @ 1.05", message_id="m2", edited=True)
    )
    assert original.event_id != edited.event_id


def test_pending_raw_is_recovered_after_crash_boundary(tmp_path, raw_factory):
    store = Store(tmp_path / "recovery.db")
    raw = raw_factory("QQQ 719C TODAY @ 1.01", message_id="crash-boundary")
    store.append_raw(raw)
    assert store.health_snapshot()["pending_raw_revisions"] == 1

    recovered = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    assert raw.message_id in {event.message_id for event in store.load_signals()}
    assert store.health_snapshot()["pending_raw_revisions"] == 0
    assert recovered.state.positions


def test_live_and_replayed_state_fingerprints_match(tmp_path, raw_factory):
    store = Store(tmp_path / "fingerprint.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    raw = raw_factory("QQQ 719C TODAY @ 1.01", message_id="fp")
    asyncio.run(pipeline.handle(raw))
    first = state_fingerprint(pipeline.state)

    recovered = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    assert state_fingerprint(recovered.state) == first


def test_conflicting_action_is_review_not_action(raw_factory):
    from scorpion.parser import parse_message_with_evidence

    decision = parse_message_with_evidence(raw_factory("Added earlier; all out now"))
    assert decision.event.kind is EventKind.AMBIGUOUS
    assert decision.evidence.conflicts == ("ADD", "EXIT")
    assert decision.evidence.confidence < 0.5


def test_unicode_normalization_does_not_change_entry(raw_factory):
    from scorpion.parser import parse_message

    event = parse_message(raw_factory("ＱＱＱ ７１９Ｃ ０DTE @ １.０１"))
    assert event.kind is EventKind.ENTRY
    assert event.ticker == "QQQ"
