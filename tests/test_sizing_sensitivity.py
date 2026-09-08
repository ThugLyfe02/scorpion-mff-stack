from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.execution_sensitivity import run_latency_sensitivity
from scorpion.history_archive import ArchivedDiscordMessage, HistoryArchive
from scorpion.quote_tape import HistoricalQuote, HistoricalQuoteTape
from scorpion.sizing_lab import (
    SizingConstraints,
    SizingReadiness,
    build_sizing_envelope,
    rank_segments,
)
from scorpion.strategy_selector import SelectionStatus, select_candidates


GUILD = "912747256736800838"
CHANNEL = "968352649437126676"
AUTHOR = "author"


def _trade(index: int, return_fraction: str) -> CompletedTrade:
    opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    premium = Decimal("1000")
    returned = Decimal(return_fraction)
    return CompletedTrade(
        entry_event_id=f"e{index}",
        contract_key=f"AAPL|CALL|200|2026-12-{(index % 20) + 1:02d}",
        channel_id=CHANNEL,
        author_id=AUTHOR,
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        opened_ts_utc=opened,
        closed_ts_utc=opened + timedelta(minutes=10),
        initial_quantity=5,
        add_count=0,
        trim_count=0,
        gross_premium_in=premium,
        gross_proceeds=premium * (Decimal("1") + returned),
        pnl=premium * returned,
        return_fraction=returned,
        holding_seconds=600.0,
    )


def test_strong_segment_survives_fdr_and_gets_research_sizing_envelope():
    trades = tuple(_trade(index, "0.10") for index in range(40))
    constraints = SizingConstraints(
        min_samples=30,
        max_risk_fraction=0.02,
        candidate_step=0.01,
        horizon_trades=20,
        trials=200,
        bootstrap_resamples=200,
        block_length=4,
    )
    rankings = rank_segments({"channel:core": trades}, constraints=constraints)
    metric = rankings[0]
    assert metric.readiness is SizingReadiness.READY_FOR_RESEARCH
    assert metric.fdr_q_value <= constraints.max_fdr_q_value

    candidates = select_candidates(rankings)
    assert candidates[0].status is SelectionStatus.SELECTED
    envelope = build_sizing_envelope(
        metric.segment,
        trades,
        constraints=constraints,
        metrics=metric,
    )
    assert envelope.readiness is SizingReadiness.READY_FOR_RESEARCH
    assert Decimal(str(envelope.max_research_risk_fraction)) <= Decimal("0.02")
    assert "not a guarantee" in envelope.reason


def test_thin_segment_never_emits_research_sizing():
    trades = tuple(_trade(index, "0.20") for index in range(10))
    constraints = SizingConstraints(
        min_samples=30,
        trials=100,
        bootstrap_resamples=100,
    )
    metric = rank_segments({"thin": trades}, constraints=constraints)[0]
    assert metric.readiness is SizingReadiness.INSUFFICIENT_SAMPLE
    envelope = build_sizing_envelope(
        "thin",
        trades,
        constraints=constraints,
        metrics=metric,
    )
    assert envelope.max_research_risk_fraction == 0.0
    assert envelope.selected_simulation is None


def test_latency_sensitivity_exposes_edge_decay(tmp_path):
    archive = HistoryArchive(tmp_path / "history.db")
    opened = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    closed = opened + timedelta(minutes=1)
    archive.append_many(
        (
            ArchivedDiscordMessage(
                "1",
                GUILD,
                CHANNEL,
                AUTHOR,
                opened,
                "AAPL 200C TODAY @ 1.00",
            ),
            ArchivedDiscordMessage(
                "2",
                GUILD,
                CHANNEL,
                AUTHOR,
                closed,
                "closing runners",
            ),
        )
    )
    archive.mark_channel_synced(CHANNEL, reached_beginning=True)
    contract = "AAPL|CALL|200|2026-09-08"
    delays = (60, 110, 260, 510, 1010, 2010)
    entry_asks = ("1.00", "1.02", "1.04", "1.06", "1.10", "1.14")
    exit_bids = ("1.25", "1.23", "1.21", "1.19", "1.17", "1.15")
    quotes = []
    for delay, ask, bid in zip(delays, entry_asks, exit_bids, strict=True):
        quotes.append(
            HistoricalQuote(
                contract,
                opened + timedelta(milliseconds=delay),
                Decimal(ask) - Decimal("0.02"),
                Decimal(ask),
            )
        )
        quotes.append(
            HistoricalQuote(
                contract,
                closed + timedelta(milliseconds=delay),
                Decimal(bid),
                Decimal(bid) + Decimal("0.02"),
            )
        )
    report = run_latency_sensitivity(
        archive,
        HistoricalQuoteTape(tuple(quotes)),
        channel_ids=frozenset({CHANNEL}),
        allowed_author_ids=frozenset({AUTHOR}),
        latency_grid_ms=(50, 100, 250, 500, 1000, 2000),
    )
    assert len(report.scenarios) == 6
    assert all(scenario.completed_trades == 1 for scenario in report.scenarios)
    assert report.fastest_mean_return > report.slowest_mean_return
    assert report.mean_return_range > 0
