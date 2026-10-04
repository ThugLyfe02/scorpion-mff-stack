from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.conformal import (
    ConformalStatus,
    evaluate_conformal_calibrator,
    fit_conformal_calibrator,
)
from scorpion.domain import EventKind
from scorpion.history_archive import ArchivedDiscordMessage, HistoryArchive
from scorpion.latency_value import LatencyAxis, run_latency_value_frontier
from scorpion.microstructure import MarketEventKind, OptionMarketEvent, OptionMicrostructureTape

GUILD = "912747256736800838"
CHANNEL = "968352649437126676"
AUTHOR = "author"
CONTRACT = "AAPL|CALL|200|2026-09-08"
BASE = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
LABELS = (EventKind.ENTRY, EventKind.IGNORE, EventKind.AMBIGUOUS)


def _row(entry: float, ignore: float, ambiguous: float) -> dict[EventKind, float]:
    return {
        EventKind.ENTRY: entry,
        EventKind.IGNORE: ignore,
        EventKind.AMBIGUOUS: ambiguous,
    }


def test_conformal_prediction_sets_reject_dangerous_actionable_singleton_errors():
    calibration_rows = [_row(0.99, 0.005, 0.005) for _ in range(200)]
    calibration_truth = [EventKind.ENTRY for _ in range(200)]
    calibrator = fit_conformal_calibrator(
        calibration_rows,
        calibration_truth,
        labels=LABELS,
        alpha=0.01,
    )

    good_rows = [_row(0.99, 0.005, 0.005) for _ in range(200)]
    good_truth = [EventKind.ENTRY for _ in range(200)]
    good = evaluate_conformal_calibrator(calibrator, good_rows, good_truth)
    assert good.status is ConformalStatus.QUALIFIED
    assert good.qualified is True
    assert good.singleton_rate == 1.0
    assert good.actionable_singleton_errors == 0

    bad_rows = list(good_rows)
    bad_truth = list(good_truth)
    for index in (0, 1):
        bad_rows[index] = _row(0.99, 0.005, 0.005)
        bad_truth[index] = EventKind.IGNORE
    bad = evaluate_conformal_calibrator(calibrator, bad_rows, bad_truth)
    assert bad.qualified is False
    assert bad.status is ConformalStatus.FAILED
    assert bad.actionable_singleton_errors == 2
    assert any("actionable_singleton_error_rate" in item for item in bad.failures)


def _message(message_id: str, ts: datetime, content: str) -> ArchivedDiscordMessage:
    return ArchivedDiscordMessage(
        message_id=message_id,
        guild_id=GUILD,
        channel_id=CHANNEL,
        author_id=AUTHOR,
        source_ts_utc=ts,
        content=content,
    )


def test_latency_value_frontier_replays_real_microstructure_lifecycle(tmp_path):
    archive = HistoryArchive(tmp_path / "history.db")
    archive.append(_message("entry", BASE, "AAPL 200C TODAY @ 1.00"))
    archive.append(_message("exit", BASE + timedelta(seconds=60), "closing runners"))

    entry_ns = int(BASE.timestamp() * 1_000_000_000)
    exit_ns = int((BASE + timedelta(seconds=60)).timestamp() * 1_000_000_000)
    tape = OptionMicrostructureTape(
        (
            OptionMarketEvent(
                contract_key=CONTRACT,
                kind=MarketEventKind.QUOTE,
                ts_event_ns=entry_ns,
                ts_recv_ns=entry_ns,
                publisher_id=30,
                sequence=1,
                bid=Decimal("0.95"),
                ask=Decimal("1.00"),
                bid_size=10,
                ask_size=10,
            ),
            OptionMarketEvent(
                contract_key=CONTRACT,
                kind=MarketEventKind.QUOTE,
                ts_event_ns=exit_ns,
                ts_recv_ns=exit_ns,
                publisher_id=30,
                sequence=2,
                bid=Decimal("1.20"),
                ask=Decimal("1.25"),
                bid_size=10,
                ask_size=10,
            ),
        )
    )

    report = run_latency_value_frontier(
        archive,
        tape,
        channel_ids=frozenset({CHANNEL}),
        allowed_author_ids=frozenset({AUTHOR}),
        decision_grid_ms=(50, 500),
        order_transport_grid_ms=(0,),
        feed_transport_grid_ms=(0,),
    )
    decision_results = tuple(
        item for item in report.scenarios if item.axis is LatencyAxis.DECISION
    )
    assert len(decision_results) == 2
    assert all(item.completed_trades == 1 for item in decision_results)
    assert all(item.mean_return > 0 for item in decision_results)
    assert len(report.marginal_values) == 1
    assert report.marginal_values[0].axis is LatencyAxis.DECISION
