"""The risk layer: policy validation, the governor's arithmetic, and the statistics.

The tests that matter most, in order:

1. **Reduce-only is structural.** A policy that expresses x1.5 must be rejected at
   load, and `apply_contracts` must clamp even if it was not.
2. **Rules compose by minimum.** A channel tilt of 1.0 must not undo a daily stop.
3. **A stand-down is terminal until a human re-arms it**, and survives a restart.
4. The statistics answer the questions they claim to (Wilson at small n, shrinkage
   pulling small samples, drawdown measured from the peak, runs test at the edges).
"""

from __future__ import annotations

import time

import pytest

from scorpion_mff import expectancy as exp
from scorpion_mff.governor import (
    GovernorState,
    apply_contracts,
    clamp_multiplier,
    decide,
    session_for,
)
from scorpion_mff.policy import (
    MAX_MULTIPLIER,
    Policy,
    PolicyError,
    baseline,
    load,
    parse,
    write,
)

NOW = 1_790_000_000.0        # a fixed clock: every test is deterministic
DAY = time.strftime("%Y-%m-%d", time.gmtime(NOW))
NEXT_DAY = time.strftime("%Y-%m-%d", time.gmtime(NOW + 86_400))


def policy(**kw) -> Policy:
    return baseline(name="test", written_utc=NOW, **kw)


# ── policy validation ───────────────────────────────────────────────────────
def test_baseline_is_valid_and_reduce_only():
    p = policy()
    assert p.version == 1 and all(0.0 <= s.factor <= MAX_MULTIPLIER for s in p.ladder)


@pytest.mark.parametrize("bad,needle", [
    ({"channel_multipliers": {"etf-options": 1.5}}, "cannot size up"),
    ({"session_multipliers": {"open": 2.0}}, "exceeds the locked cap"),
    ({"ladder": [{"at_r": -1.0, "factor": 1.5}]}, "reduce-only"),
    ({"ladder": [{"at_r": 1.0, "factor": 0.5}]}, "must be negative"),
    ({"ladder": [{"at_r": -2.0, "factor": 0.5}, {"at_r": -1.0, "factor": 0.5}]}, "ordered"),
    ({"loss_streak": [{"at": 0, "factor": 0.5}]}, ">= 1"),
    ({"daily_loss_limit_r": None, "daily_loss_limit_usd": None}, "at least one hard daily stop"),
    ({"risk_unit_usd": 0}, "below the minimum"),
    ({"version": 2}, "refusing unknown schema"),
    ({"provenance": ""}, "provenance"),
    ({"name": ""}, "cannot be reviewed"),
])
def test_unsafe_or_malformed_policies_are_rejected(bad, needle):
    with pytest.raises(PolicyError) as caught:
        parse({**policy().to_dict(), **bad})
    assert needle in str(caught.value)


def test_every_problem_is_reported_at_once():
    with pytest.raises(PolicyError) as caught:
        parse({"version": 1, "name": "", "channel_multipliers": {"x": 3.0}})
    message = str(caught.value)
    assert "name" in message and "cannot size up" in message


def test_a_missing_policy_is_an_error_not_a_default(tmp_path):
    with pytest.raises(PolicyError):
        load(tmp_path / "nope.json")


def test_policy_round_trips_through_disk(tmp_path):
    p = policy(channel_multipliers={"etf-options": 0.5})
    path = write(p, tmp_path / "policy.json")
    again = load(path)
    assert again.channel_multipliers == {"etf-options": 0.5}
    assert again.risk_unit_usd == p.risk_unit_usd


def test_staleness_is_reported_not_enforced_per_trade():
    old = baseline(written_utc=NOW - 8 * 86_400)
    assert old.is_stale(NOW) and not policy().is_stale(NOW)


# ── the governor's arithmetic ──────────────────────────────────────────────
def test_full_size_when_nothing_binds():
    d = decide(policy(), GovernorState(), channel="etf-options", ts=NOW)
    assert d.factor == 1.0 and not d.stand_down and d.reasons == ()


