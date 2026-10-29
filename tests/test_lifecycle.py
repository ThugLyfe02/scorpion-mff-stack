"""Lifecycles: no fraction vanishes, nothing sells twice, and an unreadable claim says so."""


import pytest

from scorpion.lifecycle import (
    D,
    LifecycleError,
    Plan,
    Step,
    describe,
    from_claim,
    intents,
    single_exit,
    summary,
    validate,
)


def test_a_pipe_test_is_one_contract_one_exit():
    plan = single_exit(1)
    assert [i.contracts for i in intents(plan)] == [1]
    assert validate(plan) == []


def test_a_scale_out_splits_and_keeps_the_remainder():
    plan = from_claim(5, "sold half at 25%, runners to 75%")
    assert [i.contracts for i in intents(plan)] == [2, 3]
    assert [str(i.at_multiple) for i in intents(plan)] == ["1.25", "1.75"]
    assert validate(plan) == []


def test_the_sum_always_equals_the_quantity():
    for quantity in range(1, 40):
        plan = from_claim(quantity, "trimmed at 25%, rest at 75%")
        assert sum(i.contracts for i in intents(plan)) == quantity


def test_a_fraction_too_small_to_sell_is_absorbed_not_dropped():
    plan = Plan(quantity=3, steps=(Step(D("0.05"), D("1.1")), Step(D("0.95"), D("2"))))
    resolved = intents(plan)
    assert sum(i.contracts for i in resolved) == 3
    assert all(i.contracts > 0 for i in resolved)


def test_nothing_can_be_sold_twice():
    plan = from_claim(9, "sold half at 20%, runners to 100%")
    cumulative = [i.cumulative for i in intents(plan)]
    assert cumulative == sorted(cumulative)
    assert cumulative[-1] == 9


def test_triggers_must_be_monotone():
    plan = Plan(quantity=4, steps=(Step(D("0.5"), D("2.0")), Step(D("0.5"), D("1.2"))))
    assert any("monotone" in p for p in validate(plan))


def test_an_unreadable_claim_is_marked_rather_than_guessed():
    plan = from_claim(1, "", exit_pct=None)
    assert plan.unparsed is True
    assert intents(plan)


def test_a_single_claim_becomes_one_step_at_its_multiple():
    plan = from_claim(1, "Added at .57/.65; +75%", exit_pct=75)
    assert len(plan.steps) == 1
    assert plan.steps[0].at_multiple == D("1.75")


def test_zero_quantity_is_refused():
    with pytest.raises(LifecycleError):
        intents(Plan(quantity=0, steps=(Step(D(1)),)))


def test_describe_is_human_readable():
    assert "2x + 3x" in describe(from_claim(5, "sold half at 25%, runners to 75%"))


def test_summary_counts_unparsed_and_multi_step():
    payload = summary([from_claim(1, ""), from_claim(4, "sold half at 25%, runners to 75%")])
    assert payload["plans"] == 2 and payload["multi_step"] == 1
    assert payload["unparsed"] == 1
