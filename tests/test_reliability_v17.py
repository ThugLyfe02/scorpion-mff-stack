import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scorpion.causal_trace import trace_event
from scorpion.certification import certify_runtime
from scorpion.history_archive import ArchivedDiscordMessage, HistoryArchive
from scorpion.pipeline import Pipeline
from scorpion.processing_order import load_signals_in_processing_order
from scorpion.quote_tape import HistoricalQuote, HistoricalQuoteTape
from scorpion.recovery import verify_backup
from scorpion.release_guard import ReleaseRegistry
from scorpion.sizing_lab import SizingConstraints
from scorpion.state_checkpoint import create_state_checkpoint, restore_state
from scorpion.store import Store
from scorpion.uncertainty_envelope import (
    ExecutionScenarioSpec,
    run_execution_uncertainty_envelope,
)

GUILD = "912747256736800838"
CHANNEL = "968352649437126676"
AUTHOR = "author"


def _archived(content: str, *, message_id: str, ts: datetime) -> ArchivedDiscordMessage:
    return ArchivedDiscordMessage(
        message_id=message_id,
        guild_id=GUILD,
        channel_id=CHANNEL,
        author_id=AUTHOR,
        source_ts_utc=ts,
        content=content,
    )


def test_release_identity_binds_predecessor(tmp_path):
    registry = ReleaseRegistry(tmp_path / "releases.db")
    first = registry.register(
        component="parser",
        artifact_hash="artifact-a",
        policy_fingerprint="policy-a",
        research_manifest_hash="manifest-a",
    )
    second = registry.register(
        component="parser",
        artifact_hash="artifact-a",
        policy_fingerprint="policy-a",
        research_manifest_hash="manifest-a",
        previous_release_id=first.release_id,
    )
    assert second.release_id != first.release_id
    assert second.previous_release_id == first.release_id


def test_verify_backup_rejects_missing_paths_without_creating_files(tmp_path):
    source = tmp_path / "missing-source.db"
    backup = tmp_path / "missing-backup.db"
    with pytest.raises(FileNotFoundError):
        verify_backup(source, backup)
    assert source.exists() is False
    assert backup.exists() is False


def test_runtime_certification_rejects_signals_outside_integrity_ledger(tmp_path, raw_factory):
    store = Store(tmp_path / "certify.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({AUTHOR}))
    asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00")))
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM integrity_ledger")
        db.commit()

    report = certify_runtime(store.path)
    evidence_check = next(
        item for item in report.checks if item.name == "database_evidence_integrity"
    )
    assert evidence_check.passed is False
    assert report.passed is False
    assert "signals_outside_integrity_ledger" in evidence_check.detail


def test_checkpoint_restore_rejects_tampered_normalized_prefix(tmp_path, raw_factory):
    store = Store(tmp_path / "checkpoint.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({AUTHOR}))
    event, _ = asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00")))
    create_state_checkpoint(store.path)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE signal_events SET kind='IGNORE' WHERE event_id=?", (event.event_id,))
        db.commit()

    signals = load_signals_in_processing_order(store.path)
    restored = restore_state(store.path, signals)
    assert restored.used_checkpoint is False
    assert restored.reason == "full_replay_durable_process_order"


def test_causal_trace_binds_exact_raw_revision(tmp_path, raw_factory):
    store = Store(tmp_path / "trace.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({AUTHOR}))
    original = raw_factory("AAPL 200C TODAY @ 1.00", message_id="edited-message")
    event, _ = asyncio.run(pipeline.handle(original))
    edited = replace(
        original,
        content="AAPL 205C TODAY @ 1.20",
        edited_ts_utc=original.source_ts_utc + timedelta(seconds=2),
        received_ts_utc=original.received_ts_utc + timedelta(seconds=2),
    )
    store.append_raw(edited)

    trace = trace_event(store.path, event.event_id)
    assert trace.raw is not None
    assert trace.raw["raw_event_id"] == original.revision_id
    assert trace.raw["content"] == original.content


def test_uncertainty_counts_harsh_insufficient_scenario(tmp_path):
    archive = HistoryArchive(tmp_path / "history.db")
    opened = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    closed = opened + timedelta(minutes=1)
    archive.append(_archived("AAPL 200C TODAY @ 1.00", message_id="1", ts=opened))
    archive.append(_archived("closing runners", message_id="2", ts=closed))
    contract = "AAPL|CALL|200|2026-09-08"
    tape = HistoricalQuoteTape(
        (
            HistoricalQuote(
                contract,
                opened + timedelta(milliseconds=100),
                Decimal("1.00"),
                Decimal("1.02"),
                bid_size=10,
                ask_size=10,
            ),
            HistoricalQuote(
                contract,
                closed + timedelta(milliseconds=100),
                Decimal("1.20"),
                Decimal("1.22"),
                bid_size=10,
                ask_size=10,
            ),
        )
    )
    envelope = run_execution_uncertainty_envelope(
        archive,
        tape,
        channel_ids=frozenset({CHANNEL}),
        allowed_author_ids=frozenset({AUTHOR}),
        scenarios=(
            ExecutionScenarioSpec(50, 500, 1000),
            ExecutionScenarioSpec(2000, 100, 100),
        ),
        constraints=SizingConstraints(min_samples=1, bootstrap_resamples=100),
        minimum_scenario_samples=1,
        minimum_passing_fraction=0.50,
    )
    assert envelope.populated_scenarios == 1
    assert envelope.passing_scenarios == 1
    assert envelope.robust is False
    assert "insufficient_execution_scenarios:1" in envelope.failures
