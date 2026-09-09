import asyncio
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from scorpion.causal_features import (
    backfill_feature_snapshots,
    build_and_persist_features,
    build_point_in_time_features,
    load_training_rows,
    verify_feature_store,
)
from scorpion.domain import EventKind
from scorpion.failure_quarantine import (
    QuarantinedRawRevision,
    load_quarantined,
    requeue_quarantined,
)
from scorpion.fault_certification import certify_replay_faults
from scorpion.pipeline import Pipeline
from scorpion.processing_order import (
    inspect_processing_order,
    load_signals_in_processing_order,
)
from scorpion.replay import ReplayOrder, replay, state_fingerprint
from scorpion.store import Store


class AlwaysFailCommitter:
    def commit(self, *args, **kwargs):
        del args, kwargs
        raise RuntimeError("synthetic commit failure")


def test_live_process_order_survives_out_of_order_source_timestamps(tmp_path, raw_factory):
    store = Store(tmp_path / "order.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    first_raw = raw_factory("AAPL 200C TODAY @ 1.00", message_id="first")
    second_raw = raw_factory("NVDA 200P Sep 11 @ 1.00", message_id="second", minute=1)
    second_raw = replace(
        second_raw,
        source_ts_utc=first_raw.source_ts_utc - timedelta(seconds=1),
        received_ts_utc=first_raw.received_ts_utc + timedelta(seconds=1),
    )

    first_event, _ = asyncio.run(pipeline.handle(first_raw))
    second_event, _ = asyncio.run(pipeline.handle(second_raw))
    persisted = load_signals_in_processing_order(store.path)

    assert [event.event_id for event in persisted] == [first_event.event_id, second_event.event_id]
    report = inspect_processing_order(store.path)
    assert report.complete is True
    assert report.signal_count == 2

    restarted = Pipeline(Store(store.path), allowed_author_ids=frozenset({"author"}))
    assert state_fingerprint(restarted.state) == state_fingerprint(pipeline.state)


def test_fault_certification_reports_when_source_time_order_would_change_state(
    tmp_path,
    raw_factory,
):
    store = Store(tmp_path / "order-sensitive.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    entry = raw_factory("AAPL 200C TODAY @ 1.00", message_id="entry")
    stop = raw_factory("STOP", message_id="stop", minute=1)
    stop = replace(
        stop,
        source_ts_utc=entry.source_ts_utc - timedelta(seconds=1),
        received_ts_utc=entry.received_ts_utc + timedelta(seconds=1),
    )
    asyncio.run(pipeline.handle(entry))
    asyncio.run(pipeline.handle(stop))

    report = certify_replay_faults(store.path)
    assert report.passed is True
    assert report.source_time_differs is True


def test_causal_features_exclude_current_decision_label_and_use_only_prior_rows(
    tmp_path,
    raw_factory,
):
    store = Store(tmp_path / "features.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    first, _ = asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00", message_id="1")))
    second, _ = asyncio.run(
        pipeline.handle(raw_factory("NVDA 200P Sep 11 @ 1.00", message_id="2", minute=1))
    )

    first_features = build_point_in_time_features(store.path, first.event_id)
    second_features = build_point_in_time_features(store.path, second.event_id)
    assert first_features.features["prior_events"] == 0
    assert second_features.features["prior_events"] == 1
    assert "event_kind" not in first_features.features
    assert "event_kind" not in second_features.features

    build_and_persist_features(store.path, first.event_id)
    store.record_adjudication(first.event_id, EventKind.ENTRY, reviewer="reviewer")
    rows = load_training_rows(store.path)
    assert len(rows) == 1
    assert rows[0].expected_kind == EventKind.ENTRY.value
    assert "event_kind" not in rows[0].features


def test_feature_backfill_is_idempotent_and_tampering_is_detected(tmp_path, raw_factory):
    store = Store(tmp_path / "feature-integrity.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    event, _ = asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00")))

    first = backfill_feature_snapshots(store.path)
    second = backfill_feature_snapshots(store.path)
    assert first.created == 1
    assert second.created == 0
    assert second.existing == 1
    assert verify_feature_store(store.path).valid is True

    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE causal_feature_snapshots SET feature_json='{}' WHERE event_id=?",
            (event.event_id,),
        )
        db.commit()
    verification = verify_feature_store(store.path)
    assert verification.valid is False
    assert verification.hash_mismatches == 1


def test_poison_revision_quarantines_after_retry_budget_and_can_be_requeued(
    tmp_path,
    raw_factory,
):
    store = Store(tmp_path / "quarantine.db")
    pipeline = Pipeline(
        store,
        allowed_author_ids=frozenset({"author"}),
        committer=AlwaysFailCommitter(),
        maximum_raw_attempts=2,
    )
    raw = raw_factory("AAPL 200C TODAY @ 1.00", message_id="poison")

    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        asyncio.run(pipeline.handle(raw))
    with pytest.raises(QuarantinedRawRevision):
        asyncio.run(pipeline.handle(raw))

    items = load_quarantined(store.path)
    assert len(items) == 1
    assert items[0].attempt_count == 2
    with sqlite3.connect(store.path) as db:
        status = db.execute(
            "SELECT status FROM raw_processing WHERE raw_event_id=?",
            (raw.revision_id,),
        ).fetchone()[0]
    assert status == "QUARANTINED"

    requeue_quarantined(
        store.path,
        raw.revision_id,
        operator="reviewer",
        note="code defect fixed",
    )
    assert load_quarantined(store.path) == ()
    with sqlite3.connect(store.path) as db:
        status = db.execute(
            "SELECT status FROM raw_processing WHERE raw_event_id=?",
            (raw.revision_id,),
        ).fetchone()[0]
    assert status == "PENDING"


def test_fault_certification_preserves_duplicate_and_split_replay_invariants(
    tmp_path,
    raw_factory,
):
    store = Store(tmp_path / "faults.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00", message_id="a")))
    asyncio.run(
        pipeline.handle(raw_factory("NVDA 200P Sep 11 @ 1.00", message_id="b", minute=1))
    )
    backfill_feature_snapshots(store.path)

    report = certify_replay_faults(store.path)
    assert report.passed is True
    assert report.signal_count == 2
    assert all(probe.passed for probe in report.probes)

    signals = load_signals_in_processing_order(store.path)
    state, _ = replay(signals, order=ReplayOrder.INPUT)
    assert report.baseline_fingerprint == state_fingerprint(state)
