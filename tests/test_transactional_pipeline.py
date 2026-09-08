import asyncio
import sqlite3

import pytest

from scorpion.integrity import IntegrityLedger
from scorpion.pipeline import Pipeline
from scorpion.resilience import OperationalMode, ResilienceAssessment
from scorpion.store import Store


class FailingCommitter:
    def commit(self, store, **kwargs):
        raise RuntimeError("commit failed")


def test_atomic_pipeline_commits_normalized_bundle(tmp_path, raw_factory):
    store = Store(tmp_path / "atomic.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    event, effects = asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.01")))
    assert event.contract_key in pipeline.state.positions
    assert len(effects) == 1
    assert store.load_pending_raw() == []
    assert len(store.load_signals()) == 1
    health = store.health_snapshot()
    assert health["pending_review_effects"] == 1
    assert "pipeline" in health["heartbeats"]

    db = sqlite3.connect(store.path)
    try:
        packet = db.execute(
            "SELECT disposition FROM operator_decision_packets WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
    finally:
        db.close()
    assert packet == ("READY_FOR_OPERATOR_REVIEW",)

    ledger = IntegrityLedger(store.path)
    assert len(ledger.records()) == 1
    assert ledger.verify().ok is True
    assert ledger.verify_database().ok is True
    assert ledger.head_hash() != "0" * 64


def test_halted_mode_persists_blocked_effect_and_packet(tmp_path, raw_factory):
    store = Store(tmp_path / "halted.db")
    pipeline = Pipeline(
        store,
        allowed_author_ids=frozenset({"author"}),
        resilience_assessment=ResilienceAssessment(OperationalMode.HALTED, (), ()),
    )
    event, effects = asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))
    assert len(effects) == 1

    db = sqlite3.connect(store.path)
    try:
        effect_status = db.execute(
            "SELECT status FROM proposed_effects WHERE source_event_id=?",
            (event.event_id,),
        ).fetchone()
        packet = db.execute(
            "SELECT disposition,system_mode FROM operator_decision_packets WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
    finally:
        db.close()
    assert effect_status == ("BLOCKED_SYSTEM",)
    assert packet == ("BLOCKED_SYSTEM", "HALTED")
    assert store.health_snapshot()["pending_review_effects"] == 0
    assert IntegrityLedger(store.path).verify_database().ok is True


def test_failed_normalized_commit_does_not_advance_memory_state(tmp_path, raw_factory):
    store = Store(tmp_path / "failure.db")
    pipeline = Pipeline(
        store,
        allowed_author_ids=frozenset({"author"}),
        committer=FailingCommitter(),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.01")))
    assert pipeline.state.positions == {}
    assert len(store.load_pending_raw()) == 1
    assert store.load_signals() == []
