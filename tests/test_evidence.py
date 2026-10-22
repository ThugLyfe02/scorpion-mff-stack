"""The evidence engine: intervals on money, a posterior, selection control, drift."""

from __future__ import annotations

import json
import math

import pytest

from scorpion_mff import evidence as ev
from scorpion_mff.evidence import (
    SOURCE_CORPUS,
    SOURCE_LIVE,
    STATUS_CALIBRATION,
    STATUS_CERTIFIED,
    STATUS_DEGRADED,
    STATUS_INSUFFICIENT,
    STATUS_NEGATIVE,
    STATUS_PROVISIONAL,
    Trade,
    benjamini_hochberg,
    bootstrap_mean,
    build_report,
    change_point,
    hierarchical_shrink,
    normal_cdf,
    normal_ppf,
    required_n,
    summarise,
    trades_from_fills,
)


def series(win: float, loss: float, wins: int, losses: int) -> list[float]:
    return [win] * wins + [loss] * losses


# ── distributions ────────────────────────────────────────────────────────────
def test_normal_quantiles_are_sane():
    assert normal_ppf(0.975) == pytest.approx(1.95996, abs=1e-4)
    assert normal_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
    assert normal_cdf(1.95996) == pytest.approx(0.975, abs=1e-4)
    assert normal_cdf(normal_ppf(0.9)) == pytest.approx(0.9, abs=1e-6)


def test_bootstrap_interval_brackets_the_mean_and_covers_a_positive_edge():
    xs = series(1.0, -0.5, 70, 30)
    boot = bootstrap_mean(xs, draws=2000, seed=3)
    assert boot.low < boot.mean < boot.high
    assert boot.low > 0  # a 70/30 +1/-0.5 record has a positive mean with margin
    assert boot.p_value < 0.01
    assert boot.method == "bca"


def test_constant_series_has_a_degenerate_interval():
    boot = bootstrap_mean([1.0] * 40, draws=500)
    assert boot.low == pytest.approx(1.0)
    assert boot.high == pytest.approx(1.0)
    assert boot.p_value < 0.01


def test_bootstrap_is_deterministic_for_a_seed():
    xs = [1.0, -2.0, 3.0, 0.5, -1.0, 2.0]
    assert bootstrap_mean(xs, draws=1000, seed=11) == bootstrap_mean(
        xs, draws=1000, seed=11)


def test_bayesian_posterior_separates_positive_and_negative_records():
    assert ev.bayesian_p_up(series(1.0, -0.2, 60, 40), draws=2000) > 0.9
    assert ev.bayesian_p_up(series(0.2, -1.0, 40, 60), draws=2000) < 0.1


def test_cvar_is_the_mean_of_the_worst_tail():
    xs = [-100.0] * 5 + [0.0] * 95
    summary = summarise(xs)
    assert summary.cvar_95 == pytest.approx(-100.0)


# ── selection and shrinkage ──────────────────────────────────────────────────
def test_benjamini_hochberg_is_monotone_and_matches_hand_example():
    q = benjamini_hochberg([0.001, 0.01, 0.04, 0.5])
    assert q[0] == pytest.approx(0.004)
    assert q[1] == pytest.approx(0.02)
    assert q[2] == pytest.approx(0.04 * 4 / 3)
    assert q[3] == pytest.approx(0.5)
    assert q == sorted(q)


def test_benjamini_hochberg_handles_empty_and_caps_at_one():
    assert benjamini_hochberg([]) == []
    assert max(benjamini_hochberg([0.9, 0.95, 0.99])) <= 1.0


def test_hierarchical_shrink_pulls_a_thin_channel_harder_than_a_thick_one():
    groups = {
        "thin": [20.0, 21.0, 19.0],          # mean 20, n=3
        "thick": [1.0] * 50 + [2.0] * 50,    # mean 1.5, n=100
    }
    shrunk = hierarchical_shrink(groups)
    assert shrunk["thin"][1] < shrunk["thick"][1]
    assert shrunk["thin"][0] < 20.0  # pulled toward the pooled mean
    assert shrunk["thick"][0] == pytest.approx(1.5, abs=0.05)


def test_hierarchical_shrink_with_one_channel_is_a_no_op():
    out = hierarchical_shrink({"only": [1.0, 2.0, 3.0]})
    assert out["only"] == (pytest.approx(2.0), 1.0)


# ── drift and power ──────────────────────────────────────────────────────────
def test_change_point_finds_a_real_regime_break():
    xs = [1.0, 1.2, 0.8, 1.1, 0.9] * 8 + [-1.0, -1.2, -0.8, -1.1, -0.9] * 4
    change = change_point(xs, permutations=200, seed=5)
    assert change.detected is True
    assert 34 <= change.index <= 46
    assert change.after_mean < change.before_mean


def test_change_point_does_not_invent_a_break_in_a_stationary_series():
    xs = [1.0, -1.0] * 25
    change = change_point(xs, permutations=200, seed=5)
    assert change.detected is False


def test_required_n_matches_the_textbook_and_scales_with_noise():
    n = required_n(0.5, 1.0)
    assert n is not None
    assert 24 <= n <= 26     # (1.645 + 0.842)^2 / 0.25
    assert required_n(0.25, 1.0) > n
    assert required_n(0.0, 1.0) is None
    assert required_n(0.5, 0.0) is None


# ── classification and the sizing gate ───────────────────────────────────────
def _report(source: str = SOURCE_LIVE, n: int = 60):
    trades = []
    for i in range(n):
        pnl = 100.0 if i % 3 else -50.0
        trades.append(Trade(pnl_usd=pnl, ts=float(i), channel="options-king",
                            ticker="QQQ", qty=1, entry_price=1.0))
    return build_report(trades, source=source, draws=600, permutations=100)


