from datetime import UTC, datetime
from decimal import Decimal

from research.corrected_tape import Lot, position_is_open_at
from scorpion.replay import Bar, same_bar_target_resolution


def test_entry_bar_target_is_not_conservatively_credited():
    bar = Bar(
        begins_at=datetime(2026, 9, 8, 13, 30, tzinfo=UTC),
        open=Decimal("1.00"),
        high=Decimal("1.40"),
        low=Decimal("0.80"),
        close=Decimal("1.10"),
    )
    result = same_bar_target_resolution(
        entry_price=Decimal("1.00"),
        target_price=Decimal("1.20"),
        bar=bar,
        entry_occurs_within_bar=True,
    )
    assert result.optimistic == Decimal("1.20")
    assert result.conservative is None


def test_partial_sale_then_add_cost_basis():
    lot = Lot(10, Decimal("1.00")).sell(5).buy(5, Decimal("0.80"))
    assert lot.quantity == 10
    assert lot.average == Decimal("0.90")


def test_concurrency_uses_actual_candidate_timestamp():
    exit_ts = datetime(2026, 9, 8, 14, 2, tzinfo=UTC)
    candidate = datetime(2026, 9, 8, 15, 17, tzinfo=UTC)
    assert not position_is_open_at(exit_ts, candidate)
