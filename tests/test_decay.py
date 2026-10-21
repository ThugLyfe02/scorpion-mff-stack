"""Alpha decay: forward returns, monotone-only half-lives, and refusals that carry the finding."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from scorpion.decay import (
    MIN_TRADES,
    DecayError,
    Point,
    curve,
    forward_return,
    from_ledger,
    half_life,
    is_monotone_decreasing,
    marks_after,
    render,
    report,
    summarise,
)

T0 = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)


def bars(prices, step=5):
    return SimpleNamespace(bars=tuple(
        SimpleNamespace(ts_utc=T0 + timedelta(minutes=step * i), close=price)
        for i, price in enumerate(prices)
    ))


def point(minutes, mean, n=6, stderr=0.001):
    return Point(minutes, n, mean, stderr)


def test_forward_return_is_relative_to_the_entry():
    assert forward_return(1.0, 1.2) == pytest.approx(0.2)
    assert forward_return(2.0, 1.0) == pytest.approx(-0.5)


def test_a_zero_entry_is_refused():
    with pytest.raises(DecayError):
        forward_return(0.0, 1.0)


def test_marks_use_the_last_bar_at_or_before_each_horizon():
    barset = bars([1.0, 1.1, 1.2, 1.3])
    marks = marks_after(barset, T0, (5, 15))
    assert marks[5] == 1.1 and marks[15] == 1.3


def test_a_horizon_beyond_the_bars_is_omitted_not_extrapolated():
    marks = marks_after(bars([1.0, 1.1]), T0, (5, 600))
    assert 600 not in marks


def test_a_thin_horizon_is_reported_with_its_count():
    points = curve([(1.0, {5: 1.1})] * 2)
    assert points[0].n == 2 and not points[0].above_noise


def test_the_curve_averages_across_trades():
    pairs = [(1.0, {5: 1.1})] * 6
    points = curve(pairs)
    assert points[0].mean_return == pytest.approx(0.1)
    assert points[0].n == 6 and points[0].n >= MIN_TRADES


def test_a_decaying_path_is_monotone():
    assert is_monotone_decreasing([point(5, 0.10), point(15, 0.06), point(30, 0.01)])


def test_a_rising_path_is_not_monotone():
    assert not is_monotone_decreasing([point(5, 0.01), point(15, 0.08)])


def test_half_life_interpolates_between_the_bracketing_horizons():
    fitted = half_life([point(5, 0.10), point(15, 0.04), point(30, 0.01)])
    assert fitted["identified"] and 5 < fitted["half_life_minutes"] < 15


def test_a_noise_level_short_horizon_refuses_a_half_life():
    fitted = half_life([
        point(5, 0.001, stderr=0.01),
        point(15, 0.0005, stderr=0.01),
        point(30, 0.0002, stderr=0.01),
    ])
    assert fitted["identified"] is False and "standard error" in fitted["reason"]


def test_a_non_monotone_path_refuses_a_half_life():
    fitted = half_life([point(5, 0.10), point(15, 0.12), point(30, 0.01)])
    assert fitted["identified"] is False and "monotone" in fitted["reason"]


def test_a_path_that_never_halves_is_reported_as_such():
    fitted = half_life([point(5, 0.10), point(15, 0.09), point(30, 0.08)])
    assert fitted["identified"] is False and "never halved" in fitted["reason"]


def test_too_few_horizons_refuse_fitting():
    assert half_life([point(5, 0.1), point(15, 0.05)])["identified"] is False


def test_the_report_states_caveats_and_renders():
    payload = report([point(5, 0.10), point(15, 0.04), point(30, 0.01)])
    assert payload["caveats"] and payload["monotone"] is True
    assert "horizon" in render(payload)
    assert summarise(payload)["identified"] is True


def test_from_ledger_skips_trades_without_bars():
    ledger = SimpleNamespace(trades=[SimpleNamespace(
        ticker="QQQ", side="CALL", strike=1, day=datetime(2026, 9, 1).date(),
        entry_bid=1.0, ts_utc=T0,
    )])
    # the curve always reports every horizon, with its count: an empty sample is n=0, not silence
    assert all(p.n == 0 for p in from_ledger(ledger, {}))
