"""Research-only mathematical and adversarial tests."""
from itertools import product

import pytest

from scorpion.paper_adaptive_research import (
    allocate_diagnostic_research,
    assess_envelope,
    monitor_decay,
    robust_session_envelope,
    split_session_envelope,
)


def test_split_rank_refuses_underpowered_calibration():
    report = split_session_envelope(
        ((10.0, 10.0),)*10, train_sessions=5, clip=100, alpha=0.05,
    )
    assert report.interval_bps == (-100, 100)
    assert report.status == "ABSTAIN_INSUFFICIENT_RESOLUTION"


def test_censored_calibration_residual_never_tightens_envelope():
    train = ((10.0, 10.0),)*5
    cal = ((9.0, 9.0),)*9
    known = split_session_envelope(train+cal, train_sessions=5, clip=100, alpha=0.2)
    missing = split_session_envelope(
        train+cal[:-2]+((-100.0, 100.0),)*2, train_sessions=5, clip=100, alpha=0.2,
    )
    assert known.interval_bps == (9, 11)
    assert missing.interval_bps[0] <= known.interval_bps[0]
    assert missing.interval_bps[1] >= known.interval_bps[1]


def test_robust_interval_matches_two_session_exact_solution():
    result = robust_session_envelope(
        ((-20.0, -10.0), (20.0, 30.0)), clip=100, ratio=2,
        novel_mass=0.1, floor=0,
    )
    # q0 ranges [.25,.75]; contamination contributes [-100,100].
    assert result.stressed_identification_bps == pytest.approx((-19, 28))
    assert result.stressed_adverse_probability == pytest.approx((0.225, 0.775))
    assert sum(weight for _, weight in result.worst_weights) == pytest.approx(1)


def test_confirmed_e_process_is_dominated_by_every_missing_completion():
    bounds = ((-100.0, 100.0), (-1.0, -1.0), (-100.0, 100.0))
    kwargs = dict(planned=8, clip=100, floor=0, null=0.1, alternative=0.6, alpha=0.025)
    result = monitor_decay(bounds, **kwargs)
    for first, last in product((-1.0, 1.0), repeat=2):
        completed = monitor_decay(((first, first), (-1.0, -1.0), (last, last)), **kwargs)
        assert result.confirmed_max_log_e <= completed.confirmed_max_log_e + 1e-12
    assert result.ambiguous == 2
    assert result.confirmed_adverse == 1


def test_change_start_mixture_detects_sustained_late_deterioration():
    kwargs = dict(planned=60, clip=100, floor=0, null=0.1, alternative=0.7, alpha=0.025)
    healthy = monitor_decay(((2.0, 2.0),)*20, **kwargs)
    adverse = monitor_decay(((2.0, 2.0),)*20+((-2.0, -2.0),)*20, **kwargs)
    missing = monitor_decay(((-100.0, 100.0),)*40, **kwargs)
    assert healthy.first_alarm_index is None
    assert adverse.status == "CONFIRMED_ADVERSE_ALARM"
    assert adverse.first_alarm_index is not None and adverse.first_alarm_index > 20
    assert missing.status == "POSSIBLE_ADVERSE_WATCH"
    assert missing.confirmed_anytime_p == 1


def test_coverage_and_budget_are_explicit_and_deterministic():
    coverage = assess_envelope(((1, 2), (-100, 100), (4, 5)), (0, 3))
    assert (coverage.certainly_covered, coverage.possibly_covered, coverage.certainly_missed) == (
        1, 2, 1,
    )
    uncertainty = split_session_envelope(
        ((10.0, 10.0),)*10, train_sessions=5, clip=100, alpha=0.05,
    )
    robust = robust_session_envelope(
        ((-100.0, 100.0), (10.0, 10.0)), clip=100, ratio=2, novel_mass=0.1, floor=0,
    )
    decay = monitor_decay(
        ((-100.0, 100.0),)*10, planned=10, clip=100, floor=0,
        null=0.1, alternative=0.6, alpha=0.025,
    )
    kwargs = dict(uncertainty=uncertainty, robust=robust, decay=decay, economic=decay,
                  coverage=coverage, clip=100, budget=8)
    priorities, result = allocate_diagnostic_research(**kwargs)
    assert len(priorities) == 5
    full_support = assess_envelope(((-100, 100),), (-100, 100))
    assert full_support.certainly_covered == full_support.unresolved_outcomes == 1
    assert result.used_units <= 8
    assert result == allocate_diagnostic_research(**kwargs)[1]
    assert len({d.event_id for d in result.decisions}) == len(result.decisions)