def test_daily_stop_stands_down():
    state = GovernorState()
    state.record_result(-3_100.0, DAY)
    d = decide(policy(), state, channel="etf-options", ts=NOW)
    assert d.stand_down and any(r.startswith("daily_stop") for r in d.reasons)


def test_ladder_halves_then_stops():
    state = GovernorState()
    state.record_result(-1_600.0, DAY)        # -1.07R
    half = decide(policy(), state, channel="etf-options", ts=NOW)
    assert half.factor == 0.5 and not half.stand_down

    state.record_result(-1_500.0, DAY)                # total -2.07R
    stop = decide(policy(), state, channel="etf-options", ts=NOW)
    assert stop.stand_down
    assert any("ladder:-2.0R" in r or r.startswith("daily_stop") for r in stop.reasons)


def test_a_channel_tilt_cannot_undo_a_stop():
    """Rules compose by MINIMUM: the generator cannot be talked back up."""
    p = policy(channel_multipliers={"etf-options": 1.0})
    state = GovernorState()
    state.record_result(-5_000.0, DAY)        # past the daily stop
    d = decide(p, state, channel="etf-options", ts=NOW)
    assert d.stand_down and d.factor == 0.0


def test_channel_and_session_tilts_take_the_lower():
    p = policy(channel_multipliers={"etf-options": 0.75},
               session_multipliers={"open": 0.5})
    open_ts = NOW - (NOW % 86_400) + 14 * 3600        # ~10:00 ET
    d = decide(p, GovernorState(), channel="etf-options", ts=open_ts)
    assert d.factor == 0.5
    assert any(r.startswith("channel") for r in d.reasons)
    assert any(r.startswith("session:open") for r in d.reasons)


def test_loss_streak_cuts_size_and_records_the_count():
    state = GovernorState()
    state.record_result(-100.0, DAY)
    state.record_result(-100.0, DAY)
    d = decide(policy(), state, channel="etf-options", ts=NOW)
    assert d.factor == 0.5 and "n=2" in " ".join(d.reasons)


def test_a_win_resets_the_streak():
    state = GovernorState()
    state.record_result(-100.0, DAY)
    state.record_result(-100.0, DAY)
    state.record_result(500.0, DAY)
    assert state.consecutive_losses == 0
    assert decide(policy(), state, channel="etf-options", ts=NOW).factor == 1.0


def test_a_new_day_resets_the_daily_counters_but_not_the_equity_path():
    state = GovernorState()
    state.record_result(-2_000.0, DAY)
    state.record_result(-2_000.0, DAY)
    state.roll(NEXT_DAY)
    assert state.realized_today_usd == 0.0 and state.consecutive_losses == 0
    assert state.equity_usd == -4_000.0, "the drawdown anchor must not forget"


def test_max_drawdown_halts_and_the_halt_is_terminal_until_rearm():
    state = GovernorState()
    state.record_result(-9_500.0, DAY)        # -6.3R from peak
    first = decide(policy(), state, channel="etf-options", ts=NOW)
    assert first.stand_down and state.halted_reason
    # Even if equity recovered, the halt persists: it is cleared by a human, not by luck.
    state.record_result(50_000.0, NEXT_DAY)
    again = decide(policy(), state, channel="etf-options", ts=NOW + 86_400)
    assert again.stand_down and "re-arm" in " ".join(again.reasons)


# ── the reduce-only clamp, twice ───────────────────────────────────────────
def test_apply_contracts_only_reduces():
    assert apply_contracts(5, 1.0) == 5
    assert apply_contracts(5, 0.5) == 2          # floor, never round up
    assert apply_contracts(5, 0.0) == 0
    assert apply_contracts(5, 9.0) == 5, "a hand-edited policy cannot size up"
    assert apply_contracts(1, 0.99) == 0, "a fraction of one contract is no contract"


def test_clamp_rejects_nonsense():
    assert clamp_multiplier(1.5) == 1.0
    assert clamp_multiplier(-2) == 0.0
    assert clamp_multiplier(float("nan")) == 0.0


