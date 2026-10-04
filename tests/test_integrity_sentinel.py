import asyncio
from dataclasses import replace

import pytest

from scorpion.integrity import ReplayIntegritySentinel
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
    raw = raw_factory("AAPL 200C TODAY @ 1.01")

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


def test_sentinel_replays_admitted_and_observed_books_without_conflating_them(
    tmp_path, raw_factory
):
    store = Store(tmp_path / "admitted-observed.db")
    sentinel = ReplayIntegritySentinel(every_n_commits=1)
    pipeline = Pipeline(store, integrity_sentinel=sentinel)

    research, _ = asyncio.run(
        pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01", message_id="research"))
    )
    eligible, _ = asyncio.run(
        pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.01", message_id="eligible", minute=1))
    )

    assert store.runtime_halt() == (False, "")
    assert research.contract_key not in pipeline.state.positions
    assert research.contract_key in pipeline.observed_state.positions
    assert eligible.contract_key in pipeline.state.positions
    result = sentinel.after_commit(
        store,
        pipeline.state,
        observed_state=pipeline.observed_state,
        force=True,
    )
    assert result is not None
    assert result.ok is True
    assert result.live_fingerprint == result.durable_fingerprint
    assert result.observed_live_fingerprint == result.observed_durable_fingerprint
    assert result.live_fingerprint != result.observed_live_fingerprint


def test_sentinel_detects_observation_divergence_even_when_admitted_book_matches(
    tmp_path, raw_factory
):
    store = Store(tmp_path / "observed-divergence.db")
    pipeline = Pipeline(store)
    asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))

    result = pipeline.integrity_sentinel.after_commit(
        store,
        pipeline.state,
        observed_state=replace(pipeline.observed_state, halted=True),
        force=True,
    )

    assert result is not None
    assert result.ok is False
    assert result.live_fingerprint == result.durable_fingerprint
    assert result.observed_live_fingerprint != result.observed_durable_fingerprint
    assert store.runtime_halt() == (True, "replay_integrity_divergence")
