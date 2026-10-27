"""Mark-to-market: the operator view the book was missing, with withholding.

`settlement` prices realised fills; `markout` prices history; `tca` prices execution
quality. `marks` prices the open book *now*, and - like every aggregate in this lane -
withholds the total when the inputs cannot support it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.broker import Quote
from scorpion.domain import BookState, PositionState, PositionStatus
from scorpion.marks import mark, split_stale, unrealized_usd


def held(key: str, quantity: int, average_price: str) -> PositionState:
    return PositionState(
        contract_key=key,
        status=PositionStatus.OPEN,
        quantity=quantity,
        average_price=Decimal(average_price),
    )


def book(**positions: PositionState) -> BookState:
    return BookState(positions=dict(positions))


# ── the dollar arithmetic ────────────────────────────────────────────────────
def test_unrealized_usd_is_contracts_times_hundred_shares_to_the_cent():
    assert unrealized_usd(2, Decimal("1.00"), Decimal("1.25")) == Decimal("50.00")
    assert unrealized_usd(1, Decimal("3.00"), Decimal("2.50")) == Decimal("-50.00")


def test_unrealized_usd_rounds_half_up_to_the_cent():
    # (1.005 - 1.000) * 1 * 100 = 0.500 -> rounds to 0.50
    assert unrealized_usd(1, Decimal("1.000"), Decimal("1.005")) == Decimal("0.50")


# ── marking the book ─────────────────────────────────────────────────────────
def test_mark_prices_only_held_positions():
    state = book(qqq=held("QQQ|CALL|719|2026-09-11", 2, "1.00"),
                 spy=held("SPY|PUT|500|2026-09-11", 0, "3.00"))
    report = mark(state, {"QQQ|CALL|719|2026-09-11": Decimal("1.25")})
    assert report.priced == 1
    assert report.unpriced == 0
    assert report.marks[0].contract_key == "QQQ|CALL|719|2026-09-11"
    assert report.marks[0].unrealized_usd == Decimal("50.00")
    assert not report.marks[0].at_or_below_cost


def test_mark_counts_but_never_invents_a_missing_bid():
    state = book(qqq=held("QQQ|CALL|719|2026-09-11", 2, "1.00"),
                 spy=held("SPY|PUT|500|2026-09-11", 1, "3.00"))
    report = mark(state, {"QQQ|CALL|719|2026-09-11": Decimal("1.25")})
    assert report.priced == 1
    assert report.unpriced == 1
    assert report.total_unrealized_usd is None


def test_total_is_withheld_when_nothing_is_priced():
    state = book(qqq=held("QQQ|CALL|719|2026-09-11", 2, "1.00"))
    assert mark(state, {}).total_unrealized_usd is None


def test_total_sums_only_when_every_held_position_is_priced():
    state = book(qqq=held("QQQ|CALL|719|2026-09-11", 2, "1.00"),
                 spy=held("SPY|PUT|500|2026-09-11", 1, "3.00"))
    bids = {"QQQ|CALL|719|2026-09-11": Decimal("1.25"),
            "SPY|PUT|500|2026-09-11": Decimal("2.50")}
    report = mark(state, bids)
    assert report.total_unrealized_usd == Decimal("0.00")   # +50.00 - 50.00


def test_a_bid_at_or_below_cost_is_flagged():
    state = book(qqq=held("QQQ|CALL|719|2026-09-11", 1, "1.00"))
    report = mark(state, {"QQQ|CALL|719|2026-09-11": Decimal("0.90")})
    assert report.marks[0].at_or_below_cost
    assert report.marks[0].unrealized_usd == Decimal("-10.00")


def test_to_dict_serialises_money_as_strings():
    state = book(qqq=held("QQQ|CALL|719|2026-09-11", 1, "1.00"))
    report = mark(state, {"QQQ|CALL|719|2026-09-11": Decimal("1.20")})
    payload = report.to_dict()
    assert payload["priced"] == 1
    assert payload["total_unrealized_usd"] == "20.00"
    assert payload["marks"][0]["average_price"] == "1.00"


# ── stale quotes: a phantom bid must not price a position ────────────────────
def _quote(bid: str, age_s: float) -> Quote:
    now = datetime(2026, 9, 8, 14, 0, 0, tzinfo=UTC)
    return Quote(
        bid=Decimal(bid),
        ask=Decimal(bid),
        observed_ts_utc=now - timedelta(seconds=age_s),
    )


def test_split_stale_partitions_by_age_and_counts_dropped():
    now = datetime(2026, 9, 8, 14, 0, 0, tzinfo=UTC)
    quotes = {"fresh": _quote("1.00", 10), "stale": _quote("2.00", 90)}
    fresh, stale = split_stale(quotes, now, timedelta(seconds=60))
    assert set(fresh) == {"fresh"}
    assert stale == 1


def test_split_stale_boundary_is_exclusive():
    now = datetime(2026, 9, 8, 14, 0, 0, tzinfo=UTC)
    quotes = {"edge": _quote("1.00", 60)}          # exactly max_age: still fresh
    fresh, stale = split_stale(quotes, now, timedelta(seconds=60))
    assert set(fresh) == {"edge"}
    assert stale == 0


def test_stale_quote_withholds_the_total_like_a_missing_bid():
    state = book(qqq=held("QQQ|CALL|719|2026-09-11", 1, "1.00"))
    report = mark(state, {}, stale=1)
    assert report.stale == 1
    assert report.total_unrealized_usd is None


def test_zero_stale_does_not_withhold():
    state = book(qqq=held("QQQ|CALL|719|2026-09-11", 1, "1.00"))
    report = mark(state, {"QQQ|CALL|719|2026-09-11": Decimal("1.20")})
    assert report.stale == 0
    assert report.total_unrealized_usd == Decimal("20.00")
