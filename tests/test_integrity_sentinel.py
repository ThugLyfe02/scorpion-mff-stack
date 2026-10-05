import asyncio
from dataclasses import replace

import pytest

from scorpion.integrity import ReplayIntegritySentinel, audit_durable_integrity
from scorpion.pipeline import Pipeline, RuntimeHaltedError
from scorpion.store import Store


def test_replay_integrity_sentinel_latches_halt_on_divergence(tmp_path):
    store = Store(tmp_path / "integrity.db")
    pipeline = Pipeline(store)
    divergent_state = replace(pipeline.state, halted=True)
    sentinel = ReplayIntegritySentinel(every_n_commits=64)

    result = sentinel.after_commit(store, divergent_state, force=True)

    assert result is not None
    assert result.ok is False
    assert result.live_fingerprint != result.durable_fingerprint
    assert result.durable_event_count == 0
    assert store.runtime_halt() == (True, "replay_integrity_divergence")
    heartbeat = store.health_snapshot()["heartbeats"]["replay-integrity"]
    assert heartbeat["metadata"]["status"] == "diverged"


def test_replay_integrity_sentinel_is_cadenced(tmp_path):
    store = Store(tmp_path / "cadence.db")
    pipeline = Pipeline(store)
    sentinel = ReplayIntegritySentinel(every_n_commits=2)

    assert sentinel.after_commit(store, pipeline.state) is None
    result = sentinel.after_commit(store, pipeline.state)

    assert result is not None
    assert result.ok is True
    assert store.runtime_halt() == (False, "")


def test_latched_halt_keeps_new_raw_durable_but_blocks_normalization(tmp_path, raw_factory):
    store = Store(tmp_path / "halted.db")
    store.set_halt(True, "operator_guardrail")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    raw = raw_factory("QQQ 719C TODAY @ 1.01")

    with pytest.raises(RuntimeHaltedError, match="operator_guardrail"):
        asyncio.run(pipeline.handle(raw))

    pending = store.load_pending_raw()
    assert [row.revision_id for row in pending] == [raw.revision_id]
    assert store.load_signals() == []
    assert pipeline.state.positions == {}


def test_pending_recovery_does_not_run_while_halt_is_latched(tmp_path, raw_factory):
    store = Store(tmp_path / "recovery-halt.db")
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    store.append_raw(raw)
    store.set_halt(True, "manual_hold")

    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))

    assert pipeline.state.positions == {}
    assert [row.revision_id for row in store.load_pending_raw()] == [raw.revision_id]
    assert store.load_signals() == []


def test_store_reasserts_connection_scoped_sqlite_safety_pragmas(tmp_path):
    store = Store(tmp_path / "pragmas.db")

    with store.connect() as db:
        foreign_keys = db.execute("PRAGMA foreign_keys").fetchone()[0]
        synchronous = db.execute("PRAGMA synchronous").fetchone()[0]
        busy_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]

    assert foreign_keys == 1
    assert synchronous == 2
    assert busy_timeout == 5000


def test_divergence_is_durably_latched_before_telemetry_failure(tmp_path, monkeypatch):
    store = Store(tmp_path / "telemetry-failure.db")
    pipeline = Pipeline(store)
    sentinel = ReplayIntegritySentinel(every_n_commits=1)

    def fail_heartbeat(*args, **kwargs):
        assert Store(store.path).runtime_halt() == (True, "replay_integrity_divergence")
        raise RuntimeError("diagnostic write failed")

    monkeypatch.setattr(store, "heartbeat", fail_heartbeat)
    with pytest.raises(RuntimeError, match="diagnostic write failed"):
        sentinel.after_commit(store, replace(pipeline.state, halted=True))

    assert Store(store.path).runtime_halt() == (True, "replay_integrity_divergence")


@pytest.mark.parametrize("failure_site", ["replay_read", "heartbeat"])
def test_post_commit_check_failure_preserves_done_and_halts_new_work(
    tmp_path, raw_factory, monkeypatch, failure_site
):
    store = Store(tmp_path / f"post-commit-{failure_site}.db")
    pipeline = Pipeline(store, integrity_sentinel=ReplayIntegritySentinel(every_n_commits=1))
    raw = raw_factory("QQQ 719C TODAY @ 1.01")

    def fail(*args, **kwargs):
        raise RuntimeError("post-commit check failed")

    with monkeypatch.context() as patch:
        patch.setattr(store, "load_signals" if failure_site == "replay_read" else "heartbeat", fail)
        with pytest.raises(RuntimeError, match="post-commit check failed"):
            asyncio.run(pipeline.handle(raw))

    assert len(store.load_signals()) == 1
    assert pipeline.state.positions
    assert store.load_pending_raw() == []
    assert store.runtime_halt()[0] is True
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM proposed_effects").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0] == 1
        assert db.execute("SELECT status FROM raw_processing").fetchone()[0] == "DONE"

    later = raw_factory("All out", message_id="later", minute=1)
    with pytest.raises(RuntimeHaltedError):
        asyncio.run(pipeline.handle(later))
    assert len(store.load_signals()) == 1
    assert [row.revision_id for row in store.load_pending_raw()] == [later.revision_id]


