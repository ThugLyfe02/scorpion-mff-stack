"""No-regret weighting: uniform start, bounded regret, and adaptation when the leader changes."""

import random

import pytest

from scorpion.experts import (
    ExpertsError,
    Hedge,
    from_returns,
    learning_rate,
    regret_bound,
    render,
    report,
    run,
    switches,
    weight_path,
)


def test_the_hedge_rate_shrinks_as_the_horizon_grows():
    assert learning_rate(1000, 4) < learning_rate(10, 4)


def test_the_regret_bound_grows_with_rounds_and_rules():
    assert regret_bound(100, 4) > regret_bound(100, 2)
    assert regret_bound(200, 4) > regret_bound(100, 4)


def test_bad_shapes_are_refused():
    with pytest.raises(ExpertsError):
        learning_rate(0, 3)
    with pytest.raises(ExpertsError):
        Hedge(n_experts=0)
    with pytest.raises(ExpertsError):
        run({})


def test_a_fresh_hedge_is_uniform():
    hedge = Hedge(n_experts=4, eta=0.1)
    assert hedge.weights == [0.25] * 4
    assert hedge.concentration() == pytest.approx(0.25)


def test_an_update_shifts_weight_toward_the_lower_loss():
    hedge = Hedge(n_experts=2, eta=0.5)
    weights = hedge.update([1.0, 0.0])
    assert weights[1] > weights[0]


def test_weights_stay_a_distribution_under_extreme_losses():
    hedge = Hedge(n_experts=3, eta=5.0)
    for _ in range(50):
        hedge.update([1000.0, 0.0, 500.0])
    assert sum(hedge.weights) == pytest.approx(1.0)
    assert all(0.0 <= w <= 1.0 for w in hedge.weights)


def test_a_ragged_mapping_is_refused():
    with pytest.raises(ExpertsError):
        run({"a": [1.0, 2.0], "b": [1.0]})


def test_the_best_expert_is_identified_and_regret_is_bounded():
    rng = random.Random(4)
    losses = {
        "good": [rng.gauss(0.0, 0.1) for _ in range(300)],
        "bad": [rng.gauss(1.0, 0.1) for _ in range(300)],
    }
    # the pure Hedge bound applies at alpha=0; fixed share deliberately pays a little extra regret
    result = run(losses, alpha=0.0)
    assert result.names[result.best_expert] == "good"
    assert result.within_bound, (result.regret, result.bound)


def test_weights_move_when_the_leader_changes():
    first = [0.0] * 100 + [2.0] * 100
    second = [2.0] * 100 + [0.0] * 100
    result = run({"a": first, "b": second}, alpha=0.05)
    path = weight_path(result)
    assert path["a"][60] > path["b"][60]
    assert path["b"][180] > path["a"][180]


def test_switches_are_reported_with_the_rule_that_took_over():
    first = [0.0] * 60 + [3.0] * 60
    second = [3.0] * 60 + [0.0] * 60
    result = run({"a": first, "b": second}, alpha=0.05)
    changes = switches(result)
    assert changes and any(c["to"] == "b" for c in changes)


def test_pure_noise_leaves_the_weights_near_uniform():
    rng = random.Random(9)
    losses = {f"e{i}": [rng.gauss(1.0, 1.0) for _ in range(200)] for i in range(4)}
    result = run(losses)
    # noise should not produce commitment: concentration stays well below a single-rule allocation
    assert result.rounds[-1].weights
    assert max(result.rounds[-1].weights) < 0.75


def test_returns_are_scored_as_negative_losses():
    result = from_returns({"winner": [1.0] * 50, "loser": [-1.0] * 50})
    assert result.names[result.best_expert] == "winner"


def test_the_report_carries_caveats_and_renders():
    payload = report(from_returns({"a": [0.5] * 30, "b": [-0.2] * 30}))
    assert payload["caveats"]
    assert "regret" in render(payload)
    assert payload["weight_path"]["a"] and payload["within_bound"] is True


# ── wiring the runner into the allocator ──────────────────────────────────
def rows(name, values):
    return [{"identity": f"{name}-{i}", "executed_r": v} for i, v in enumerate(values)]


def test_alignment_uses_the_intersection_of_executed_trades():
    from scorpion.experts import aligned_returns

    trades = {
        "a": [{"identity": "t1", "executed_r": 1.0}, {"identity": "t2", "executed_r": 0.5}],
        "b": [{"identity": "t2", "executed_r": 0.5}, {"identity": "t3", "executed_r": 2.0}],
    }
    returns, identities = aligned_returns(trades)
    assert identities == ["t2"]
    assert returns["a"] == returns["b"] == [0.5]


def test_a_refusal_is_dropped_rather_than_scored_as_zero():
    from scorpion.experts import aligned_returns

    trades = {
        "a": [{"identity": "t1", "executed_r": 1.0}, {"identity": "t2", "executed_r": 1.0}],
        "b": [{"identity": "t1", "executed_r": 1.0},
              {"identity": "t2", "executed_r": None}],
    }
    returns, identities = aligned_returns(trades)
    assert identities == ["t1"] and len(returns["b"]) == 1


def test_thin_overlap_is_refused():
    from scorpion.experts import aligned_returns

    with pytest.raises(ExpertsError):
        aligned_returns({"a": [{"identity": "t1", "executed_r": 1.0}], "b": []})


def test_blending_variant_trades_reports_coverage():
    from scorpion.experts import from_variant_trades

    payload = from_variant_trades({
        "no_markup": rows("x", [1.0] * 30),
        "wider_gate": rows("x", [-1.0] * 30),
    })
    assert payload["identity_count"] == 30 and payload["variant_count"] == 2
    assert payload["best_expert"] == "no_markup"
    assert payload["coverage_note"]


def test_the_allocation_sums_to_one_and_drift_is_measured():
    from scorpion.experts import allocation_drift, from_variant_trades, weights_file

    first = from_variant_trades({"a": rows("x", [1.0] * 20), "b": rows("x", [1.0] * 20)})
    last = from_variant_trades({"a": rows("x", [1.0] * 20), "b": rows("x", [-2.0] * 20)})
    assert sum(weights_file(last).values()) == pytest.approx(1.0)
    drift = allocation_drift(first, last)
    assert drift["total_variation"] > 0 and drift["largest_move"] in ("a", "b")
