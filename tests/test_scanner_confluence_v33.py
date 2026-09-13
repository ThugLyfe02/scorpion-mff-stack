from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.scanner_batch import (
    EXPECTED_SCANNER_CONTRACT_FINGERPRINT,
    ScannerBatchRowMetadata,
    ScannerContextBatch,
)
from scorpion.scanner_confluence import (
    ConfluenceStatus,
    ScannerConfluencePolicy,
    evaluate_scanner_mff_confluence,
)
from scorpion.scanner_context import ScannerContextObservation


def _trade(symbol: str, opened: datetime, return_pct: float, event_id: str) -> CompletedTrade:
    premium_in = Decimal("100")
    return_fraction = Decimal(str(return_pct / 100.0))
    pnl = premium_in * return_fraction
    return CompletedTrade(
        entry_event_id=event_id,
        contract_key=f"{symbol}|CALL|10|2026-09-18",
        channel_id="channel",
        author_id="author",
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        opened_ts_utc=opened,
        closed_ts_utc=opened + timedelta(minutes=10),
        initial_quantity=1,
        add_count=0,
        trim_count=0,
        gross_premium_in=premium_in,
        gross_proceeds=premium_in + pnl,
        pnl=pnl,
        return_fraction=return_fraction,
        holding_seconds=600.0,
        depth_evidence_complete=True,
    )


def _observation(
    symbol: str,
    run_id: str,
    opened: datetime,
    index: int,
    *,
    received_offset_seconds: int = -240,
    age_seconds: int = 300,
) -> ScannerContextObservation:
    observed = opened - timedelta(seconds=age_seconds)
    received = opened + timedelta(seconds=received_offset_seconds)
    return ScannerContextObservation(
        observation_id=f"{index:064x}"[-64:],
        run_id=run_id,
        symbol=symbol,
        instrument_id=f"US_EQUITY:{symbol}",
        category="gapper_mobility",
        observed_at_utc=observed,
        received_at_utc=received,
        observation_time_precision="EXACT",
        availability_time_verified=True,
        market_data_as_of_utc=observed - timedelta(seconds=1),
        market_data_source="fixture-consensus",
        session="RTH",
        ross_boxes_hit=5,
        ross_boxes_known=5,
        rvol=6.0,
        rvol_basis="EXTERNAL_PROVIDED",
        tape_flag="GO",
        policy_fingerprint="p" * 64,
        quality_blocking_reasons=(),
        causal_blocking_reasons=(),
    )


def _batch(
    run_id: str,
    observations: tuple[ScannerContextObservation, ...],
    dispositions: tuple[str, ...],
    generated: datetime,
    *,
    full_universe: bool = True,
) -> ScannerContextBatch:
    metadata = tuple(
        ScannerBatchRowMetadata(
            observation_id=observation.observation_id,
            symbol=observation.symbol,
            selection_disposition=disposition,
            confidence=0.6,
            confidence_semantics="RANKING_HEURISTIC",
        )
        for observation, disposition in zip(observations, dispositions, strict=True)
    )
    selected = sum(item == "SELECTED" for item in dispositions)
    return ScannerContextBatch(
        manifest_id="m" * 64,
        run_id=run_id,
        contract_fingerprint=EXPECTED_SCANNER_CONTRACT_FINGERPRINT,
        row_count=len(observations),
        selected_symbol_count=selected,
        ordered_observation_ids=tuple(item.observation_id for item in observations),
        batch_sha256="b" * 64,
        selection_scope="FULL_UNIVERSE" if full_universe else "HITS_ONLY",
        universe_fingerprint="u" * 64,
        symbols_attempted_count=len(observations),
        symbols_with_market_data_count=len(observations),
        error_count=0,
        coverage_complete=full_universe,
        policy_fingerprint="p" * 64,
        code_revision="fixture",
        generated_at_utc=generated,
        observations=observations,
        row_metadata=metadata,
    )


def _sample(
    *,
    selected_return: float,
    not_selected_return: float,
) -> tuple[tuple[CompletedTrade, ...], tuple[ScannerContextBatch, ...]]:
    trades: list[CompletedTrade] = []
    batches: list[ScannerContextBatch] = []
    index = 1
    base = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    for day in range(3):
        opened = base + timedelta(days=day)
        observations: list[ScannerContextObservation] = []
        dispositions: list[str] = []
        run_id = f"run-{day}"
        for side, return_pct in (("S", selected_return), ("N", not_selected_return)):
            for j in range(2):
                symbol = f"{side}{day}{j}"
                event_id = f"event-{symbol}"
                trades.append(_trade(symbol, opened, return_pct, event_id))
                observations.append(_observation(symbol, run_id, opened, index))
                dispositions.append("SELECTED" if side == "S" else "NOT_SELECTED")
                index += 1
        batches.append(
            _batch(
                run_id,
                tuple(observations),
                tuple(dispositions),
                opened - timedelta(minutes=1),
            )
        )
    return tuple(trades), tuple(batches)


