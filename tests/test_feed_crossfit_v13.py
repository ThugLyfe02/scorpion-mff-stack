from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.feed_integrity import (
    FeedContinuityMode,
    FeedIntegrityStatus,
    evaluate_feed_integrity,
)
from scorpion.microstructure import MarketEventKind, OptionMarketEvent
from scorpion.temporal_crossfit import (
    TemporalCrossFitPolicy,
    TemporalCrossFitStatus,
    TemporalPrediction,
    evaluate_temporal_crossfit,
)

BASE_NS = 1_789_000_000_000_000_000
CONTRACT = "AAPL|CALL|200|2026-09-18"
BASE_TS = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)


def _quote(*, sequence: int, recv_offset_ns: int, ask: str = "1.02") -> OptionMarketEvent:
    return OptionMarketEvent(
        contract_key=CONTRACT,
        kind=MarketEventKind.QUOTE,
        ts_event_ns=BASE_NS + recv_offset_ns - 1_000,
        ts_recv_ns=BASE_NS + recv_offset_ns,
        publisher_id=30,
        sequence=sequence,
        bid=Decimal("1.00"),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=10,
    )


def test_filtered_feed_cannot_self_certify_continuity():
    events = (_quote(sequence=10, recv_offset_ns=10_000),)
    report = evaluate_feed_integrity(
        events,
        continuity_mode=FeedContinuityMode.FILTERED_UNPROVEN,
        warmup_complete=True,
    )
    assert report.status is FeedIntegrityStatus.FAIL
    assert "provider_continuity_not_certified" in report.failures


def test_provider_certified_feed_passes_clean_sequence_and_warmup_evidence():
    events = (
        _quote(sequence=10, recv_offset_ns=10_000),
        _quote(sequence=11, recv_offset_ns=20_000),
    )
    report = evaluate_feed_integrity(
        events,
        continuity_mode=FeedContinuityMode.PROVIDER_CERTIFIED,
        provider_gap_events=0,
        warmup_complete=True,
    )
    assert report.status is FeedIntegrityStatus.PASS
    assert report.certified is True


def test_feed_integrity_detects_sequence_regression_and_conflicting_reuse():
    events = (
        _quote(sequence=20, recv_offset_ns=10_000),
        _quote(sequence=19, recv_offset_ns=20_000),
        _quote(sequence=20, recv_offset_ns=30_000, ask="1.05"),
    )
    report = evaluate_feed_integrity(
        events,
        continuity_mode=FeedContinuityMode.PROVIDER_CERTIFIED,
        warmup_complete=True,
    )
    assert report.status is FeedIntegrityStatus.FAIL
    assert any(item.startswith("sequence_regressions:") for item in report.failures)
    assert any(item.startswith("conflicting_sequence_ids:") for item in report.failures)


def _prediction(index: int, *, leaked: bool = False) -> TemporalPrediction:
    event_ts = BASE_TS + timedelta(minutes=index)
    truth = index % 2 == 0
    return TemporalPrediction(
        event_id=f"event-{index}",
        event_ts_utc=event_ts,
        trained_through_ts_utc=(
            event_ts if leaked else event_ts - timedelta(days=1)
        ),
        probability=0.85 if truth else 0.15,
        truth=truth,
    )


def test_temporal_crossfit_passes_well_calibrated_time_causal_predictions():
    predictions = tuple(_prediction(index) for index in range(120))
    report = evaluate_temporal_crossfit(predictions)
    assert report.status is TemporalCrossFitStatus.PASS
    assert report.passed is True
    assert report.temporal_leakage_rows == 0
    assert report.auc > 0.99
    assert report.brier < 0.05
    assert report.positive_fold_ratio == 1.0


def test_temporal_crossfit_fails_if_predictions_train_through_their_own_event():
    predictions = tuple(_prediction(index, leaked=index == 7) for index in range(40))
    report = evaluate_temporal_crossfit(
        predictions,
        policy=TemporalCrossFitPolicy(
            minimum_samples=20,
            minimum_positive_samples=5,
            folds=4,
        ),
    )
    assert report.status is TemporalCrossFitStatus.FAIL
    assert report.temporal_leakage_rows == 1
    assert "temporal_leakage_rows:1" in report.failures
