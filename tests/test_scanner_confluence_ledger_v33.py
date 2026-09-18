from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.scanner_batch import (
    EXPECTED_SCANNER_CONTRACT_FINGERPRINT,
    ScannerBatchRowMetadata,
    ScannerContextBatch,
)
from scorpion.scanner_confluence_ledger import (
    AttributionMode,
    ContextBindingStatus,
    attach_confluence_outcome,
    freeze_confluence_snapshot,
    persist_confluence_snapshot,
    verify_confluence_ledger,
)
from scorpion.scanner_context import ScannerContextObservation


def _observation(
    *,
    symbol: str,
    opened: datetime,
    observation_id: str,
    observed_offset_seconds: int = -300,
    received_offset_seconds: int = -240,
) -> ScannerContextObservation:
    observed = opened + timedelta(seconds=observed_offset_seconds)
    received = opened + timedelta(seconds=received_offset_seconds)
    return ScannerContextObservation(
        observation_id=observation_id,
        run_id="scanner-run",
        symbol=symbol,
        instrument_id=f"US_EQUITY:{symbol}",
        category="gapper_mobility",
        observed_at_utc=observed,
        received_at_utc=received,
        observation_time_precision="EXACT",
        availability_time_verified=True,
        market_data_as_of_utc=observed - timedelta(seconds=1),
        market_data_source="fixture-feed",
        session="RTH",
        ross_boxes_hit=5,
        ross_boxes_known=5,
        rvol=8.0,
        rvol_basis="TIME_ADJUSTED_INTRADAY_RVOL",
        tape_flag="GO",
        policy_fingerprint="p" * 64,
        quality_blocking_reasons=(),
        causal_blocking_reasons=(),
    )


def _batch(
    observation: ScannerContextObservation,
    *,
    disposition: str = "SELECTED",
    full_universe: bool = True,
) -> ScannerContextBatch:
    metadata = ScannerBatchRowMetadata(
        observation_id=observation.observation_id,
        symbol=observation.symbol,
        selection_disposition=disposition,
        confidence=0.7,
        confidence_semantics="RANKING_HEURISTIC",
    )
    return ScannerContextBatch(
        manifest_id="m" * 64,
        run_id=observation.run_id,
        contract_fingerprint=EXPECTED_SCANNER_CONTRACT_FINGERPRINT,
        row_count=1,
        selected_symbol_count=int(disposition == "SELECTED"),
        ordered_observation_ids=(observation.observation_id,),
        batch_sha256="b" * 64,
        selection_scope="FULL_UNIVERSE" if full_universe else "HITS_ONLY",
        universe_fingerprint="u" * 64,
        symbols_attempted_count=1,
        symbols_with_market_data_count=1,
        error_count=0,
        coverage_complete=full_universe,
        policy_fingerprint="p" * 64,
        code_revision="fixture",
        generated_at_utc=observation.observed_at_utc,
        observations=(observation,),
        row_metadata=(metadata,),
    )


def _trade(*, symbol: str, opened: datetime, entry_event_id: str) -> CompletedTrade:
    return CompletedTrade(
        entry_event_id=entry_event_id,
        contract_key=f"{symbol}|CALL|10|2026-09-18",
        channel_id="channel",
        author_id="author",
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        opened_ts_utc=opened,
        closed_ts_utc=opened + timedelta(minutes=10),
        initial_quantity=1,
        add_count=0,
        trim_count=0,
        gross_premium_in=Decimal("100"),
        gross_proceeds=Decimal("112"),
        pnl=Decimal("12"),
        return_fraction=Decimal("0.12"),
        holding_seconds=600.0,
        depth_evidence_complete=True,
    )


def test_entry_context_freezes_before_outcome_then_attaches_later(tmp_path: Path) -> None:
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    observation = _observation(
        symbol="AENT",
        opened=opened,
        observation_id="1" * 64,
    )
    snapshot = freeze_confluence_snapshot(
        entry_event_id="event-aent",
        contract_key="AENT|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=2),
        batches=(_batch(observation),),
        mode=AttributionMode.OPERATIONAL,
    )
    assert snapshot.binding_status is ContextBindingStatus.BOUND
    assert snapshot.observation_id == observation.observation_id
    assert snapshot.selection_disposition == "SELECTED"
    assert snapshot.full_universe_comparable is True
    assert snapshot.execution_authority is False

    database = tmp_path / "confluence.sqlite3"
    persist_confluence_snapshot(database, snapshot)
    before = verify_confluence_ledger(database)
    assert before.snapshots == 1
    assert before.outcomes == 0
    assert before.completed_fraction == 0.0
    assert before.chain_valid
    assert before.rows_valid

    outcome = attach_confluence_outcome(
        database,
        snapshot,
        _trade(symbol="AENT", opened=opened, entry_event_id="event-aent"),
    )
    assert outcome.snapshot_id == snapshot.snapshot_id
    assert outcome.return_fraction == "0.12"

    after = verify_confluence_ledger(database)
    assert after.snapshots == 1
    assert after.bound_snapshots == 1
    assert after.outcomes == 1
    assert after.completed_fraction == 1.0
    assert after.chain_valid
    assert after.rows_valid


