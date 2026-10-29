"""Drift detection: exact in one dimension, calibrated rather than asserted."""

import random

import pytest

from scorpion.drift import (
    Detection,
    DriftError,
    calibrate,
    detect,
    guard,
    latest,
    profile,
    render,
    wasserstein_1d,
    windows,
)


def test_the_distance_between_identical_samples_is_zero():
    sample = [1.0, 2.0, 3.0, 4.0]
    assert wasserstein_1d(sample, sample) == pytest.approx(0.0)


def test_a_shifted_copy_has_distance_equal_to_the_shift():
    base = [float(i) for i in range(100)]
    shifted = [value + 2.0 for value in base]
    assert wasserstein_1d(base, shifted) == pytest.approx(2.0, abs=1e-6)


def test_mass_matters_not_just_position():
    left = [0.0] * 50 + [10.0] * 50
    right = [0.0] * 90 + [10.0] * 10
    assert wasserstein_1d(left, right) > 0.5


def test_empty_samples_are_refused():
    with pytest.raises(DriftError):
        wasserstein_1d([], [1.0])
    with pytest.raises(DriftError):
        windows([1.0, 2.0], size=0)


def test_windows_step_correctly():
    assert windows([1, 2, 3, 4, 5], size=2) == [[1, 2], [3, 4]]
    assert windows([1, 2, 3, 4, 5], size=2, step=1)[0] == [1, 2]
    assert len(windows([1, 2, 3, 4, 5], size=2, step=1)) == 4


def test_the_threshold_is_a_quantile_of_resampled_distances():
    rng = random.Random(11)
    reference = [rng.gauss(0.0, 1.0) for _ in range(400)]
    low = calibrate(reference, size=30, quantile=0.5, draws=200, seed=1)
    high = calibrate(reference, size=30, quantile=0.99, draws=200, seed=1)
    assert high > low > 0.0


def test_a_thin_reference_is_refused():
    with pytest.raises(DriftError):
        calibrate([1.0, 2.0, 3.0], size=10)


def test_a_bad_threshold_is_refused():
    with pytest.raises(DriftError):
        detect([1.0, 2.0], [1.0, 2.0], threshold=0.0)


def test_an_in_distribution_window_does_not_flag():
    rng = random.Random(5)
    reference = [rng.gauss(0.0, 1.0) for _ in range(500)]
    threshold = calibrate(reference, size=40, quantile=0.99, draws=400, seed=2)
    window = [rng.gauss(0.0, 1.0) for _ in range(40)]
    assert not detect(reference, window, threshold=threshold).drifted


def test_a_shifted_window_flags():
    rng = random.Random(6)
    reference = [rng.gauss(0.0, 1.0) for _ in range(500)]
    threshold = calibrate(reference, size=40, quantile=0.99, draws=400, seed=2)
    window = [rng.gauss(6.0, 1.0) for _ in range(40)]
    assert detect(reference, window, threshold=threshold).drifted


def test_the_guard_zeroes_sizes_rather_than_omitting_them():
    allocation = {"a": 0.5, "b": 0.5}
    clean = guard(Detection(0.1, 1.0, 40, 500), allocation)
    dirty = guard(Detection(2.0, 1.0, 40, 500), allocation)
    assert clean["allocated"] and clean["sizes"]["a"] == 0.5
    assert not dirty["allocated"]
    assert dirty["sizes"] == {"a": 0.0, "b": 0.0}
    assert "recalibrate" in dirty["reason"]


def test_latest_returns_the_most_recent_window():
    assert latest([1, 2, 3, 4, 5], size=2) == [4, 5]
    with pytest.raises(DriftError):
        latest([1.0], size=5)


def test_the_profile_reports_a_rate_and_renders():
    rng = random.Random(17)
    series = [rng.gauss(0.0, 1.0) for _ in range(400)]
    payload = profile(series, size=20, draws=200, seed=3)
    assert payload["windows_tested"] > 0
    assert 0.0 <= payload["flag_rate"] <= 1.0
    assert payload["caveats"]
    assert "threshold" in render(payload)