def test_thin_record_is_insufficient_not_a_verdict():
    report = build_report([Trade(100.0, ts=float(i), channel="etf-options")
                           for i in range(10)], draws=400)
    assert report.status == STATUS_INSUFFICIENT
    assert report.allocation is None


def test_negative_record_is_negative():
    report = build_report([Trade(-100.0, ts=float(i), channel="etf-options")
                           for i in range(40)], draws=600)
    assert report.status == STATUS_NEGATIVE


def test_a_strong_live_record_can_certify_and_allocates_at_most_half():
    report = _report(SOURCE_LIVE)
    assert report.status in (STATUS_PROVISIONAL, STATUS_CERTIFIED)
    assert report.sizing_eligible is True
    assert report.allocation is not None
    assert sum(report.allocation.values()) <= 0.5 + 1e-9


def test_corpus_evidence_can_never_size_even_when_the_record_is_strong():
    report = _report(SOURCE_CORPUS)
    assert report.status == STATUS_CALIBRATION
    assert report.sizing_eligible is False
    assert report.allocation is None
    assert "winners-heavy" in report.allocation_blocked_reason


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError, match="unknown source"):
        build_report([], source="VIBES")


def test_degrading_record_is_marked_degraded():
    xs = [2.0] * 40 + [-2.0] * 20
    trades = [Trade(x, ts=float(i), channel="etf-options") for i, x in enumerate(xs)]
    report = build_report(trades, draws=400, permutations=200)
    assert report.status == STATUS_DEGRADED


# ── inputs ───────────────────────────────────────────────────────────────────
def test_fills_reconstruct_round_trips_at_average_cost():
    records = [
        {"event": "buy", "contract_key": "QQQ|CALL|719|0DTE", "qty": 10, "price": 1.0,
         "ts": 1.0, "note": "etf-options", "accepted": True},
        {"event": "sell", "contract_key": "QQQ|CALL|719|0DTE", "qty": 5, "price": 1.2,
         "ts": 2.0, "accepted": True},
        {"event": "buy", "contract_key": "QQQ|CALL|719|0DTE", "qty": 5, "price": 1.4,
         "ts": 3.0, "note": "add", "accepted": True},
        {"event": "sell", "contract_key": "QQQ|CALL|719|0DTE", "qty": 10, "price": 0.8,
         "ts": 4.0, "accepted": True},
    ]
    trades, meta = trades_from_fills(records)
    assert len(trades) == 2
    assert trades[0].pnl_usd == pytest.approx(100.0)      # (1.2 - 1.0) * 5 * 100
    assert trades[0].channel == "etf-options"
    # Average cost after adding 5 at 1.4 is 1.2, so (0.8 - 1.2) * 10 * 100.
    assert trades[1].pnl_usd == pytest.approx(-400.0)
    assert meta["open_positions"] == 0


def test_fills_skip_unaccepted_rows_and_report_anomalies():
    records = [
        {"event": "buy", "contract_key": "SPY|PUT|500|0DTE", "qty": 2, "price": 2.0,
         "ts": 1.0, "accepted": True},
        {"event": "sell", "contract_key": "SPY|PUT|500|0DTE", "qty": 5, "price": 3.0,
         "ts": 2.0, "accepted": True},
        {"event": "sell", "contract_key": "NOPE|CALL|1|0DTE", "qty": 1, "price": 1.0,
         "ts": 3.0, "accepted": True},
        {"event": "expire", "contract_key": "QQQ|CALL|1|0DTE", "qty": 5, "price": 1.0,
         "ts": 4.0, "accepted": False},
    ]
    trades, meta = trades_from_fills(records)
    assert len(trades) == 1 and trades[0].qty == 2  # oversell clipped to holding
    assert meta["anomalies"]["oversell"] == 1
    assert meta["anomalies"]["sell_without_position"] == 1


def test_corpus_loader_prices_percent_exits_and_skips_missing_exits(tmp_path):
    path = tmp_path / "trades.json"
    path.write_text(json.dumps([
        {"date": "2026-03-10", "time_et": "08:26", "channel": "etf-options",
         "ticker": "QQQ", "entry_bid": 2.0, "exit_pct": 10.0, "outcome": "win"},
        {"date": "2026-03-11", "time_et": "08:26", "channel": "etf-options",
         "ticker": "QQQ", "entry_bid": 2.0, "exit_pct": None, "outcome": "unknown"},
    ]))
    trades, meta = ev.load_corpus(path)
    assert len(trades) == 1
    assert trades[0].pnl_usd == pytest.approx(20.0)  # 2.0 * 10% * 100
    assert meta["skipped_rows"] == 1


def test_markdown_withholds_allocation_for_the_corpus():
    report = _report(SOURCE_CORPUS)
    text = ev.to_markdown(report)
    assert "Withheld" in text
    assert "winners-heavy" in text


def test_report_json_is_serialisable():
    report = _report(SOURCE_LIVE)
    payload = json.dumps(report.to_dict())
    assert "posterior_p_up" in payload
    assert "channels" in payload
    assert math.isfinite(report.to_dict()["overall"]["ci_low"])


def test_report_json_is_strict_when_there_are_no_losses():
    report = build_report([Trade(100.0, ts=float(i), channel="etf-options")
                           for i in range(30)], draws=300)
    # profit factor is infinite; strict JSON has no Infinity literal.
    json.dumps(report.to_dict(), allow_nan=False)
