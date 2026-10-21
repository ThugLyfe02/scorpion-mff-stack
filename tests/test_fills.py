"""Partial fills: three-then-two must equal five, and nothing may be clamped."""

from decimal import Decimal

import pytest

from scorpion.fills import (
    Fill,
    FillError,
    Position,
    apply,
    apply_many,
    equivalence_check,
    fill_report,
    open_position,
)

D = Decimal


def test_a_fill_composes_into_a_position():
    pos = open_position("QQQ|CALL|719|2026-09-01", 1, intent_quantity=5)
    result = apply(pos, Fill("QQQ|CALL|719|2026-09-01", 1, 3, D("1.02"), intent_quantity=5))
    assert result.position.quantity == 3
    assert result.remaining == 2
    assert not result.complete


def test_three_then_two_realises_what_five_would():
    report = equivalence_check(5, [3, 2], D("1.00"), D("1.50"))
    assert report["identical"] is True, report
    assert report["delta"] == "0.00"


def test_a_partial_does_not_close_the_intent():
    pos = open_position("X", 1, intent_quantity=5)
    pos = apply(pos, Fill("X", 1, 2, D("1.00"), intent_quantity=5)).position
    assert pos.outstanding == 3
    assert pos.flat is False


def test_an_overfill_is_refused_not_clamped():
    pos = open_position("X", 1, intent_quantity=5)
    with pytest.raises(FillError) as exc:
        apply(pos, Fill("X", 1, 6, D("1.00"), intent_quantity=5))
    assert exc.value.code == "overfill"


def test_an_oversell_is_refused():
    pos = Position(contract_key="X", generation=1, quantity=2, cost=D("200"))
    with pytest.raises(FillError) as exc:
        apply(pos, Fill("X", 1, -3, D("2.00")))
    assert exc.value.code == "oversell"


def test_a_stale_generation_is_refused():
    pos = open_position("X", 2, intent_quantity=1)
    with pytest.raises(FillError) as exc:
        apply(pos, Fill("X", 1, 1, D("1.00")))
    assert exc.value.code == "stale_generation"


def test_a_fill_on_a_closed_position_is_refused():
    pos = Position(contract_key="X", generation=1, quantity=0, closed=True)
    with pytest.raises(FillError):
        apply(pos, Fill("X", 1, 1, D("1.00")))


def test_zero_quantity_without_final_is_refused():
    with pytest.raises(FillError):
        Fill("X", 1, 0, D("1.00"))


def test_realised_uses_average_cost_not_last_price():
    """Two buys at different prices must average before the sell realises anything."""
    pos = open_position("X", 1, intent_quantity=2)
    pos = apply(pos, Fill("X", 1, 1, D("1.00"), intent_quantity=2)).position
    pos = apply(pos, Fill("X", 1, 1, D("2.00"), intent_quantity=2)).position
    assert pos.average_price == D("1.5000")
    outcome = apply(pos, Fill("X", 1, -2, D("2.00"), final=True))
    assert outcome.realised == D("100.00")      # (2.00 - 1.50) * 2 * 100


def test_a_closed_position_is_flat_and_complete():
    pos = open_position("X", 1, intent_quantity=1)
    pos = apply(pos, Fill("X", 1, 1, D("1.00"), intent_quantity=1)).position
    outcome = apply(pos, Fill("X", 1, -1, D("1.50"), final=True))
    assert outcome.complete and outcome.position.flat


def test_many_fills_accumulate_realised():
    pos = open_position("X", 1, intent_quantity=2)
    fills = [Fill("X", 1, 1, D("1.00"), intent_quantity=2),
             Fill("X", 1, 1, D("1.00"), intent_quantity=2),
             Fill("X", 1, -2, D("1.50"), final=True)]
    final, total, results = apply_many(pos, fills)
    assert len(results) == 3 and total == D("100.00") and final.flat


def test_fill_report_counts_partials():
    fills = [Fill("X", 1, 3, D("1.00"), intent_quantity=5),
             Fill("X", 1, 2, D("1.10"), intent_quantity=5)]
    report = fill_report(fills)
    assert report["filled"] == 5 and report["intended"] == 5
    assert report["partial_count"] == 2
