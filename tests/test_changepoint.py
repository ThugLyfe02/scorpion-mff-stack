"""CUSUM: no lookahead, measured false alarms, and a planted change detected quickly."""

from types import SimpleNamespace

import pytest

from scorpion.changepoint import (
    ChangePointError,
    Config,
    average_run_length,
    detect,
    detect_bars,
    detection_latency,
    render,
    report,
    scan,
    standardise,
)


def test_a_shifted_series_alarms_after_the_change():
    series = [0.0] * 50 + [3.0] * 20
    alarms = detect(series, Config(shift=0.5, threshold=3.0))
    assert alarms and alarms[0].index >= 50


def test_a_stationary_series_does_not_alarm_constantly():
    alarms = detect([0.0] * 200, Config(shift=0.5, threshold=3.0))
    assert alarms == []


def test_a_downward_shift_alarms_in_the_down_direction():
    series = [0.0] * 50 + [-3.0] * 20
    alarms = detect(series, Config(shift=0.5, threshold=3.0))
    assert alarms and alarms[0].direction == "down"


def test_no_lookahead_the_alarm_uses_only_prior_bars():
    prefix = [0.0] * 50 + [3.0] * 10
    alarms = detect(prefix, Config(shift=0.5, threshold=3.0))
    later = detect(prefix + [99.0] * 50, Config(shift=0.5, threshold=3.0))
    assert alarms[0].index == later[0].index


def test_reset_re_arms_instead_of_latching():
    """A sustained shift re-accumulates after each reset: resetting is what keeps it a detector."""
    series = [0.0] * 20 + [3.0] * 100
    rearming = detect(series, Config(shift=0.5, threshold=3.0))
    latched = detect(series, Config(shift=0.5, threshold=3.0, reset_after_alarm=False))
    # without a reset the statistic stays above the boundary and fires on every later bar
    assert len(latched) > len(rearming)
    assert latched[1].index == latched[0].index + 1
    assert len(rearming) > 1


def test_standardising_makes_two_scales_comparable():
    small = standardise([1.00, 1.01, 1.02, 1.00, 1.01])
    large = standardise([100.0, 101.0, 102.0, 100.0, 101.0])
    assert max(small) == pytest.approx(max(large))


def test_bad_configuration_is_refused():
    with pytest.raises(ChangePointError):
        Config(shift=0)
    with pytest.raises(ChangePointError):
        Config(threshold=0)


def test_a_planted_change_is_detected_reliably():
    payload = detection_latency(Config(), shift=2.0, runs=40, bars=200, change_at=100)
    assert payload["detected_pct"] >= 90.0
    assert payload["median_latency_bars"] is not None


def test_the_false_alarm_rate_is_measured_not_asserted():
    payload = average_run_length(Config(), runs=40, bars=300)
    assert payload["median_run"] > 0
    assert 0.0 <= payload["alarmed_within_window_pct"] <= 100.0


def test_a_bar_set_is_scanned_on_log_returns():
    bars = tuple(
        SimpleNamespace(close=1.0 if i < 10 else 1.5) for i in range(20)
    )
    barset = SimpleNamespace(contract_key="QQQ|CALL|719|2026-09-01", bars=bars)
    alarms = detect_bars(barset, Config(shift=0.5, threshold=3.0, target=0.0))
    assert isinstance(alarms, list)


def test_a_bar_set_without_enough_bars_is_empty_not_an_error():
    barset = SimpleNamespace(contract_key="X", bars=(SimpleNamespace(close=1.0),))
    assert detect_bars(barset) == []


def test_non_positive_prices_are_refused():
    bars = tuple(SimpleNamespace(close=c) for c in (1.0, 0.0, 1.0))
    with pytest.raises(ChangePointError):
        detect_bars(SimpleNamespace(contract_key="X", bars=bars))


def test_scan_names_sets_it_could_not_read():
    good = SimpleNamespace(contract_key="good", bars=tuple(SimpleNamespace(close=1.0 + i * 0.01)
                                                          for i in range(10)))
    bad = SimpleNamespace(contract_key="bad", bars=tuple(SimpleNamespace(close=c) for c in (1, 0, 1)))
    payload = scan([good, bad])
    assert "error" in payload["sets"]["bad"]


def test_the_report_states_its_caveats_and_renders():
    payload = report([0.0] * 50 + [3.0] * 50, Config(shift=0.5, threshold=3.0))
    assert payload["caveats"] and payload["first_alarm_bar"] is not None
    assert "alarms" in render(payload)
