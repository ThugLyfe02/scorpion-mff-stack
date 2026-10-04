from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from scorpion.scanner_attribution_bias import (
    AttributionBiasPolicy,
    AttributionBiasStatus,
    evaluate_attribution_availability_bias,
)
from scorpion.scanner_batch import (
    EXPECTED_SCANNER_CONTRACT_FINGERPRINT,
    ScannerBatchRowMetadata,
    ScannerContextBatch,
)
from scorpion.scanner_confluence_ledger import (
    AttributionMode,
    ContextBindingStatus,
    freeze_confluence_snapshot,
    persist_confluence_snapshot,
)
from scorpion.scanner_context import ScannerContextObservation


def _observation(
    *,
    symbol: str,
    opened: datetime,
    observation_id: str,
    observed_offset_seconds: int,
    received_offset_seconds: int,
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
        market_data_as_of_utc=observed - timedelta(milliseconds=1),
        market_data_source="fixture",
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
    run_id: str,
    observations: tuple[ScannerContextObservation, ...],
) -> ScannerContextBatch:
    metadata = tuple(
        ScannerBatchRowMetadata(
            observation_id=item.observation_id,
            symbol=item.symbol,
            selection_disposition="SELECTED",
            confidence=0.8,
            confidence_semantics="RANKING_HEURISTIC",
        )
        for item in observations
    )
    return ScannerContextBatch(
        manifest_id=(run_id.encode().hex() + "0" * 64)[:64],
        run_id=run_id,
        contract_fingerprint=EXPECTED_SCANNER_CONTRACT_FINGERPRINT,
        row_count=len(observations),
        selected_symbol_count=len(observations),
        ordered_observation_ids=tuple(item.observation_id for item in observations),
        batch_sha256="b" * 64,
        selection_scope="FULL_UNIVERSE",
        universe_fingerprint="u" * 64,
        symbols_attempted_count=len(observations),
        symbols_with_market_data_count=len(observations),
        error_count=0,
        coverage_complete=True,
        policy_fingerprint="p" * 64,
        code_revision="fixture",
        generated_at_utc=min(item.observed_at_utc for item in observations),
        observations=observations,
        row_metadata=metadata,
    )


def _freeze_pair(
    database: Path,
    *,
    entry_event_id: str,
    symbol: str,
    opened: datetime,
    batch: ScannerContextBatch,
) -> None:
    for mode in (AttributionMode.SOURCE_TIME, AttributionMode.OPERATIONAL):
        snapshot = freeze_confluence_snapshot(
            entry_event_id=entry_event_id,
            contract_key=f"{symbol}|CALL|10|2026-09-18",
            mff_source_ts_utc=opened,
            mff_received_ts_utc=opened + timedelta(seconds=2),
            batches=(batch,),
            mode=mode,
        )
        persist_confluence_snapshot(database, snapshot)


def test_identical_source_and_operational_context_is_clean(tmp_path: Path) -> None:
    database = tmp_path / "confluence.sqlite3"
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    for index in range(4):
        symbol = f"C{index}"
        observation = _observation(
            symbol=symbol,
            opened=opened,
            observation_id=f"{index + 1:064x}",
            observed_offset_seconds=-60,
            received_offset_seconds=-50,
        )
        _freeze_pair(
            database,
            entry_event_id=f"event-{index}",
            symbol=symbol,
            opened=opened,
            batch=_batch(f"run-{index}", (observation,)),
        )

    report = evaluate_attribution_availability_bias(
        database,
        policy=AttributionBiasPolicy(min_paired_entries=4),
    )
    assert report.status is AttributionBiasStatus.CLEAN
    assert report.source_coverage_rate == 1.0
    assert report.operational_coverage_rate == 1.0
    assert report.availability_distortion_entries == 0
    assert report.verify_report_id()


