"""Survival with censoring, and the per-day drill that proves the log is a pure fold."""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from scorpion.drill import DrillError, drill_day, fingerprint, pick, render, run
from scorpion.pipeline import Pipeline
from scorpion.scenario import message
from scorpion.store import Store
from scorpion.survival import (
    MIN_SAMPLE,
    Observation,
    SurvivalError,
    by_channel,
    compare,
    distribution,
    hazard_table,
    hazards_are_falling,
    kaplan_meier,
    report,
)
from scorpion.survival import (
    render as render_survival,
)


# ── Kaplan-Meier ───────────────────────────────────────────────────────────
def test_all_exits_at_the_same_time_give_a_step_to_zero():
    curve = kaplan_meier([Observation(10.0) for _ in range(5)])
    assert curve.points == ((10.0, 0.0),)
    assert curve.median() == 10.0


def test_censoring_leaves_the_curve_above_zero():
    curve = kaplan_meier([Observation(10.0), Observation(20.0, censored=True)])
    assert curve.censoring_rate == 50.0
    assert curve.survival_at(30) > 0.0
    assert curve.median() == 10.0          # one exit among two reaches exactly 0.5


def test_the_product_limit_uses_the_risk_set():
    observed = [Observation(5.0), Observation(10.0), Observation(10.0), Observation(20.0)]
    curve = kaplan_meier(observed)
    # first death at 5 among 4 -> 0.75; two deaths at 10 among 3 -> 0.75 * (1/3) = 0.25
    assert curve.points[0] == (5.0, 0.75)
    assert abs(curve.survival_at(10.0) - 0.25) < 1e-9
    assert curve.median() == 10.0


def test_negative_durations_are_refused():
    with pytest.raises(SurvivalError):
        Observation(-1.0)


def test_an_empty_sample_is_an_empty_curve():
    curve = kaplan_meier([])
    assert curve.n == 0 and curve.median() is None


def test_quantiles_are_ordered():
    sample = [Observation(float(m)) for m in (5, 10, 15, 20, 30, 45, 60)]
    curve = kaplan_meier(sample)
    assert curve.quantile(0.25) <= curve.median() <= curve.quantile(0.75)


# ── hazards ────────────────────────────────────────────────────────────────
def test_hazard_comparison_reads_the_first_and_last_usable_buckets():
    falling = [
        {"bucket": "0-5m", "start": 0, "end": 5, "at_risk": 20, "exits": 12, "hazard_pct": 60.0},
        {"bucket": "5-15m", "start": 5, "end": 15, "at_risk": 8, "exits": 2, "hazard_pct": 25.0},
    ]
    rising = [
        {"bucket": "0-5m", "start": 0, "end": 5, "at_risk": 20, "exits": 2, "hazard_pct": 10.0},
        {"bucket": "5-15m", "start": 5, "end": 15, "at_risk": 18, "exits": 9, "hazard_pct": 50.0},
    ]
    assert hazards_are_falling(falling) is True
    assert hazards_are_falling(rising) is False


def test_hazard_table_reports_the_risk_set_it_used():
    sample = ([Observation(3.0)] * 6) + ([Observation(30.0)] * 6)
    rows = hazard_table(sample)
    assert rows and all(row["at_risk"] >= row["exits"] for row in rows)


def test_hazard_needs_a_usable_risk_set():
    assert hazards_are_falling(hazard_table([Observation(3.0)])) is None


def test_distribution_buckets_by_time():
    payload = distribution([Observation(3.0), Observation(10.0), Observation(200.0)])
    assert payload["<5m"] == 1 and payload["<15m"] == 1 and payload[">=120m"] == 1


# ── refusal and reporting ──────────────────────────────────────────────────
def test_a_channel_below_the_floor_is_withheld():
    sample = [Observation(10.0, channel="thin") for _ in range(3)]
    payload = by_channel(sample)
    assert "withheld" in payload["thin"]


def test_comparison_refuses_below_the_floor():
    small = [Observation(10.0)] * 2
    assert compare(small, small)["compared"] is False


def test_report_states_its_caveats():
    payload = report([Observation(float(m)) for m in range(1, 12)])
    assert payload["caveats"] and payload["floor"] == MIN_SAMPLE
    assert "median" in render_survival(payload)


# ── drill ──────────────────────────────────────────────────────────────────
def seeded(tmp_path):
    store = Store(tmp_path / "d.db")
    pipeline = Pipeline(store=store)
    base = datetime.now(UTC).replace(microsecond=0)
    asyncio.run(pipeline.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=base)))
    asyncio.run(pipeline.handle(
        message("SPY 600P TODAY @ 0.90", message_id="m2", at=base + timedelta(days=1))
    ))
    return store, pipeline


def test_an_empty_log_cannot_be_drilled(tmp_path):
    with pytest.raises(DrillError):
        pick(Store(tmp_path / "empty.db"), count=3)


def test_every_chosen_day_replays_on_top_of_its_prefix(tmp_path):
    store, _ = seeded(tmp_path)
    report_ = run(store, count=3, seed=1)
    assert report_.ok, [r.to_dict() for r in report_.results]


def test_picking_is_deterministic_by_seed(tmp_path):
    store, _ = seeded(tmp_path)
    assert pick(store, count=2, seed=7) == pick(store, count=2, seed=7)


def test_a_day_with_no_rows_is_vacuously_consistent(tmp_path):
    store, _ = seeded(tmp_path)
    result = drill_day(store, date(2020, 1, 2))
    assert result.consistent and result.rows == 0


def test_the_drill_render_names_each_day(tmp_path):
    store, _ = seeded(tmp_path)
    text = render(run(store, count=2, seed=3))
    assert "seed 3" in text and "CONSISTENT" in text


def test_the_log_fingerprint_is_stable(tmp_path):
    store, _ = seeded(tmp_path)
    assert fingerprint(store) == fingerprint(store)
    assert len(fingerprint(store)) == 16
