"""Out-of-sample tournaments: fold discipline, selection audits, and honest refusals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from scorpion.attribution import BASELINE, Variant
from scorpion.tournament import (
    DEFAULT_SEED,
    TournamentError,
    VariantRun,
    build_folds,
    deflated_sharpe,
    paired_block_bootstrap,
    probability_of_backtest_overfitting,
    render,
    tournament,
)

BASE = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def _ledger(days: int = 8, per_day: int = 3) -> Any:
    trades = []
    for day in range(days):
        for trade in range(per_day):
            identity = f"t{day}-{trade}"
            trades.append(
                SimpleNamespace(
                    identity=identity,
                    ts_utc=BASE + timedelta(days=day, minutes=trade),
                )
            )
    return SimpleNamespace(trades=tuple(trades))


def _run(name: str, values: dict[str, float], *, taken: bool = True) -> VariantRun:
    return VariantRun(
        variant=Variant(name),
        r_by_identity=dict(values),
        taken_by_identity={identity: taken for identity in values},
        summary={"taken": len(values) if taken else 0},
        verified=True,
    )


def test_folds_are_chronological_and_purge_the_boundary():
    days = [BASE.date() + timedelta(days=index) for index in range(12)]
    folds = build_folds(days, n_folds=4, purge_days=1)
    assert folds
    for fold in folds:
        assert fold.train_days
        assert fold.test_days
        assert max(fold.train_days) < min(fold.test_days)
        assert not set(fold.train_days) & set(fold.test_days)
        assert fold.purged_days
    assert [fold.index for fold in folds] == list(range(1, len(folds) + 1))


def test_folds_refuse_too_few_days_or_bad_gaps():
    with pytest.raises(TournamentError, match="insufficient_days"):
        build_folds([BASE.date()], n_folds=4)
    with pytest.raises(TournamentError, match="bad_gap"):
        build_folds([BASE.date() + timedelta(days=index) for index in range(8)], purge_days=-1)


def test_deflated_sharpe_rewards_a_strong_series_and_refuses_a_thin_one():
    strong = [0.5, 0.3, 0.6, 0.4, 0.5, 0.2, 0.7, 0.3] * 5
    result = deflated_sharpe(strong, trials=5)
    assert result.refused == ""
    assert result.deflated_sharpe > 0.95

    thin = deflated_sharpe([0.1, 0.2], trials=5)
    assert thin.refused == "insufficient_observations"

    flat = deflated_sharpe([0.5] * 30, trials=5)
    assert flat.refused == "zero_variance"


def test_deflated_sharpe_prices_the_number_of_trials():
    returns = [0.4, 0.1, 0.3, 0.2, 0.5, 0.0, 0.4, 0.1] * 4
    one = deflated_sharpe(returns, trials=1)
    many = deflated_sharpe(returns, trials=100)
    assert many.expected_max_sharpe > one.expected_max_sharpe
    assert many.deflated_sharpe < one.deflated_sharpe


def test_pbo_is_zero_when_one_variant_dominates_and_positive_when_the_winner_reverses():
    dominant = probability_of_backtest_overfitting(
        {"a": [1.0] * 32, "b": [0.0] * 32, "c": [-1.0] * 32}
    )
    assert dominant.pbo == 0.0

    reversal = probability_of_backtest_overfitting(
        {
            "overfit": [1.0] * 16 + [-1.0] * 16,
            "steady": [0.1] * 32,
        },
        n_splits=8,
    )
    assert reversal.pbo > 0.0
    assert reversal.splits > 0


def test_pbo_refuses_a_ragged_or_short_matrix():
    with pytest.raises(TournamentError, match="ragged_matrix"):
        probability_of_backtest_overfitting({"a": [0.0], "b": [0.0, 1.0]})
    with pytest.raises(TournamentError, match="insufficient_observations"):
        probability_of_backtest_overfitting({"a": [0.0] * 8, "b": [0.0] * 8})


def test_paired_bootstrap_keeps_the_difference_and_reports_a_p_value():
    selected = [0.5] * 100
    baseline = [0.1] * 100
    result = paired_block_bootstrap(selected, baseline, draws=500, seed=DEFAULT_SEED)
    assert result.observed_edge == pytest.approx(0.4)
    assert result.low > 0.0
    assert result.p_value < 0.05

    same = paired_block_bootstrap(baseline, baseline, draws=200, seed=DEFAULT_SEED)
    assert same.observed_edge == 0.0
    assert same.p_value == pytest.approx(1.0)


def test_paired_bootstrap_refuses_ragged_pairs():
    with pytest.raises(TournamentError, match="ragged_pairs"):
        paired_block_bootstrap([0.1], [], draws=10)


def test_tournament_selects_on_train_and_scores_on_test(monkeypatch):
    ledger = _ledger()
    identities = [trade.identity for trade in ledger.trades]
    values = {
        BASELINE.name: {identity: 0.1 for identity in identities},
        "good": {
            identity: 0.1 + (index % 5) * 0.15 for index, identity in enumerate(identities)
        },
        "bad": {identity: -0.4 for identity in identities},
    }

    def fake_run(ledger: Any, variant: Variant, *, db_path: Any) -> VariantRun:
        return _run(variant.name, values[variant.name])

    monkeypatch.setattr("scorpion.tournament.run_variant", fake_run)
    result = tournament(
        ledger,
        (Variant("good"), Variant("bad")),
        db_path_for=lambda name: f"/tmp/{name}.db",
        n_folds=3,
        min_total_trades=10,
        min_fold_trades=1,
        min_fold_test_trades=1,
        bootstrap_draws=300,
    )
    assert result.verdict in {"SUPPORTED", "FRAGILE"}
    assert result.selection_counts["good"] == len(result.folds)
    assert result.oos_edge > 0.0
    assert result.test_trades > 0
    assert result.deflated.trials == 3
    assert result.pbo.splits > 0


def test_tournament_refuses_when_the_corpus_cannot_support_a_claim(monkeypatch):
    ledger = _ledger(days=2, per_day=1)
    identities = [trade.identity for trade in ledger.trades]

    def fake_run(ledger: Any, variant: Variant, *, db_path: Any) -> VariantRun:
        return _run(variant.name, {identity: 0.1 for identity in identities})

    monkeypatch.setattr("scorpion.tournament.run_variant", fake_run)
    with pytest.raises(TournamentError, match="insufficient_days"):
        tournament(ledger, (Variant("good"),), db_path_for=lambda name: name, n_folds=4)


def test_tournament_refuses_an_incomplete_replay(monkeypatch):
    ledger = _ledger()
    identities = [trade.identity for trade in ledger.trades]

    def fake_run(ledger: Any, variant: Variant, *, db_path: Any) -> VariantRun:
        values = {identity: 0.1 for identity in identities}
        if variant.name == "broken":
            values.pop(identities[0])
        return _run(variant.name, values)

    monkeypatch.setattr("scorpion.tournament.run_variant", fake_run)
    with pytest.raises(TournamentError, match="incomplete_replay"):
        tournament(ledger, (Variant("broken"),), db_path_for=lambda name: name)


def test_tournament_report_is_json_shaped_and_renders(monkeypatch):
    ledger = _ledger()
    identities = [trade.identity for trade in ledger.trades]
    values = {
        BASELINE.name: {identity: 0.1 for identity in identities},
        "good": {identity: 0.2 for identity in identities},
    }

    def fake_run(ledger: Any, variant: Variant, *, db_path: Any) -> VariantRun:
        return _run(variant.name, values[variant.name])

    monkeypatch.setattr("scorpion.tournament.run_variant", fake_run)
    result = tournament(
        ledger,
        (Variant("good"),),
        db_path_for=lambda name: name,
        n_folds=2,
        min_total_trades=5,
        min_fold_trades=1,
        min_fold_test_trades=1,
        bootstrap_draws=100,
    )
    payload = result.to_dict()
    assert payload["verdict"] in {"SUPPORTED", "FRAGILE", "REJECTED"}
    assert "caveats" in payload
    assert "deflated_sharpe" in payload
    assert "PBO" in render(payload)


def test_tournament_can_support_a_rule_and_can_hold_the_incumbent(monkeypatch):
    ledger = _ledger(days=10)
    identities = [trade.identity for trade in ledger.trades]

    def run_with(baseline_value: float, good_values: list[float]) -> Any:
        values = {
            BASELINE.name: {identity: baseline_value for identity in identities},
            "good": {
                identity: good_values[index % len(good_values)]
                for index, identity in enumerate(identities)
            },
        }

        def fake_run(ledger: Any, variant: Variant, *, db_path: Any) -> VariantRun:
            return _run(variant.name, values[variant.name])

        return fake_run

    monkeypatch.setattr(
        "scorpion.tournament.run_variant", run_with(0.1, [0.5, 0.4, 0.6])
    )
    supported = tournament(
        ledger,
        (Variant("good"),),
        db_path_for=lambda name: name,
        n_folds=3,
        min_total_trades=10,
        min_fold_trades=1,
        min_fold_test_trades=1,
        bootstrap_draws=500,
    )
    assert supported.verdict == "SUPPORTED"
    assert supported.selection_counts["good"] == len(supported.folds)

    monkeypatch.setattr(
        "scorpion.tournament.run_variant", run_with(0.2, [0.1, 0.1, 0.1])
    )
    held = tournament(
        ledger,
        (Variant("good"),),
        db_path_for=lambda name: name,
        n_folds=3,
        min_total_trades=10,
        min_fold_trades=1,
        min_fold_test_trades=1,
        bootstrap_draws=200,
    )
    assert held.verdict == "INCUMBENT_HELD"
    assert "incumbent_selected_every_fold" in held.reasons