def test_backfill_only_source_time_context_is_explicit_inflation(tmp_path: Path) -> None:
    database = tmp_path / "confluence.sqlite3"
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    for index in range(4):
        symbol = f"B{index}"
        observation = _observation(
            symbol=symbol,
            opened=opened,
            observation_id=f"{index + 10:064x}",
            observed_offset_seconds=-60,
            received_offset_seconds=30,
        )
        _freeze_pair(
            database,
            entry_event_id=f"backfill-{index}",
            symbol=symbol,
            opened=opened,
            batch=_batch(f"backfill-run-{index}", (observation,)),
        )

    report = evaluate_attribution_availability_bias(
        database,
        policy=AttributionBiasPolicy(
            min_paired_entries=4,
            max_availability_distortion_fraction=0.05,
        ),
    )
    assert report.status is AttributionBiasStatus.INFLATED
    assert report.backfill_only_entries == 4
    assert report.source_coverage_rate == 1.0
    assert report.operational_coverage_rate == 0.0
    assert report.coverage_inflation_fraction == 1.0


def test_different_bound_context_is_detected_as_substitution(tmp_path: Path) -> None:
    database = tmp_path / "confluence.sqlite3"
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    for index in range(4):
        symbol = f"S{index}"
        older = _observation(
            symbol=symbol,
            opened=opened,
            observation_id=f"{100 + index:064x}",
            observed_offset_seconds=-300,
            received_offset_seconds=-290,
        )
        newer = _observation(
            symbol=symbol,
            opened=opened,
            observation_id=f"{200 + index:064x}",
            observed_offset_seconds=-60,
            received_offset_seconds=30,
        )
        _freeze_pair(
            database,
            entry_event_id=f"substitution-{index}",
            symbol=symbol,
            opened=opened,
            batch=_batch(f"substitution-run-{index}", (older, newer)),
        )

    report = evaluate_attribution_availability_bias(
        database,
        policy=AttributionBiasPolicy(min_paired_entries=4),
    )
    assert report.status is AttributionBiasStatus.INFLATED
    assert report.source_coverage_rate == 1.0
    assert report.operational_coverage_rate == 1.0
    assert report.context_substitution_entries == 4
    assert report.availability_distortion_fraction == 1.0


def test_operational_only_context_is_structural_inconsistency(tmp_path: Path) -> None:
    database = tmp_path / "confluence.sqlite3"
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    symbol = "ODD"

    source = freeze_confluence_snapshot(
        entry_event_id="odd-entry",
        contract_key=f"{symbol}|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=2),
        batches=(),
        mode=AttributionMode.SOURCE_TIME,
    )
    assert source.binding_status is ContextBindingStatus.NO_CONTEXT
    persist_confluence_snapshot(database, source)

    observation = _observation(
        symbol=symbol,
        opened=opened,
        observation_id="f" * 64,
        observed_offset_seconds=-60,
        received_offset_seconds=-50,
    )
    operational = freeze_confluence_snapshot(
        entry_event_id="odd-entry",
        contract_key=f"{symbol}|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=2),
        batches=(_batch("odd-run", (observation,)),),
        mode=AttributionMode.OPERATIONAL,
    )
    assert operational.binding_status is ContextBindingStatus.BOUND
    persist_confluence_snapshot(database, operational)

    report = evaluate_attribution_availability_bias(
        database,
        policy=AttributionBiasPolicy(min_paired_entries=1),
    )
    assert report.status is AttributionBiasStatus.INCONSISTENT
    assert report.operational_only_entries == 1


def test_unpaired_modes_are_insufficient_not_silently_imputed(tmp_path: Path) -> None:
    database = tmp_path / "confluence.sqlite3"
    opened = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    snapshot = freeze_confluence_snapshot(
        entry_event_id="single-mode",
        contract_key="ONE|CALL|10|2026-09-18",
        mff_source_ts_utc=opened,
        mff_received_ts_utc=opened + timedelta(seconds=2),
        batches=(),
        mode=AttributionMode.OPERATIONAL,
    )
    persist_confluence_snapshot(database, snapshot)
    report = evaluate_attribution_availability_bias(
        database,
        policy=AttributionBiasPolicy(min_paired_entries=1),
    )
    assert report.status is AttributionBiasStatus.INSUFFICIENT
    assert report.paired_entries == 0
