import asyncio

import pytest

from scorpion.pipeline import Pipeline, RuntimeHaltedError
from scorpion.store import Store
from scorpion.transactional import SQLiteTransitionCommitter


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


class HaltBeforeCommit(SQLiteTransitionCommitter):
    def commit(self, store, **kwargs):
        # A different connection wins the writer lock after Pipeline's admission
        # read, exactly the interleaving an operator/sentinel can produce.
        Store(store.path).set_halt(True, "concurrent_operator_halt")
        return super().commit(store, **kwargs)


def test_halt_committed_after_admission_blocks_the_entire_transaction(tmp_path, raw_factory):
    store = Store(tmp_path / "halt-race.db")
    pipeline = Pipeline(store, committer=HaltBeforeCommit())
    raw = raw_factory("QQQ 719C TODAY @ 1.01")

    with pytest.raises(RuntimeHaltedError, match="concurrent_operator_halt"):
        asyncio.run(pipeline.handle(raw))

    assert pipeline.state.positions == {}
    assert [row.revision_id for row in store.load_pending_raw()] == [raw.revision_id]
    assert store.load_signals() == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM proposed_effects").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0] == 0
        row = db.execute("SELECT status,error FROM raw_processing").fetchone()
        assert tuple(row) == ("PENDING", "")
    assert store.runtime_halt() == (True, "concurrent_operator_halt")


def test_recovery_halt_race_preserves_all_pending_revisions(tmp_path, raw_factory):
    store = Store(tmp_path / "recovery-race.db")
    first = raw_factory("QQQ 719C TODAY @ 1.01", message_id="first")
    second = raw_factory("All out", message_id="second", minute=1)
    store.append_raw(first)
    store.append_raw(second)

    pipeline = Pipeline(store, committer=HaltBeforeCommit())

    assert pipeline.state.positions == {}
    assert store.load_signals() == []
    assert {row.revision_id for row in store.load_pending_raw()} == {
        first.revision_id,
        second.revision_id,
    }
    assert store.runtime_halt() == (True, "concurrent_operator_halt")


def test_failure_marker_cannot_regress_a_committed_revision(tmp_path, raw_factory):
    store = Store(tmp_path / "done-is-durable.db")
    pipeline = Pipeline(store)
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    asyncio.run(pipeline.handle(raw))

    store.mark_raw_failed(raw.revision_id, "late_diagnostic_error")

    assert store.load_pending_raw() == []
    with store.connect() as db:
        row = db.execute("SELECT status,error FROM raw_processing").fetchone()
        assert tuple(row) == ("DONE", "")
