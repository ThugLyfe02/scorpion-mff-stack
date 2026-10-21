"""Session structure: fixed edges, and averages withheld below the minimum."""

from datetime import datetime, timedelta, timezone

import pytest

from scorpion.session import (
    MIN_SAMPLE,
    SessionError,
    bucket,
    minutes_since_open,
    render,
    summarise,
)

ET = timezone(timedelta(hours=-4))


def at(hour, minute=0):
    return datetime(2026, 8, 13, hour, minute, tzinfo=ET)


def test_the_open_is_its_own_bucket():
    assert bucket(at(9, 35)) == "open"
    assert bucket(at(10, 0)) == "morning"


def test_the_edges_are_half_open_and_do_not_overlap():
    assert bucket(at(11, 59)) == "morning"
    assert bucket(at(12, 0)) == "midday"
    assert bucket(at(15, 30)) == "close"


def test_a_pre_market_timestamp_is_outside():
    assert bucket(at(6, 23)) == "outside"
    assert bucket(at(17, 0)) == "outside"


def test_minutes_since_open_is_negative_before_the_bell():
    assert minutes_since_open(at(9, 0)) == pytest.approx(-30.0)
    assert minutes_since_open(at(10, 0)) == pytest.approx(30.0)


def test_trades_are_grouped_with_totals_preserved():
    rows = [
        {"entry_ts": at(9, 40).isoformat(), "realized_r": 1.0, "realized_usd": 150.0},
        {"entry_ts": at(9, 50).isoformat(), "realized_r": -0.5, "realized_usd": -75.0},
        {"entry_ts": at(14, 10).isoformat(), "realized_r": 0.2, "realized_usd": 30.0},
    ]
    summary = summarise(rows)
    assert summary.trades == 3
    assert summary.buckets["open"].trades == 2
    assert summary.buckets["open"].r_total == pytest.approx(0.5)
    assert summary.buckets["open"].wins == 1


def test_a_thin_bucket_reports_counts_but_withholds_means():
    rows = [{"entry_ts": at(12, 10).isoformat(), "realized_r": 2.0, "realized_usd": 300.0}]
    payload = summarise(rows).to_dict()
    midday = next(row for row in payload["buckets"] if row["bucket"] == "midday")
    assert midday["trades"] == 1
    assert midday["mean_r"] is None and midday["win_rate"] is None
    assert midday["r_total"] == pytest.approx(2.0)
    assert midday["decisive"] is False


def test_a_bucket_becomes_decisive_at_the_minimum():
    rows = [{"entry_ts": at(10, i).isoformat(), "realized_r": 1.0, "realized_usd": 150.0}
            for i in range(MIN_SAMPLE)]
    payload = summarise(rows).to_dict()
    morning = next(row for row in payload["buckets"] if row["bucket"] == "morning")
    assert morning["decisive"] is True
    assert morning["win_rate"] == pytest.approx(1.0)


def test_the_best_bucket_needs_enough_trades():
    thin = summarise([{"entry_ts": at(9, 35).isoformat(), "realized_r": 9.0,
                       "realized_usd": 1.0}])
    assert thin.best() is None


def test_a_missing_or_bad_timestamp_is_refused():
    with pytest.raises(SessionError):
        summarise([{"realized_r": 1.0}])
    with pytest.raises(SessionError):
        summarise([{"entry_ts": "not-a-time", "realized_r": 1.0}])


def test_outside_session_trades_are_reported_separately():
    rows = [{"entry_ts": at(6, 23).isoformat(), "realized_r": 1.0, "realized_usd": 100.0}]
    payload = summarise(rows).to_dict()
    assert any(row["bucket"] == "outside" for row in payload["buckets"])


def test_render_marks_withheld_buckets():
    text = render(summarise([{"entry_ts": at(12, 10).isoformat(), "realized_r": 1.0,
                              "realized_usd": 10.0}]))
    assert "withheld" in text
