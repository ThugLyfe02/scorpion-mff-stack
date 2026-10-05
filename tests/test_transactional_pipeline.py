import asyncio

import pytest

from scorpion.pipeline import Pipeline, RuntimeHaltedError
from scorpion.store import Store
from scorpion.transactional import RawReceiptInvariantError, SQLiteTransitionCommitter


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



def test_normalized_commit_rejects_missing_raw_receipt(tmp_path, raw_factory, monkeypatch):
    store = Store(tmp_path / "missing-receipt.db")
    pipeline = Pipeline(store)
    raw = raw_factory("QQQ 719C TODAY @ 1.01")

    monkeypatch.setattr(store, "append_raw", lambda _raw: False)
    with pytest.raises(RawReceiptInvariantError, match="missing raw receipt"):
        asyncio.run(pipeline.handle(raw))

    assert store.load_signals() == []
    assert pipeline.state.positions == {}
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM proposed_effects").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0] == 0


def test_normalized_commit_rejects_raw_message_identity_tampering(
    tmp_path, raw_factory, monkeypatch
):
    store = Store(tmp_path / "receipt-identity.db")
    pipeline = Pipeline(store)
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    original_append = store.append_raw

    def append_then_tamper(message):
        inserted = original_append(message)
        with store.connect() as db:
            db.execute(
                "UPDATE raw_discord_events SET message_id='tampered' WHERE raw_event_id=?",
                (message.revision_id,),
            )
        return inserted

    monkeypatch.setattr(store, "append_raw", append_then_tamper)
    with pytest.raises(RawReceiptInvariantError, match="raw receipt message mismatch"):
        asyncio.run(pipeline.handle(raw))

    assert store.load_signals() == []
    assert [row.revision_id for row in store.load_pending_raw()] == [raw.revision_id]


def test_exact_redelivery_of_done_raw_is_idempotent(tmp_path, raw_factory):
    store = Store(tmp_path / "redelivery.db")
    pipeline = Pipeline(store)
    raw = raw_factory("QQQ 719C TODAY @ 1.01")

    first_event, first_effects = asyncio.run(pipeline.handle(raw))
    second_event, second_effects = asyncio.run(pipeline.handle(raw))

    assert first_event.event_id == second_event.event_id
    assert len(first_effects) == 1
    assert second_effects == ()
    assert len(store.load_signals()) == 1
    assert store.load_pending_raw() == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM proposed_effects").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0] == 1


def test_done_receipt_cannot_be_reinterpreted_into_new_event(tmp_path, raw_factory, monkeypatch):
    store = Store(tmp_path / "done-reinterpret.db")
    pipeline = Pipeline(store)
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    asyncio.run(pipeline.handle(raw))

    original_signal_payload = store._signal_payload

    def changed_payload(event):
        payload = original_signal_payload(event)
        return payload.replace('"reason":', '"reason_changed":', 1)

    monkeypatch.setattr(store, "_signal_payload", changed_payload)
    with pytest.raises(
        RawReceiptInvariantError,
        match="completed raw receipt does not match committed signal",
    ):
        asyncio.run(pipeline.handle(raw))

    assert len(store.load_signals()) == 1
    assert store.load_pending_raw() == []