# ── state persistence: a restart must not forget a drawdown ───────────────
def test_state_survives_a_restart(tmp_path):
    state = GovernorState()
    state.record_result(-9_500.0, DAY)
    decide(policy(), state, channel="etf-options", ts=NOW)   # sets halted_reason
    path = tmp_path / "governor.json"
    state.save(path)

    revived = GovernorState.load(path)
    assert revived.halted_reason and revived.equity_usd == -9_500.0
    assert decide(policy(), revived, channel="etf-options", ts=NOW).stand_down, \
        "a relaunched process must not forget a max-drawdown halt"


def test_a_corrupt_state_file_is_an_error_not_a_reset(tmp_path):
    path = tmp_path / "governor.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        GovernorState.load(path)


def test_missing_state_file_is_a_clean_start(tmp_path):
    assert GovernorState.load(tmp_path / "absent.json").equity_usd == 0.0


# ── sessions ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("hour_et,expected", [
    (9, "off_hours"), (10, "open"), (12, "midday"), (15, "close"), (20, "off_hours"),
])
def test_session_buckets(hour_et, expected):
    base = NOW - (NOW % 86_400)                 # midnight UTC
    ts = base + (hour_et + 4) * 3600            # ET = UTC-4
    assert session_for(ts) == expected


# ── the statistics ─────────────────────────────────────────────────────────
def test_wilson_is_sane_at_the_edges():
    assert exp.wilson_interval(0, 0) == (0.0, 0.0)
    lo, hi = exp.wilson_interval(0, 10)
    assert lo == 0.0 and 0 < hi < 0.35
    lo, hi = exp.wilson_interval(10, 10)
    assert hi >= 0.999 and lo > 0.65
    lo, hi = exp.wilson_interval(5, 10)
    assert 0.2 < lo < 0.5 < hi < 0.8


def test_shrink_pulls_small_samples_harder():
    pooled = 100.0
    assert exp.shrink(500.0, pooled, n=1) < exp.shrink(500.0, pooled, n=50)
    assert exp.shrink(500.0, pooled, n=0) == pooled
    assert exp.shrink(500.0, pooled, n=10) == pytest.approx(300.0)   # half weight at n=prior


def test_summarise_handles_wins_losses_and_flats():
    stats = exp.summarise([100.0, -50.0, 0.0, 200.0])
    assert stats.n == 4 and stats.wins == 2 and stats.losses == 1 and stats.flats == 1
    assert stats.expectancy_usd == pytest.approx(62.5)
    assert stats.profit_factor == pytest.approx(6.0)


def test_drawdown_is_peak_to_trough_not_sum_of_losses():
    dd = exp.drawdown([100.0, 100.0, -300.0, 50.0], risk_unit_usd=1_500.0)
    assert dd.max_drawdown_usd == pytest.approx(300.0)
    assert dd.longest_losing_run == 1


def test_longest_losing_run_counts_consecutive_losses():
    dd = exp.drawdown([-10.0, -20.0, -30.0, 5.0, -1.0])
    assert dd.longest_losing_run == 3


def test_runs_test_flags_a_streaky_sequence():
    streaky = [-1.0] * 10 + [1.0] * 10
    assert exp.runs_test(streaky)["streaky"] is True
    alternating = [-1.0, 1.0] * 20
    assert exp.runs_test(alternating)["streaky"] is False


def test_streak_odds_fall_with_length():
    short = exp.streak_odds(0.15, 2, 100)
    long = exp.streak_odds(0.15, 4, 100)
    assert short > long > 0


def test_allocation_prefers_consistency_over_a_lucky_average():
    rows = [exp.Outcome("steady", 100.0, ts=float(i)) for i in range(20)]
    rows += [exp.Outcome("lumpy", 2000.0, ts=100.0 + i) for i in range(5)]
    rows += [exp.Outcome("lumpy", -3000.0, ts=200.0 + i) for i in range(5)]
    ranking = exp.allocation(rows)
    assert ranking[0][0] == "steady", "a $2,000 average with wild swings is not better"
