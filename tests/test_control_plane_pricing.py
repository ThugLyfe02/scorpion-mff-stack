from decimal import Decimal

from scorpion.pricing import entry_is_stale, entry_limit


def test_entry_limit():
    assert entry_limit(Decimal("1.00"), Decimal("1.10")) == Decimal("1.10")
    assert entry_limit(Decimal("1.00"), Decimal("1.30")) == Decimal("1.15")


def test_stale_dislocation():
    assert entry_is_stale(Decimal("1.00"), Decimal("1.26")) is True
    assert entry_is_stale(Decimal("1.00"), Decimal("1.25")) is False
