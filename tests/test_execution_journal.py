import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from scorpion.cli import reconcile_main
from scorpion.execution_journal import ExecutionJournal, FillSource, reconstruct_execution_state
from scorpion.pipeline import Pipeline
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
