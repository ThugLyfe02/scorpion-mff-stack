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
