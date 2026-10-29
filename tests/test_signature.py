"""Path signatures: exact values on hand-checked paths, and the honest fit gate."""

import pytest

from scorpion.signature import (
    FitGate,
    SignatureError,
    feature_count,
    gate_fit,
    lead_lag,
    minimum_samples,
    nontrivial_feature_count,
    render,
    report,
    signature,
)


def polygon_area(points):
    """Independent oracle: signed area of the polygon a piecewise-linear path encloses."""
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        total += x0 * y1 - x1 * y0
    return total / 2.0


# ── feature counts ────────────────────────────────────────────────────────────


def test_feature_count_matches_the_geometric_series():
    assert feature_count(2, 3) == 1 + 2 + 4 + 8
    assert feature_count(2, 4) == 1 + 2 + 4 + 8 + 16
    assert feature_count(3, 2) == 1 + 3 + 9
    assert feature_count(1, 3) == 4


def test_nontrivial_count_excludes_the_constant():
    assert nontrivial_feature_count(2, 3) == 14
    assert nontrivial_feature_count(2, 2) == 6
    assert nontrivial_feature_count(1, 3) == 3


def test_bad_dimensions_and_levels_are_refused():
    with pytest.raises(SignatureError):
        feature_count(0, 3)
    with pytest.raises(SignatureError):
        feature_count(2, -1)


# ── exact values ──────────────────────────────────────────────────────────────


def test_a_single_segment_is_the_tensor_exponential():
    sig = signature([(0.0, 0.0), (2.0, 3.0)], level=3)
    assert sig.get(()) == 1.0
    assert sig.get((0,)) == 2.0
    assert sig.get((1,)) == 3.0
    assert sig.get((0, 0)) == pytest.approx(2.0)  # 2^2 / 2!
    assert sig.get((0, 1)) == pytest.approx(3.0)  # 2*3 / 2!
    assert sig.get((1, 0)) == pytest.approx(3.0)
    assert sig.get((1, 1)) == pytest.approx(4.5)  # 3^2 / 2!
    assert sig.get((0, 0, 0)) == pytest.approx(8.0 / 6.0)
    assert sig.get((0, 0, 1)) == pytest.approx(12.0 / 6.0)
    assert sig.get((0, 1, 1)) == pytest.approx(18.0 / 6.0)
    assert sig.get((1, 1, 1)) == pytest.approx(27.0 / 6.0)


def test_level_one_is_net_displacement_whatever_the_route():
    corner = signature([(0.0, 0.0), (2.0, 0.0), (2.0, 3.0)], level=2)
    straight = signature([(0.0, 0.0), (2.0, 3.0)], level=2)
    assert corner.get((0,)) == straight.get((0,)) == 2.0
    assert corner.get((1,)) == straight.get((1,)) == 3.0


def test_level_two_sees_the_corner_that_displacement_cannot():
    corner = signature([(0.0, 0.0), (2.0, 0.0), (2.0, 3.0)], level=2)
    straight = signature([(0.0, 0.0), (2.0, 3.0)], level=2)
    assert corner.get((0, 1)) == pytest.approx(6.0)  # the corner: a * b, not a*b/2
    assert straight.get((0, 1)) == pytest.approx(3.0)
    assert corner.levy_area() == pytest.approx(3.0)  # the right triangle 2*3/2
    assert straight.levy_area() == pytest.approx(0.0)  # a straight line encloses nothing


def test_levy_area_matches_the_polygon_oracle_for_arbitrary_paths():
    paths = [
        [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)],
        [(0.0, 0.0), (2.0, 0.0), (3.0, 1.0), (0.0, 4.0)],
        [(1.0, 2.0), (5.0, 1.0), (4.0, 6.0), (0.0, 0.0), (1.0, 2.0)],
    ]
    for points in paths:
        assert signature(points, level=2).levy_area() == pytest.approx(polygon_area(points))


def test_unit_square_is_one_counter_clockwise_and_minus_one_clockwise():
    ccw = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]
    cw = list(reversed(ccw))
    assert signature(ccw, level=2).levy_area() == pytest.approx(1.0)
    assert signature(cw, level=2).levy_area() == pytest.approx(-1.0)


# ── lead-lag ──────────────────────────────────────────────────────────────────


def test_lead_lag_alternates_lead_then_lag():
    assert lead_lag([0.0, 2.0, 5.0]) == [
        (0.0, 0.0),
        (2.0, 0.0),
        (2.0, 2.0),
        (5.0, 2.0),
        (5.0, 5.0),
    ]


def test_lead_lag_area_is_half_the_realized_quadratic_variation():
    series = [0.0, 2.0, 5.0, 1.0]
    increments = [series[i] - series[i - 1] for i in range(1, len(series))]
    expected = 0.5 * sum(delta * delta for delta in increments)
    path = lead_lag(series)
    assert signature(path, level=2).levy_area() == pytest.approx(expected)
    assert signature(path, level=2).levy_area() == pytest.approx(polygon_area(path))


def test_lead_lag_refuses_a_single_point():
    with pytest.raises(SignatureError):
        lead_lag([1.0])


# ── the honest gate ───────────────────────────────────────────────────────────


def test_the_gate_refuses_a_level_three_fit_on_the_corpus():
    gate = gate_fit(107, dim=2, level=3)
    assert gate.refused is True
    assert gate.nontrivial_features == 14
    assert gate.minimum_samples == 140


def test_the_gate_permits_level_two_on_the_corpus():
    gate = gate_fit(107, dim=2, level=2)
    assert gate.refused is False
    assert gate.nontrivial_features == 6
    assert gate.minimum_samples == 60


def test_the_floor_binds_even_for_a_few_features():
    gate = gate_fit(30, dim=2, level=1)
    assert gate.refused is False  # exactly at the floor is permitted
    assert gate.minimum_samples == 30
    assert gate_fit(29, dim=2, level=1).refused is True


def test_minimum_samples_floor_and_per_feature():
    assert minimum_samples(0) == 30
    assert minimum_samples(2) == 30
    assert minimum_samples(14) == 140


def test_a_fit_gate_round_trips_as_a_dataclass():
    gate = FitGate(nontrivial_features=6, minimum_samples=60, provided_samples=107,
                   refused=False, reason="ok")
    assert gate.refused is False
    assert gate.nontrivial_features == 6


# ── report and render ─────────────────────────────────────────────────────────


def test_the_report_names_every_number_and_the_refusal():
    payload = report([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)],
                     level=2, samples=107)
    assert payload["levy_area"] == pytest.approx(1.0)
    assert payload["fit_gate"]["refused"] is False
    assert payload["caveats"]
    assert render(payload).startswith("signature dim=2 level=2")


def test_the_report_refuses_at_level_three():
    payload = report([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)], level=3, samples=107)
    assert payload["fit_gate"]["refused"] is True
    assert "REFUSED" in render(payload)
