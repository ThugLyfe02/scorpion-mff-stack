import asyncio
import sqlite3

import pytest

from scorpion.integrity import IntegrityLedger
from scorpion.pipeline import Pipeline, RuntimeHaltedError
from scorpion.resilience import OperationalMode, ResilienceAssessment
from scorpion.store import Store
from scorpion.transactional import SQLiteTransitionCommitter


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


def test_blocked_research_state_cannot_consume_execution_capacity(tmp_path, raw_factory):
    store = Store(tmp_path / "research-isolation.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    research_channel = "1448448931116748993"

    for index, text in enumerate(
        (
            "QQQ 719C TODAY @ 1.01",
            "SPY 660C TODAY @ 1.00",
        ),
        start=1,
    ):
        _event, effects = asyncio.run(
            pipeline.handle(
                raw_factory(
                    text,
                    message_id=f"research-{index}",
                    channel_id=research_channel,
                    minute=index,
                )
            )
        )
        assert len(effects) == 1

    eligible_event, eligible_effects = asyncio.run(
        pipeline.handle(
            raw_factory(
                "AAPL 200C TODAY @ 1.01",
                message_id="eligible",
                minute=3,
            )
        )
    )
    assert len(eligible_effects) == 1
    assert eligible_effects[0].kind.value == "PROPOSE_OPEN"
    assert eligible_effects[0].reason == "first_entry_pipe_test"
    assert set(pipeline.state.positions) == {eligible_event.contract_key}
    assert "QQQ|CALL|719|2026-09-08" in pipeline.observed_state.positions
    assert "SPY|CALL|660|2026-09-08" in pipeline.observed_state.positions

    restarted = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    assert set(restarted.state.positions) == {eligible_event.contract_key}
    assert "QQQ|CALL|719|2026-09-08" in restarted.observed_state.positions


def test_halted_mode_persists_blocked_effect_and_packet_without_advancing_execution_state(
    tmp_path,
    raw_factory,
):
    store = Store(tmp_path / "halted.db")
    pipeline = Pipeline(
        store,
        allowed_author_ids=frozenset({"author"}),
        resilience_assessment=ResilienceAssessment(OperationalMode.HALTED, (), ()),
    )
    event, effects = asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))
    assert len(effects) == 1
    assert pipeline.state.positions == {}
    assert event.contract_key in pipeline.observed_state.positions

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
    assert pipeline.observed_state.positions == {}
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
