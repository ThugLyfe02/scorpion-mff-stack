import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

import scorpion.integrity as integrity_module
from scorpion.integrity import ReplayIntegritySentinel
from scorpion.pipeline import Pipeline, RuntimeHaltedError
from scorpion.policy_bundle import RuntimePolicyBundle
from scorpion.processing_order import load_signals_in_processing_order
from scorpion.replay import replay, state_fingerprint
from scorpion.state_checkpoint import create_state_checkpoint
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
        if failure_site == "replay_read":
            patch.setattr(integrity_module, "load_signals_in_processing_order", fail)
        else:
            patch.setattr(store, "heartbeat", fail)
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
        assert db.execute("SELECT COUNT(*) FROM raw_failure_events").fetchone()[0] == 0

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
    original_heartbeat = store.heartbeat

    def fail(*args, **kwargs):
        if failure_site == "heartbeat" and args[0] != "replay-integrity":
            return original_heartbeat(*args, **kwargs)
        raise RuntimeError("recovery post-commit check failed")

    with monkeypatch.context() as patch:
        if failure_site == "replay_read":
            patch.setattr(integrity_module, "load_signals_in_processing_order", fail)
        else:
            patch.setattr(store, "heartbeat", fail)
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
        assert db.execute("SELECT COUNT(*) FROM raw_failure_events").fetchone()[0] == 0


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


def test_sentinel_and_restart_use_the_effective_nondefault_policy(tmp_path, raw_factory):
    store = Store(tmp_path / "nondefault-policy.db")
    default = RuntimePolicyBundle()
    policy = replace(default, base=replace(default.base, max_open_positions=4))
    pipeline = Pipeline(
        store,
        runtime_policy=policy,
        integrity_sentinel=ReplayIntegritySentinel(every_n_commits=1),
    )
    for index, symbol in enumerate(("AAPL", "NVDA", "MSFT")):
        asyncio.run(
            pipeline.handle(
                raw_factory(f"{symbol} 200C TODAY @ 1.00", message_id=symbol, minute=index)
            )
        )

    assert len(pipeline.state.positions) == 3
    assert len(pipeline.observed_state.positions) == 3
    assert store.runtime_halt() == (False, "")
    create_state_checkpoint(store.path, runtime_policy=policy)
    restarted = Pipeline(Store(store.path), runtime_policy=policy)
    assert state_fingerprint(restarted.state) == state_fingerprint(pipeline.state)
    assert state_fingerprint(restarted.observed_state) == state_fingerprint(pipeline.observed_state)
    result = restarted.integrity_sentinel.after_commit(
        restarted.store,
        restarted.state,
        observed_state=restarted.observed_state,
        runtime_policy=policy,
        force=True,
    )
    assert result is not None and result.ok


def test_sentinel_uses_durable_process_order_when_source_sort_changes_capacity(
    tmp_path, raw_factory
):
    store = Store(tmp_path / "process-order-sentinel.db")
    pipeline = Pipeline(store, integrity_sentinel=ReplayIntegritySentinel(every_n_commits=1))
    first = raw_factory("AAPL 200C TODAY @ 1.00", message_id="first")
    second = raw_factory("NVDA 200C TODAY @ 1.00", message_id="second", minute=1)
    third = replace(
        raw_factory("MSFT 200C TODAY @ 1.00", message_id="third", minute=2),
        source_ts_utc=first.source_ts_utc - timedelta(seconds=1),
    )
    for raw in (first, second, third):
        asyncio.run(pipeline.handle(raw))

    assert store.runtime_halt() == (False, "")
    source_order_state, _ = replay(
        load_signals_in_processing_order(store.path),
        pipeline.runtime_policy.base,
    )
    assert state_fingerprint(source_order_state) != state_fingerprint(pipeline.observed_state)
    create_state_checkpoint(store.path, runtime_policy=pipeline.runtime_policy)
    restarted = Pipeline(Store(store.path), runtime_policy=pipeline.runtime_policy)
    assert state_fingerprint(restarted.state) == state_fingerprint(pipeline.state)
    assert state_fingerprint(restarted.observed_state) == state_fingerprint(pipeline.observed_state)


def test_checkpoint_of_observed_history_cannot_admit_blocked_strategy_on_restart(
    tmp_path, raw_factory
):
    store = Store(tmp_path / "observed-checkpoint.db")
    pipeline = Pipeline(store)
    first, _ = asyncio.run(
        pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.00", message_id="blocked-before"))
    )
    create_state_checkpoint(store.path, runtime_policy=pipeline.runtime_policy)
    eligible, effects = asyncio.run(
        pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00", message_id="eligible", minute=1))
    )
    last, _ = asyncio.run(
        pipeline.handle(raw_factory("SPY 660C TODAY @ 1.00", message_id="blocked-after", minute=2))
    )
    assert effects[0].reason == "first_entry_pipe_test"
    with store.connect() as db:
        before = [
            tuple(row) for row in db.execute("SELECT * FROM proposed_effects ORDER BY effect_id")
        ]

    restarted = Pipeline(Store(store.path), runtime_policy=pipeline.runtime_policy)

    assert set(restarted.state.positions) == {eligible.contract_key}
    assert first.event_id in restarted.observed_state.seen_event_ids
    assert last.event_id in restarted.observed_state.seen_event_ids
    assert first.event_id not in restarted.state.seen_event_ids
    assert last.event_id not in restarted.state.seen_event_ids
    assert state_fingerprint(restarted.observed_state) == state_fingerprint(pipeline.observed_state)
    assert store.runtime_halt() == (False, "")
    with store.connect() as db:
        after = [
            tuple(row) for row in db.execute("SELECT * FROM proposed_effects ORDER BY effect_id")
        ]
    assert after == before
    recovery = store.health_snapshot()["heartbeats"]["replay-recovery"]["metadata"]
    assert recovery["used_checkpoint"] is True
    assert recovery["checkpoint_state_scope"] == "OBSERVED"


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_restart_preserves_damaged_effect_evidence_and_latches_halt(
    tmp_path, raw_factory, damage
):
    store = Store(tmp_path / f"damaged-effects-{damage}.db")
    pipeline = Pipeline(store)
    asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00")))
    with store.connect() as db:
        if damage == "missing":
            db.execute("DELETE FROM proposed_effects")
        else:
            db.execute("UPDATE proposed_effects SET quantity_hint=42")
        damaged = [tuple(row) for row in db.execute("SELECT * FROM proposed_effects")]

    restarted = Pipeline(Store(store.path))

    assert store.runtime_halt() == (True, "startup_evidence_integrity_failed")
    with store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM proposed_effects")] == damaged
    with pytest.raises(RuntimeHaltedError):
        asyncio.run(restarted.handle(raw_factory("All out", message_id="later", minute=1)))
    assert len(store.load_signals()) == 1
