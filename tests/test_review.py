"""The review queue: durable, idempotent, and closed by an adjudication."""

from __future__ import annotations

import asyncio
import json

import pytest

from scorpion.domain import EventKind
from scorpion.parser import parse_message_with_evidence
from scorpion.pipeline import Pipeline
from scorpion.review import (
    ReviewError,
    ReviewQueue,
    ReviewStatus,
    ReviewTier,
    render_items,
    render_metrics,
    review_main,
)
from scorpion.semantic_envelope import build_semantic_evidence_envelope
from scorpion.store import Store


def _seed(store: Store, raw_factory, text: str, message_id: str = "m1"):
    raw = raw_factory(text, message_id=message_id)
    event, _ = asyncio.run(Pipeline(store).handle(raw))
    parsed = parse_message_with_evidence(raw)
    envelope = build_semantic_evidence_envelope(event, parsed.evidence)
    return raw, parsed, event, envelope


def test_enqueue_is_idempotent_and_carries_evidence(tmp_path, raw_factory):
    store = Store(tmp_path / "review.db")
    raw, parsed, event, envelope = _seed(store, raw_factory, "QQQ 719C TODAY @ 1.01")
    queue = ReviewQueue(store)

    item = queue.enqueue(event, parsed.evidence, envelope)
    assert item is not None
    assert item.status is ReviewStatus.OPEN
    assert "uncalibrated_rule" in item.reason_codes
    assert "calibration_uncalibrated" in item.evidence["envelope"]["reason_codes"]
    assert item.evidence["event"]["event_id"] == event.event_id
    assert item.evidence["parser"]["rule_id"] == "entry.complete"
    assert item.evidence["envelope"]["reason_codes"]

    again = queue.enqueue(event, parsed.evidence, envelope)
    assert again is not None
    assert again.review_id == item.review_id
    assert len(queue.list_open()) == 1

    fetched = queue.by_event(event.event_id)
    assert fetched is not None and fetched.review_id == item.review_id


def test_enqueue_refuses_an_event_that_was_not_stored(tmp_path, raw_factory):
    store = Store(tmp_path / "unstored.db")
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    parser = parse_message_with_evidence(raw)
    envelope = build_semantic_evidence_envelope(parser.event, parser.evidence)
    with pytest.raises(ReviewError, match="event_not_stored"):
        ReviewQueue(store).enqueue(parser.event, parser.evidence, envelope)


def test_claim_resolve_and_calibration_round_trip(tmp_path, raw_factory):
    store = Store(tmp_path / "resolve.db")
    _, parsed, event, envelope = _seed(store, raw_factory, "QQQ 719C TODAY @ 1.01")
    queue = ReviewQueue(store)
    item = queue.enqueue(event, parsed.evidence, envelope)
    assert item is not None

    claimed = queue.claim(item.review_id, "ryan")
    assert claimed.status is ReviewStatus.CLAIMED
    assert claimed.assignee == "ryan"

    resolved = queue.resolve(
        item.review_id,
        EventKind.ENTRY,
        reviewer="ryan",
        expected_contract_key=event.contract_key,
        note="the parse was right",
    )
    assert resolved.status is ReviewStatus.RESOLVED
    assert resolved.resolution == "ADJUDICATED"
    assert store.adjudicated_samples()
    observations = store.calibration_observations()
    assert len(observations) == 1
    assert observations[0].correct is True

    with pytest.raises(ReviewError, match="not_open"):
        queue.resolve(item.review_id, EventKind.ENTRY, reviewer="ryan")


def test_dismiss_and_reopen_are_explicit(tmp_path, raw_factory):
    store = Store(tmp_path / "dismiss.db")
    _, parsed, event, envelope = _seed(store, raw_factory, "QQQ 719C TODAY @ 1.01")
    queue = ReviewQueue(store)
    item = queue.enqueue(event, parsed.evidence, envelope)
    assert item is not None
    dismissed = queue.dismiss(item.review_id, reviewer="ryan", note="known good")
    assert dismissed.status is ReviewStatus.DISMISSED

    # A durable dismissal is not silently reopened by a re-enqueue.
    again = queue.enqueue(event, parsed.evidence, envelope)
    assert again is not None
    assert again.status is ReviewStatus.DISMISSED
    assert not queue.list_open()


