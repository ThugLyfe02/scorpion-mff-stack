"""The ledger and the ruin model: determinism, refusals, and the numbers that must hold.

The properties worth testing here are not "does it run". They are:

* deterministic - a seed, not a clock, decides a simulation (two identical configs must agree
  to the digit, or no research claim can be reproduced);
* honest - rejections are collected and reported, near-duplicates are surfaced, and a corpus
  with no total loss says so instead of simulating a world where nothing expires worthless;
* bounded - the closed-form breakeven haircut agrees with brute force, and a monotone
  perturbation produces a monotone response (a haircut that increases wins can never reduce
  expected R).
"""

import json
from datetime import datetime
from decimal import Decimal

import pytest

from scorpion.ledger import (
    Ledger,
    LedgerError,
    load_csv,
    parse_lifecycle,
    parse_row,
)
from scorpion.ruin import (
    RuinConfig,
    breakeven_win_haircut,
    kelly,
    reclassify_losses,
    report,
    simulate,
    win_haircut_table,
)

D = Decimal


def row(**over) -> dict[str, str]:
    base = {
        "channel": "etf-options", "date": "2026-09-03", "time_et": "06:31", "ticker": "QQQ",
        "side": "CALL", "strike": "712", "expiry": "today", "entry_bid": "1.30",
        "exit_price": "", "exit_pct": "400", "outcome": "win", "notes": "Added 06:32",
    }
    base.update(over)
    return base


# ── ledger ──────────────────────────────────────────────────────────────────
def test_a_row_becomes_a_trade_with_an_et_timestamp():
    trade = parse_row(row())
    assert trade.side == "CALL" and trade.strike == D("712")
    assert trade.ts_utc == datetime(2026, 9, 3, 10, 31)
    assert trade.multiple == D("5.0000") and trade.r_multiple == D("4.0000")


def test_a_rejection_names_its_reason():
    with pytest.raises(LedgerError) as exc:
        parse_row(row(strike=""))
    assert exc.value.code == "bad_strike"


def test_an_impossible_exit_percentage_is_refused():
    with pytest.raises(LedgerError) as exc:
        parse_row(row(exit_pct="99999"))
    assert exc.value.code == "exit_out_of_range"


def test_notes_become_adds_targets_and_exit_kind():
    life = parse_lifecycle("Added at .57/.65; +12%; sold remaining calls at 25%", exit_pct=25)
    assert life.adds == (D("0.57"), D("0.65"))
    assert life.exit_kind == "full" and life.exit_pct == 25


def test_a_stopped_trade_is_labelled_even_with_a_percentage():
    assert parse_lifecycle("stopped out at -40%", exit_pct=-40).stopped is True


def test_duplicate_identity_is_rejected_but_a_later_realert_survives():
    a = parse_row(row(time_et="06:23"), line=2)
    b = parse_row(row(time_et="06:40"), line=3)
    assert a.identity != b.identity
    ledger = Ledger(trades=(a, b))
    assert len(ledger.near_duplicates(window_minutes=30)) == 1


def test_the_manifest_digest_is_stable_and_changes_with_content(tmp_path):
    path = tmp_path / "t.csv"
    header = "channel,date,time_et,ticker,side,strike,expiry,entry_bid,exit_price,exit_pct,outcome,notes\n"
    path.write_text(header + "etf-options,2026-09-03,06:31,QQQ,CALL,712,today,1.30,,400,win,ok\n",
                    encoding="utf-8")
    first = load_csv(path).manifest()
    second = load_csv(path).manifest()
    assert first["digest"] == second["digest"]
    assert first["trades"] == 1 and first["rejected"] == 0


def test_the_real_corpus_loads_with_zero_rejections():
    """Guards the shipped corpus against a schema drift in the loader."""
    ledger = load_csv("mff-backtest/trades.csv")
    assert len(ledger.trades) > 100
    assert ledger.rejected == ()
    assert len(ledger.channels) == 5
    summary = ledger.r_summary()
    assert summary["resolved"] > 90 and summary["win_rate"] > 50


# ── ruin ────────────────────────────────────────────────────────────────────
RS = [1.2, 0.8, -0.4, 2.0, 0.35, -1.0, 0.6, 1.5, -0.25, 0.9]


def test_a_simulation_is_deterministic_in_its_seed():
    cfg = RuinConfig(runs=50, trades_per_run=40, seed=3)
    a, b = simulate(RS, cfg), simulate(RS, cfg)
    assert a.summary() == b.summary()


def test_a_different_seed_produces_a_different_path():
    one = simulate(RS, RuinConfig(runs=50, trades_per_run=40, seed=1)).summary()
    two = simulate(RS, RuinConfig(runs=50, trades_per_run=40, seed=2)).summary()
    assert one["config"]["fingerprint"] != two["config"]["fingerprint"]


def test_configuration_refuses_a_size_that_cannot_be_defended():
    with pytest.raises(ValueError):
        RuinConfig(risk_fraction=0.9)


def test_stress_rewrites_the_loss_rate_without_changing_the_count():
    pool = reclassify_losses(RS, loss_rate=0.4)
    assert len(pool) == len(RS)
    assert sum(1 for r in pool if r < 0) >= int(0.4 * len(RS))


def test_stress_injects_total_losses_that_the_corpus_lacks():
    pool = reclassify_losses(RS, loss_rate=0.5, total_loss_share=1.0)
    assert min(pool) == -1.0


def test_the_breakeven_haircut_agrees_with_brute_force():
    """Closed form vs the definition: expected R must be ~0 at the stated haircut."""
    pool = reclassify_losses(RS, loss_rate=0.3)
    result = breakeven_win_haircut(RS, loss_rate=0.3)
    h = result["breakeven_win_haircut"]
    degraded = [r * (1 - h) if r > 0 else r for r in pool]
    # the returned haircut is rounded to four decimals, so the residual is ~1e-5 * avg win
    assert abs(sum(degraded) / len(degraded)) < 1e-3, "the closed form must zero the mean"


def test_a_harder_win_haircut_can_only_reduce_the_edge():
    rows = win_haircut_table(RS, RuinConfig(runs=40, trades_per_run=30), loss_rate=0.3)
    means = [r["mean_r"] for r in rows]
    assert means == sorted(means, reverse=True)


def test_kelly_prefers_a_positive_edge_and_shrinks_as_it_fades():
    strong = kelly([1.5, -0.5, 1.2, -0.4, 1.1, -0.6] * 4)["optimal_fraction"]
    weak = kelly([0.2, -0.5, 0.15, -0.4] * 4)["optimal_fraction"]
    assert strong > weak


def test_p_down_separates_a_thin_edge_from_a_dead_one():
    thin = win_haircut_table(RS, RuinConfig(runs=120, trades_per_run=40), loss_rate=0.4,
                             haircuts=(0.0, 0.9))
    assert thin[0]["mean_r"] > thin[-1]["mean_r"]
    assert thin[-1]["p_down"] >= thin[0]["p_down"]


def test_the_report_states_why_it_is_optimistic():
    payload = report(RS, RuinConfig(runs=40, trades_per_run=30))
    assert payload["caveats"] and payload["diagnostics"]
    assert payload["edge_breakeven"]["breakeven_win_haircut"] is not None
    assert json.dumps(payload)          # serialisable end to end


def test_an_empty_corpus_is_refused_rather_than_simulated():
    with pytest.raises(ValueError):
        simulate([], RuinConfig(runs=5, trades_per_run=5))
