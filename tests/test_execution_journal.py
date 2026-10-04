import asyncio
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from scorpion.cli import reconcile_main
from scorpion.config import DEFAULT_POLICY
from scorpion.execution_journal import ExecutionJournal, FillSource, reconstruct_execution_state
from scorpion.pipeline import Pipeline
from scorpion.policy_bundle import RuntimePolicyBundle
from scorpion.store import Store


def _eligible_entry(tmp_path, raw_factory):
    path = tmp_path / "execution-truth.db"
    store = Store(path)
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    event, _effects = asyncio.run(
        pipeline.handle(
            raw_factory(
                "AAPL 200C TODAY @ 1.00",
                message_id="entry",
            )
        )
    )
    return path, store, pipeline, event


def test_fill_journal_reconstructs_executed_quantity_and_cost_basis(tmp_path, raw_factory):
    path, store, _pipeline, event = _eligible_entry(tmp_path, raw_factory)
    journal = ExecutionJournal(path)
    record = journal.record(
        event.event_id,
        quantity_delta=1,
        fill_price=Decimal("1.05"),
        source=FillSource.PAPER,
        recorded_by="test",
        filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
    )

    assert record.sequence == 1
    assert journal.verify().ok is True
    snapshot = reconstruct_execution_state(path, store.load_signals())
    assert snapshot.available is True
    assert snapshot.fills_applied == 1
    assert snapshot.state is not None
    position = snapshot.state.positions[event.contract_key or ""]
    assert position.quantity == 1
    assert position.average_price == Decimal("1.05")
    assert position.status.value == "OPEN"


def test_execution_journal_enforces_first_entry_quantity_hint(tmp_path, raw_factory):
    path, _store, _pipeline, event = _eligible_entry(tmp_path, raw_factory)
    with pytest.raises(ValueError, match="quantity hint"):
        ExecutionJournal(path).record(
            event.event_id,
            quantity_delta=2,
            fill_price=Decimal("1.05"),
            source=FillSource.PAPER,
            recorded_by="test",
            filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
        )


def test_blocked_research_effect_cannot_be_recorded_as_execution(tmp_path, raw_factory):
    path = tmp_path / "blocked-fill.db"
    store = Store(path)
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    event, _effects = asyncio.run(
        pipeline.handle(
            raw_factory(
                "QQQ 719C TODAY @ 1.01",
                message_id="blocked",
                channel_id="1448448931116748993",
            )
        )
    )
    with pytest.raises(ValueError, match="blocked"):
        ExecutionJournal(path).record(
            event.event_id,
            quantity_delta=1,
            fill_price=Decimal("1.02"),
            source=FillSource.EXTERNAL_CONFIRMATION,
            recorded_by="operator",
            external_ref="broker-fill-1",
            filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
        )


def test_external_fill_reference_is_idempotent_but_conflicts_fail(tmp_path, raw_factory):
    path, _store, _pipeline, event = _eligible_entry(tmp_path, raw_factory)
    journal = ExecutionJournal(path)
    first = journal.record(
        event.event_id,
        quantity_delta=1,
        fill_price=Decimal("1.05"),
        source=FillSource.EXTERNAL_CONFIRMATION,
        recorded_by="operator",
        external_ref="exec-abc",
        filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
    )
    second = journal.record(
        event.event_id,
        quantity_delta=1,
        fill_price=Decimal("1.05"),
        source=FillSource.EXTERNAL_CONFIRMATION,
        recorded_by="operator",
        external_ref="exec-abc",
        filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
    )
    assert second.record_hash == first.record_hash
    assert len(journal.records()) == 1

    with pytest.raises(ValueError, match="different fill"):
        journal.record(
            event.event_id,
            quantity_delta=1,
            fill_price=Decimal("1.06"),
            source=FillSource.EXTERNAL_CONFIRMATION,
            recorded_by="operator",
            external_ref="exec-abc",
            filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
        )


def test_tampered_fill_journal_fails_execution_truth_closed(tmp_path, raw_factory):
    path, store, _pipeline, event = _eligible_entry(tmp_path, raw_factory)
    journal = ExecutionJournal(path)
    journal.record(
        event.event_id,
        quantity_delta=1,
        fill_price=Decimal("1.05"),
        source=FillSource.PAPER,
        recorded_by="test",
        filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
    )
    with sqlite3.connect(path) as db:
        db.execute("UPDATE execution_fill_ledger SET quantity_delta=2 WHERE sequence=1")

    verification = journal.verify()
    assert verification.ok is False
    assert any("column_payload_mismatch" in failure for failure in verification.failures)
    snapshot = reconstruct_execution_state(path, store.load_signals())
    assert snapshot.available is False
    assert snapshot.state is None