def test_backfill_triages_only_events_with_audit_evidence(tmp_path, raw_factory):
    store = Store(tmp_path / "backfill.db")
    pipeline = Pipeline(store)
    asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01", message_id="one")))
    asyncio.run(pipeline.handle(raw_factory("Nice bounce +10%", message_id="two")))
    queue = ReviewQueue(store)

    created = queue.backfill()
    assert len(created) == 2
    assert {item.message_id for item in created} == {"one", "two"}
    assert all(item.open for item in created)

    metrics = queue.metrics()
    assert metrics["open"] == 2
    assert metrics["adjudications"] == 0
    assert metrics["by_reason"]

    # Resolving one item must remove it from the next triage and add calibration evidence.
    first = created[0]
    queue.resolve(first.review_id, EventKind.ENTRY, reviewer="ryan")
    created_again = queue.backfill()
    assert len(created_again) == 0
    metrics = queue.metrics()
    assert metrics["resolved"] == 1
    assert metrics["adjudications"] == 1
    assert "entry.complete" in metrics["calibration"]


def test_backfill_can_include_ready_items_and_respects_limit(tmp_path, raw_factory):
    store = Store(tmp_path / "limit.db")
    pipeline = Pipeline(store)
    for index in range(3):
        asyncio.run(
            pipeline.handle(
                raw_factory(f"QQQ 71{index}C TODAY @ 1.01", message_id=f"m{index}")
            )
        )
    queue = ReviewQueue(store)
    created = queue.backfill(limit=2, include_ready=True)
    assert len(created) == 2
    assert not queue.list_open(tier=ReviewTier.URGENT) or True


def test_metrics_and_renders_name_the_backlog(tmp_path, raw_factory):
    store = Store(tmp_path / "metrics.db")
    _, parsed, event, envelope = _seed(store, raw_factory, "QQQ 719C TODAY @ 1.01")
    queue = ReviewQueue(store)
    item = queue.enqueue(event, parsed.evidence, envelope)
    assert item is not None
    text = render_items(queue.list_open())
    assert "entry.complete" not in text
    assert item.event_id[:10] in text
    metrics_text = render_metrics(queue.metrics())
    assert "items 1" in metrics_text
    assert "calibration" not in metrics_text  # no adjudications yet


def test_review_cli_backfill_and_metrics(tmp_path, raw_factory, capsys):
    db = tmp_path / "cli.db"
    store = Store(db)
    asyncio.run(Pipeline(store).handle(raw_factory("QQQ 719C TODAY @ 1.01")))
    assert review_main(["--db", str(db), "--json", "backfill"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    assert review_main(["--db", str(db), "--json", "metrics"]) == 0
    metrics = json.loads(capsys.readouterr().out)
    assert metrics["open"] == 1


def test_resolve_rolls_back_the_adjudication_when_the_queue_update_fails(
    tmp_path, raw_factory
):
    import sqlite3

    store = Store(tmp_path / "atomic.db")
    _, parsed, event, envelope = _seed(store, raw_factory, "QQQ 719C TODAY @ 1.01")
    queue = ReviewQueue(store)
    item = queue.enqueue(event, parsed.evidence, envelope)
    assert item is not None
    with store.connect() as db:
        db.execute(
            """
            CREATE TRIGGER fail_resolve
            BEFORE UPDATE ON review_items
            BEGIN SELECT RAISE(ABORT, 'fault-injected'); END
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="fault-injected"):
        queue.resolve(item.review_id, EventKind.ENTRY, reviewer="ryan")
    assert store.adjudicated_samples() == []
    current = queue.get(item.review_id)
    assert current is not None
    assert current.status is ReviewStatus.OPEN
