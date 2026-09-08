import asyncio

import pytest

from scorpion.integrity import IntegrityLedger
from scorpion.pipeline import Pipeline
from scorpion.store import Store


class FailingCommitter:
    def commit(self, store, **kwargs):
        raise RuntimeError("commit failed")


def test_atomic_pipeline_commits_normalized_bundle(tmp_path, raw_factory):
    store = Store(tmp_path / "atomic.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    event, effects = asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))
    assert event.contract_key in pipeline.state.positions
    assert len(effects) == 1
    assert store.load_pending_raw() == []
    assert len(store.load_signals()) == 1
    health = store.health_snapshot()
    assert health["pending_review_effects"] == 1
    assert "pipeline" in health["heartbeats"]

    ledger = IntegrityLedger(store.path)
    assert len(ledger.records()) == 1
    assert ledger.verify().ok is True
    assert ledger.head_hash() != "0" * 64


def test_failed_normalized_commit_does_not_advance_memory_state(tmp_path, raw_factory):
    store = Store(tmp_path / "failure.db")
    pipeline = Pipeline(
        store,
        allowed_author_ids=frozenset({"author"}),
        committer=FailingCommitter(),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))
    assert pipeline.state.positions == {}
    assert len(store.load_pending_raw()) == 1
    assert store.load_signals() == []
