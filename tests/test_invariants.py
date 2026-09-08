from decimal import Decimal

import pytest

from scorpion.domain import BookState, PositionState, PositionStatus
from scorpion.invariants import assert_valid_book, validate_book_state


def test_invalid_closed_quantity_is_detected():
    position = PositionState(
        contract_key="QQQ|CALL|719|2026-09-08",
        status=PositionStatus.CLOSED,
        generation=1,
        quantity=1,
        average_price=Decimal("1.00"),
    )
    state = BookState(positions={position.contract_key: position})
    violations = validate_book_state(state)
    assert any(item.code == "closed_with_quantity" for item in violations)
    with pytest.raises(RuntimeError, match="book invariant violation"):
        assert_valid_book(state)