@pytest.mark.parametrize("failure_site", ["replay_read", "heartbeat"])
def test_recovery_post_commit_failure_does_not_repend_committed_raw(
    tmp_path, raw_factory, monkeypatch, failure_site
):
    store = Store(tmp_path / f"recovery-post-commit-{failure_site}.db")
    first = raw_factory("QQQ 719C TODAY @ 1.01", message_id="first")
    second = raw_factory("All out", message_id="second", minute=1)
    store.append_raw(first)
    store.append_raw(second)
    original_load = store.load_signals
    reads = 0

    def fail(*args, **kwargs):
        nonlocal reads
        reads += 1
        if failure_site == "replay_read" and reads == 1:
            return original_load()
        raise RuntimeError("recovery post-commit check failed")

    with monkeypatch.context() as patch:
        patch.setattr(store, "load_signals" if failure_site == "replay_read" else "heartbeat", fail)
        with pytest.raises(RuntimeError, match="failed to recover"):
            Pipeline(store, integrity_sentinel=ReplayIntegritySentinel(every_n_commits=1))

    assert len(store.load_signals()) == 1
    assert store.load_signals()[0].message_id == first.message_id
    assert [row.revision_id for row in store.load_pending_raw()] == [second.revision_id]
    assert store.runtime_halt()[0] is True
    with store.connect() as db:
        row = db.execute(
            "SELECT status,error FROM raw_processing WHERE raw_event_id=?",
            (first.revision_id,),
        ).fetchone()
        assert tuple(row) == ("DONE", "")



def test_durable_integrity_audit_detects_missing_decision_bundle(tmp_path, raw_factory):
    store = Store(tmp_path / "missing-audit.db")
    pipeline = Pipeline(store)
    asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))

    with store.connect() as db:
        db.execute("DELETE FROM decision_audit")

    report = audit_durable_integrity(store)
    assert report.ok is False
    assert report.signals_missing_audit == 1
    assert report.reason_codes == ("signal_missing_decision_audit",)

    result = ReplayIntegritySentinel(every_n_commits=1).after_commit(
        store, pipeline.state, force=True
    )
    assert result is not None
    assert result.ok is False
    assert result.live_fingerprint == result.durable_fingerprint
    assert store.runtime_halt() == (True, "durable_integrity_violation")


def test_durable_integrity_audit_detects_orphan_effect_and_invalid_journal_status(
    tmp_path, raw_factory
):
    store = Store(tmp_path / "bundle-corruption.db")
    pipeline = Pipeline(store)
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    asyncio.run(pipeline.handle(raw))

    with store.connect() as db:
        db.execute(
            """
            INSERT INTO proposed_effects
            (source_event_id,kind,contract_key,generation,reason,quantity_hint,
             metadata_json,created_ts_utc,status)
            VALUES ('ghost-event','REVIEW',NULL,0,'fault-injected',NULL,'{}',
                    '2026-09-08T14:00:00+00:00','PENDING_REVIEW')
            """
        )
        db.execute(
            "UPDATE raw_processing SET status='CORRUPTED' WHERE raw_event_id=?",
            (raw.revision_id,),
        )

    report = audit_durable_integrity(store)
    assert report.ok is False
    assert report.orphan_effects == 1
    assert report.invalid_raw_statuses == 1
    assert set(report.reason_codes) == {"invalid_raw_status", "orphan_effect"}


def test_durable_integrity_audit_detects_audit_kind_tampering(tmp_path, raw_factory):
    store = Store(tmp_path / "audit-kind.db")
    pipeline = Pipeline(store)
    asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))

    with store.connect() as db:
        db.execute("UPDATE decision_audit SET kind='EXIT'")

    report = audit_durable_integrity(store)
    assert report.ok is False
    assert report.audit_kind_mismatches == 1
    assert "audit_kind_mismatch" in report.reason_codes


def test_halt_recovery_round_trip_preserves_exact_replay_identity(tmp_path, raw_factory):
    store = Store(tmp_path / "halt-recovery-roundtrip.db")
    pipeline = Pipeline(store)
    first = raw_factory("QQQ 719C TODAY @ 1.01", message_id="first")
    asyncio.run(pipeline.handle(first))

    store.set_halt(True, "operator_hold")
    queued = [
        raw_factory("SPY 600C TODAY @ 1.00", message_id="second", minute=1),
        raw_factory("IWM 250C TODAY @ 1.00", message_id="third", minute=2),
    ]
    for raw in queued:
        with pytest.raises(RuntimeHaltedError):
            asyncio.run(pipeline.handle(raw))

    assert len(store.load_pending_raw()) == 2
    before_recovery_count = len(store.load_signals())

    # A restart while halted is capture-only; clearing the operator hold then
    # restarting must recover each pending revision exactly once and in order.
    halted_restart = Pipeline(store)
    assert len(halted_restart.state.seen_event_ids) == before_recovery_count
    assert len(store.load_pending_raw()) == 2

    store.set_halt(False, "operator_released")
    recovered = Pipeline(store)

    assert store.load_pending_raw() == []
    events = store.load_signals()
    assert len(events) == before_recovery_count + 2
    assert len({event.event_id for event in events}) == len(events)

    from scorpion.replay import replay, state_fingerprint

    durable_state, _ = replay(events)
    assert state_fingerprint(recovered.state) == state_fingerprint(durable_state)