def test_reconcile_cli_uses_persisted_fills_not_signal_intent(
    tmp_path,
    raw_factory,
    monkeypatch,
    capsys,
):
    path, _store, _pipeline, event = _eligible_entry(tmp_path, raw_factory)
    ExecutionJournal(path).record(
        event.event_id,
        quantity_delta=1,
        fill_price=Decimal("1.05"),
        source=FillSource.PAPER,
        recorded_by="test",
        filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
    )
    observations = tmp_path / "positions.json"
    observations.write_text(
        json.dumps(
            [
                {
                    "contract_key": event.contract_key,
                    "quantity": 1,
                    "average_price": "1.05",
                }
            ]
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["scorpion-reconcile", str(observations), "--db", str(path)],
    )
    reconcile_main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_truth_available"] is True
    assert payload["fills_applied"] == 1
    assert payload["clean"] is True
    assert payload["critical"] is False


def test_reconcile_cli_refuses_to_compare_against_tampered_fill_truth(
    tmp_path,
    raw_factory,
    monkeypatch,
    capsys,
):
    path, _store, _pipeline, event = _eligible_entry(tmp_path, raw_factory)
    ExecutionJournal(path).record(
        event.event_id,
        quantity_delta=1,
        fill_price=Decimal("1.05"),
        source=FillSource.PAPER,
        recorded_by="test",
        filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
    )
    with sqlite3.connect(path) as db:
        db.execute("UPDATE execution_fill_ledger SET fill_price='9.99' WHERE sequence=1")

    observations = tmp_path / "positions.json"
    observations.write_text("[]")
    monkeypatch.setattr(
        sys,
        "argv",
        ["scorpion-reconcile", str(observations), "--db", str(path)],
    )
    reconcile_main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_truth_available"] is False
    assert payload["critical"] is True
    assert payload["findings"][0]["code"] == "execution_truth_unavailable"


@pytest.mark.parametrize("supply_legacy_events", [False, True])
def test_fill_truth_preserves_durable_admission_order_and_current_policy(
    tmp_path, raw_factory, supply_legacy_events
):
    path = tmp_path / "ordered-fill-truth.db"
    store = Store(path)
    policy = RuntimePolicyBundle(base=replace(DEFAULT_POLICY, max_open_positions=1))
    pipeline = Pipeline(
        store,
        allowed_author_ids=frozenset({"author"}),
        runtime_policy=policy,
    )
    blocked, _ = asyncio.run(
        pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01", message_id="blocked"))
    )
    first, effects = asyncio.run(
        pipeline.handle(
            replace(
                raw_factory("AAPL 200C TODAY @ 1.00", message_id="first", minute=2),
                received_ts_utc=datetime(2026, 9, 8, 14, 3, tzinfo=UTC),
            )
        )
    )
    later, _ = asyncio.run(
        pipeline.handle(
            replace(
                raw_factory("MSFT 500C TODAY @ 1.00", message_id="delayed", minute=1),
                received_ts_utc=datetime(2026, 9, 8, 14, 4, tzinfo=UTC),
            )
        )
    )
    assert effects[0].quantity_hint == 1
    assert set(pipeline.state.positions) == {first.contract_key}
    assert pipeline.state.positions[first.contract_key].quantity == 0

    journal = ExecutionJournal(path, runtime_policy=policy)
    with pytest.raises(ValueError, match="blocked"):
        journal.record(
            blocked.event_id,
            quantity_delta=1,
            fill_price=Decimal("1.02"),
            source=FillSource.PAPER,
            recorded_by="test",
            filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
        )
    journal.record(
        first.event_id,
        quantity_delta=1,
        fill_price=Decimal("1.05"),
        source=FillSource.PAPER,
        recorded_by="test",
        filled_ts_utc=datetime(2026, 9, 8, 14, 3, 5, tzinfo=UTC),
    )
    # Store.load_signals() sorts the delayed event before the event admitted live.
    supplied = store.load_signals() if supply_legacy_events else None
    snapshot = reconstruct_execution_state(path, supplied, runtime_policy=policy)
    assert snapshot.available is True
    assert snapshot.fills_applied == 1
    assert snapshot.state is not None
    assert set(snapshot.state.positions) == {first.contract_key}
    assert later.contract_key not in snapshot.state.positions
    assert blocked.contract_key not in snapshot.state.positions
    assert snapshot.state.positions[first.contract_key].quantity == 1
    assert snapshot.state.positions[first.contract_key].average_price == Decimal("1.05")
    assert pipeline.state.positions[first.contract_key].quantity == 0


def test_fill_truth_uses_explicit_friday_reducer_policy(tmp_path, raw_factory):
    path = tmp_path / "friday-fill-truth.db"
    store = Store(path)
    policy = RuntimePolicyBundle(base=replace(DEFAULT_POLICY, skip_fridays=False))
    pipeline = Pipeline(
        store,
        allowed_author_ids=frozenset({"author"}),
        runtime_policy=policy,
    )
    timestamp = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
    event, _ = asyncio.run(
        pipeline.handle(
            replace(
                raw_factory("AAPL 200C TODAY @ 1.00", message_id="friday"),
                source_ts_utc=timestamp,
                received_ts_utc=timestamp,
            )
        )
    )
    ExecutionJournal(path, runtime_policy=policy).record(
        event.event_id,
        quantity_delta=1,
        fill_price=Decimal("1.05"),
        source=FillSource.PAPER,
        recorded_by="test",
        filled_ts_utc=timestamp.replace(second=5),
    )
    snapshot = reconstruct_execution_state(path, runtime_policy=policy)
    assert snapshot.available is True
    assert snapshot.state is not None
    assert snapshot.state.positions[event.contract_key].quantity == 1
    # Reading with the incompatible default policy cannot turn that fill into trusted truth.
    assert reconstruct_execution_state(path).available is False


def test_fill_truth_does_not_repair_missing_durable_order_during_read(tmp_path, raw_factory):
    path, _store, _pipeline, event = _eligible_entry(tmp_path, raw_factory)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM event_processing_order WHERE event_id=?", (event.event_id,))

    snapshot = reconstruct_execution_state(path)
    assert snapshot.available is False
    assert snapshot.state is None
    assert any("missing durable process order" in anomaly for anomaly in snapshot.anomalies)
    journal = ExecutionJournal(path)
    with pytest.raises(ValueError, match="missing durable process order"):
        journal.record(
            event.event_id,
            quantity_delta=1,
            fill_price=Decimal("1.05"),
            source=FillSource.PAPER,
            recorded_by="test",
            filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
        )
    assert journal.records() == ()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM event_processing_order").fetchone()[0] == 0


def test_legacy_fill_truth_uses_source_time_not_supplied_list_order(tmp_path, raw_factory):
    path, store, pipeline, first = _eligible_entry(tmp_path, raw_factory)
    asyncio.run(
        pipeline.handle(raw_factory("MSFT 500C TODAY @ 1.00", message_id="second", minute=1))
    )
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE event_processing_order")
    policy = RuntimePolicyBundle(base=replace(DEFAULT_POLICY, max_open_positions=1))
    supplied = tuple(reversed(store.load_signals()))
    snapshot = reconstruct_execution_state(path, supplied, runtime_policy=policy)
    assert snapshot.available is True
    assert snapshot.state is not None
    assert set(snapshot.state.positions) == {first.contract_key}
    assert snapshot.state.positions[first.contract_key].quantity == 0
    assert snapshot.fills_applied == 0


def test_execution_journal_sets_durability_pragmas_on_every_connection(
    tmp_path, raw_factory, monkeypatch
):
    path, _store, _pipeline, event = _eligible_entry(tmp_path, raw_factory)
    real_connect = sqlite3.connect
    observed = []

    class InspectConnection(sqlite3.Connection):
        def close(self):
            observed.append(
                tuple(
                    self.execute(f"PRAGMA {name}").fetchone()[0]
                    for name in ("synchronous", "foreign_keys", "busy_timeout")
                )
            )
            super().close()

    def weak_default_connect(*args, **kwargs):
        db = real_connect(*args, **kwargs, factory=InspectConnection)
        db.execute("PRAGMA synchronous=OFF")
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA busy_timeout=1")
        return db

    monkeypatch.setattr(sqlite3, "connect", weak_default_connect)
    journal = ExecutionJournal(path)
    journal.record(
        event.event_id,
        quantity_delta=1,
        fill_price=Decimal("1.05"),
        source=FillSource.PAPER,
        recorded_by="test",
        filled_ts_utc=datetime(2026, 9, 8, 14, 0, 5, tzinfo=UTC),
    )
    assert journal.verify().ok is True
    assert len(journal.records()) == 1
    assert reconstruct_execution_state(path).available is True
    assert len(observed) == 5
    assert all(settings == (2, 1, 5000) for settings in observed)