def test_scanner_selection_lift_on_mff_completed_trades_passes_when_stable() -> None:
    trades, batches = _sample(selected_return=12.0, not_selected_return=-3.0)
    report = evaluate_scanner_mff_confluence(
        trades,
        batches,
        policy=ScannerConfluencePolicy(
            min_selected_trades=6,
            min_not_selected_trades=6,
            min_paired_days=3,
            bootstrap_trials=500,
        ),
    )
    assert report.status is ConfluenceStatus.PASS
    assert report.causal_fresh_context_trades == 12
    assert report.comparable_full_universe_trades == 12
    assert report.selected_n == 6
    assert report.not_selected_n == 6
    assert report.mean_paired_day_lift_pct == 15.0
    assert report.bootstrap_paired_day_lift_lower_pct == 15.0


def test_broad_market_winners_do_not_fake_positive_scanner_lift() -> None:
    trades, batches = _sample(selected_return=10.0, not_selected_return=11.0)
    report = evaluate_scanner_mff_confluence(
        trades,
        batches,
        policy=ScannerConfluencePolicy(
            min_selected_trades=6,
            min_not_selected_trades=6,
            min_paired_days=3,
            bootstrap_trials=500,
        ),
    )
    assert report.status is ConfluenceStatus.FAIL
    assert report.mean_paired_day_lift_pct == -1.0
    assert any(reason.startswith("paired_day_lift_lower") for reason in report.failures)


def test_hits_only_context_can_count_for_coverage_but_not_selection_lift() -> None:
    opened = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
    symbol = "AENT"
    trade = _trade(symbol, opened, 20.0, "event-aent")
    observation = _observation(symbol, "hits", opened, 1)
    batch = _batch(
        "hits",
        (observation,),
        ("SELECTED",),
        opened - timedelta(minutes=1),
        full_universe=False,
    )
    report = evaluate_scanner_mff_confluence(
        (trade,),
        (batch,),
        policy=ScannerConfluencePolicy(
            min_selected_trades=1,
            min_not_selected_trades=1,
            min_paired_days=2,
            bootstrap_trials=100,
        ),
    )
    assert report.causal_fresh_context_trades == 1
    assert report.comparable_full_universe_trades == 0
    assert report.selected_n == 0
    assert report.status is ConfluenceStatus.INSUFFICIENT


def test_stale_scanner_context_is_not_linked() -> None:
    opened = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
    trade = _trade("AENT", opened, 20.0, "event-aent")
    observation = _observation("AENT", "run", opened, 1, age_seconds=3600)
    batch = _batch(
        "run",
        (observation,),
        ("SELECTED",),
        opened - timedelta(minutes=1),
    )
    report = evaluate_scanner_mff_confluence(
        (trade,),
        (batch,),
        policy=ScannerConfluencePolicy(
            max_context_age_seconds=1800,
            min_selected_trades=1,
            min_not_selected_trades=1,
            min_paired_days=2,
            bootstrap_trials=100,
        ),
    )
    assert report.causal_fresh_context_trades == 0
    assert report.links[0].observation_id is None


def test_backfilled_context_is_excluded_when_operational_availability_required() -> None:
    opened = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
    received = opened + timedelta(seconds=2)
    trade = _trade("AENT", opened, 20.0, "event-aent")
    # Observation existed by source clock, but bridge delivery came after MFF received the entry.
    observation = _observation(
        "AENT",
        "run",
        opened,
        1,
        received_offset_seconds=10,
    )
    batch = _batch(
        "run",
        (observation,),
        ("SELECTED",),
        opened - timedelta(minutes=1),
    )
    source_time = evaluate_scanner_mff_confluence(
        (trade,),
        (batch,),
        policy=ScannerConfluencePolicy(
            min_selected_trades=1,
            min_not_selected_trades=1,
            min_paired_days=2,
            bootstrap_trials=100,
        ),
    )
    operational = evaluate_scanner_mff_confluence(
        (trade,),
        (batch,),
        policy=ScannerConfluencePolicy(
            require_operational_availability=True,
            min_selected_trades=1,
            min_not_selected_trades=1,
            min_paired_days=2,
            bootstrap_trials=100,
        ),
        entry_received_at_by_event_id={"event-aent": received},
    )
    assert source_time.causal_fresh_context_trades == 1
    assert operational.causal_fresh_context_trades == 0
