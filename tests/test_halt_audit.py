"""A halt is a state change: it must be in the log, and a day must be exportable and checkable."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from scorpion.audit import build, verify_bundle
from scorpion.pipeline import Pipeline
from scorpion.scenario import message
from scorpion.store import Store
from scorpion.verify import replay_applied, verify

D = Decimal
START = datetime.now(UTC).replace(microsecond=0)


def test_a_halt_is_written_to_the_applied_log(tmp_path):
    store = Store(tmp_path / "s.db")
    pipeline = Pipeline(store=store)
    asyncio.run(pipeline.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))

    assert pipeline.halt("drawdown breach", now=START) is True
    kinds = store.applied_counts()
    assert kinds.get("halt") == 1
    assert store.halt_state()[0] is True


def test_a_replay_reproduces_a_halted_book(tmp_path):
    """Without a halt row the log said 'trading' while the book said 'stopped'."""
    store = Store(tmp_path / "s.db")
    pipeline = Pipeline(store=store)
    asyncio.run(pipeline.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))
    pipeline.halt("operator", now=START)

    replayed = replay_applied(store.load_applied())
    assert replayed.halted is True
    assert verify(store, pipeline.state).ok


def test_re_arming_is_recorded_too(tmp_path):
    store = Store(tmp_path / "s.db")
    pipeline = Pipeline(store=store)
    pipeline.halt("operator", now=START)
    assert pipeline.clear_halt(now=START) is True
    assert replay_applied(store.load_applied()).halted is False
    assert verify(store, pipeline.state).ok


def test_halting_twice_is_idempotent(tmp_path):
    store = Store(tmp_path / "s.db")
    pipeline = Pipeline(store=store)
    assert pipeline.halt("first", now=START) is True
    assert pipeline.halt("second", now=START) is False
    assert store.applied_counts().get("halt") == 1


def test_a_bundle_records_counts_and_verifies(tmp_path):
    store = Store(tmp_path / "s.db")
    pipeline = Pipeline(store=store)
    asyncio.run(pipeline.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))

    bundle = build(store, pipeline.state, START.date())
    assert bundle.size > 0
    result = verify_bundle(bundle.payload_json, expected_digest=bundle.digest)
    assert result["ok"], result["problems"]
    assert result["counts"]["signals"] == 1


def test_a_tampered_bundle_is_refused(tmp_path):
    store = Store(tmp_path / "s.db")
    pipeline = Pipeline(store=store)
    asyncio.run(pipeline.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))
    bundle = build(store, pipeline.state, START.date())

    tampered = bundle.payload_json.replace('"signals":1', '"signals":2')
    assert verify_bundle(tampered, expected_digest=bundle.digest)["ok"] is False


def test_a_bundle_carries_the_verification_result(tmp_path):
    store = Store(tmp_path / "s.db")
    pipeline = Pipeline(store=store)
    asyncio.run(pipeline.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))
    payload = build(store, pipeline.state, START.date()).payload_json
    assert '"verification"' in payload and '"ok":true' in payload