def test_no_context_is_frozen_as_denominator_evidence(tmp_path: Path) -> None:
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    snapshot = freeze_confluence_snapshot(
        entry_event_id="event-missing",
        contract_key="MISS|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=1),
        batches=(),
        mode=AttributionMode.OPERATIONAL,
    )
    assert snapshot.binding_status is ContextBindingStatus.NO_CONTEXT
    assert snapshot.observation_id is None

    database = tmp_path / "confluence.sqlite3"
    persist_confluence_snapshot(database, snapshot)
    report = verify_confluence_ledger(database)
    assert report.snapshots == 1
    assert report.bound_snapshots == 0
    assert report.explicit_missing_snapshots == 1


def test_late_backfill_cannot_rewrite_frozen_missing_context(tmp_path: Path) -> None:
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    database = tmp_path / "confluence.sqlite3"
    missing = freeze_confluence_snapshot(
        entry_event_id="event-backfill",
        contract_key="AENT|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=1),
        batches=(),
        mode=AttributionMode.SOURCE_TIME,
    )
    persist_confluence_snapshot(database, missing)

    historical = _observation(
        symbol="AENT",
        opened=opened,
        observation_id="2" * 64,
        observed_offset_seconds=-60,
        received_offset_seconds=120,
    )
    rewritten = freeze_confluence_snapshot(
        entry_event_id="event-backfill",
        contract_key="AENT|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=1),
        batches=(_batch(historical),),
        mode=AttributionMode.SOURCE_TIME,
    )
    assert rewritten.binding_status is ContextBindingStatus.BOUND
    with pytest.raises(ValueError, match="already frozen"):
        persist_confluence_snapshot(database, rewritten)


def test_operational_mode_records_backfill_as_not_available() -> None:
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    historical = _observation(
        symbol="AENT",
        opened=opened,
        observation_id="3" * 64,
        observed_offset_seconds=-60,
        received_offset_seconds=30,
    )
    snapshot = freeze_confluence_snapshot(
        entry_event_id="event-operational",
        contract_key="AENT|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=2),
        batches=(_batch(historical),),
        mode=AttributionMode.OPERATIONAL,
    )
    assert snapshot.binding_status is ContextBindingStatus.NOT_AVAILABLE_BY_RECEIVE_TIME
    assert snapshot.observation_id is None


def test_stale_context_is_explicit_not_silently_bound() -> None:
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    stale = _observation(
        symbol="AENT",
        opened=opened,
        observation_id="4" * 64,
        observed_offset_seconds=-7200,
        received_offset_seconds=-7190,
    )
    snapshot = freeze_confluence_snapshot(
        entry_event_id="event-stale",
        contract_key="AENT|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=2),
        batches=(_batch(stale),),
        mode=AttributionMode.OPERATIONAL,
    )
    assert snapshot.binding_status is ContextBindingStatus.STALE_CONTEXT
    assert snapshot.observation_id is None


def test_snapshot_and_outcome_tables_are_append_only(tmp_path: Path) -> None:
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    observation = _observation(
        symbol="AENT",
        opened=opened,
        observation_id="5" * 64,
    )
    snapshot = freeze_confluence_snapshot(
        entry_event_id="event-append-only",
        contract_key="AENT|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=2),
        batches=(_batch(observation),),
        mode=AttributionMode.OPERATIONAL,
    )
    database = tmp_path / "confluence.sqlite3"
    persist_confluence_snapshot(database, snapshot)
    attach_confluence_outcome(
        database,
        snapshot,
        _trade(symbol="AENT", opened=opened, entry_event_id="event-append-only"),
    )

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE confluence_snapshots SET payload_json='{}' WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM confluence_outcomes WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            )


def test_same_snapshot_and_outcome_retry_is_idempotent(tmp_path: Path) -> None:
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    observation = _observation(
        symbol="AENT",
        opened=opened,
        observation_id="6" * 64,
    )
    snapshot = freeze_confluence_snapshot(
        entry_event_id="event-retry",
        contract_key="AENT|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=2),
        batches=(_batch(observation),),
        mode=AttributionMode.OPERATIONAL,
    )
    database = tmp_path / "confluence.sqlite3"
    persist_confluence_snapshot(database, snapshot)
    persist_confluence_snapshot(database, snapshot)

    trade = _trade(symbol="AENT", opened=opened, entry_event_id="event-retry")
    first = attach_confluence_outcome(database, snapshot, trade)
    second = attach_confluence_outcome(database, snapshot, trade)
    assert first == second

    report = verify_confluence_ledger(database)
    assert report.snapshots == 1
    assert report.outcomes == 1


def test_outcome_cannot_attach_to_different_entry() -> None:
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    snapshot = freeze_confluence_snapshot(
        entry_event_id="event-a",
        contract_key="AENT|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=2),
        batches=(),
        mode=AttributionMode.OPERATIONAL,
    )
    with pytest.raises(ValueError, match="entry_event_id"):
        attach_confluence_outcome(
            Path("/tmp/unused-confluence.sqlite3"),
            snapshot,
            _trade(symbol="AENT", opened=opened, entry_event_id="event-b"),
        )
