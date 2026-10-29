"""Expiry: three verdicts, and the two silences that must never become facts.

The money in this module is in the boundaries. 15:45 ET on a 0DTE is a flatten; 15:44 is
not. 16:01 is a page. The day after Thanksgiving closes at 13:00 ET, so 12:45 is a flatten
and 13:00 is a page - with a 16:00 assumption the same position sits unwatched for three
hours and comes back as 100 shares. And at 01:30 UTC the contract dated 2025-09-09 is still
Monday in New York: comparing to the UTC date closes it a day early.

No bid and no underlying price are *silences*. None of these tests lets a silence become
"worthless" or "out of the money".
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from scorpion_mff.calendar import CalendarError
from scorpion_mff.expiry import (
    ExpiryAction,
    assignment_shares,
    contract_terms,
    expiry_day,
    expiry_verdict,
    is_live_bid,
    page_text,
    resolve_expiry,
    sweep,
    verdict_detail,
)
from scorpion_mff.types import Position, Side

QQQ_0DTE = "QQQ|CALL|719|0DTE"


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# 2025-09-08 is a Monday in EDT: open 13:30 UTC, close 20:00 UTC, cutoff 15:45 ET = 19:45.
def monday(hour: int, minute: int = 0) -> datetime:
    return utc(2025, 9, 8, hour, minute)


# 2025-11-28 is the Friday after Thanksgiving, a half day in EST: close 13:00 ET = 18:00.
def half_day(hour: int, minute: int = 0) -> datetime:
    return utc(2025, 11, 28, hour, minute)


# ── the shape of the action ──────────────────────────────────────────────────
def test_the_actions_are_the_three_the_page_needs():
    assert ExpiryAction.HOLD == "hold"
    assert ExpiryAction.FORCE_CLOSE == "force_close"
    assert ExpiryAction.HALT_AND_PAGE == "halt_and_page"
    assert len(ExpiryAction) == 3


# ── 0DTE: today's session, and the 15:45 boundary ────────────────────────────
@pytest.mark.parametrize("moment", [monday(14, 0), utc(2025, 9, 8, 19, 44)])
def test_outside_the_cutoff_it_holds(moment):
    assert expiry_verdict(moment, QQQ_0DTE, 1, 1.10, 725.0) is ExpiryAction.HOLD


def test_a_0844_et_quote_is_not_inside_a_cutoff_that_has_not_started():
    verdict = verdict_detail(monday(19, 44), QQQ_0DTE, 1, 1.10, 725.0)
    assert verdict.reason == "before_cutoff"
    assert verdict.minutes_to_close == 16.0


def test_1545_et_is_inside_the_cutoff_and_flattens_at_the_bid():
    verdict = verdict_detail(monday(19, 45), QQQ_0DTE, 2, 1.10, 725.0)
    assert verdict.action is ExpiryAction.FORCE_CLOSE
    assert verdict.reason == "cutoff_with_live_bid"
    assert verdict.minutes_to_close == 15.0
    assert verdict.must_flatten and not verdict.must_page


def test_1559_et_still_flattens():
    assert expiry_verdict(monday(19, 59), QQQ_0DTE, 1, 0.05, None) is ExpiryAction.FORCE_CLOSE


def test_exactly_at_the_close_pages():
    verdict = verdict_detail(monday(20, 0), QQQ_0DTE, 1, 1.10, 725.0)
    assert verdict.action is ExpiryAction.HALT_AND_PAGE
    assert verdict.reason == "at_or_after_close"


def test_1601_et_pages_even_with_a_live_bid():
    verdict = verdict_detail(monday(20, 1), QQQ_0DTE, 1, 1.10, 725.0)
    assert verdict.action is ExpiryAction.HALT_AND_PAGE
    assert verdict.minutes_to_close == -1.0


def test_before_the_open_on_expiry_day_it_holds():
    verdict = verdict_detail(utc(2025, 9, 8, 12, 0), QQQ_0DTE, 1, 1.10, 725.0)
    assert verdict.action is ExpiryAction.HOLD
    assert verdict.reason == "pre_open"


def test_no_bid_inside_the_cutoff_pages_rather_than_guessing():
    verdict = verdict_detail(monday(19, 45), QQQ_0DTE, 1, None, 725.0)
    assert verdict.action is ExpiryAction.HALT_AND_PAGE
    assert verdict.reason == "cutoff_without_live_bid"


@pytest.mark.parametrize("bid", [0.0, -0.5, float("nan"), float("inf")])
def test_a_bid_that_is_not_a_market_pages(bid):
    assert is_live_bid(bid) is False
    assert expiry_verdict(monday(19, 45), QQQ_0DTE, 1, bid, None) is (
        ExpiryAction.HALT_AND_PAGE
    )


def test_a_live_bid_is_present_finite_and_positive():
    assert is_live_bid(0.01) and is_live_bid(3.5)
    assert not is_live_bid(None)


def test_no_open_position_holds_whatever_the_clock_says():
    verdict = verdict_detail(monday(20, 1), QQQ_0DTE, 0, None, None)
    assert verdict.action is ExpiryAction.HOLD
    assert verdict.reason == "no_open_position"


@pytest.mark.parametrize("token", ["0DTE", "0dte", "ODTE", "TODAY", "0-DTE"])
def test_every_same_day_spelling_is_today(token):
    key = f"QQQ|CALL|719|{token}"
    assert expiry_verdict(monday(19, 45), key, 1, 1.10, 725.0) is ExpiryAction.FORCE_CLOSE


# ── the half day the 16:00 assumption misses ─────────────────────────────────
def test_a_half_day_cutoff_is_1245_et_not_1545():
    assert expiry_verdict(half_day(17, 44), QQQ_0DTE, 1, 1.10, 725.0) is ExpiryAction.HOLD
    assert expiry_verdict(half_day(17, 45), QQQ_0DTE, 1, 1.10, 725.0) is (
        ExpiryAction.FORCE_CLOSE
    )


def test_a_half_day_close_at_1300_et_pages():
    verdict = verdict_detail(half_day(18, 0), QQQ_0DTE, 1, 1.10, 725.0)
    assert verdict.action is ExpiryAction.HALT_AND_PAGE
    assert verdict.close_utc == utc(2025, 11, 28, 18)


def test_midday_on_a_half_day_is_still_three_hours_from_a_1600_close():
    assert expiry_verdict(half_day(17, 0), QQQ_0DTE, 1, 1.10, None) is ExpiryAction.HOLD


def test_a_cutoff_wider_than_the_remaining_session_starts_at_the_open():
    # 400 minutes on a 210-minute half day covers the whole session: at the open, a live
    # bid means flatten, because there is no later moment to do it.
    verdict = verdict_detail(half_day(14, 30), QQQ_0DTE, 1, 1.10, None, cutoff_minutes=400)
    assert verdict.action is ExpiryAction.FORCE_CLOSE


def test_july_third_closes_at_1700_utc():
    assert expiry_verdict(utc(2025, 7, 3, 16, 59), QQQ_0DTE, 1, 1.10, None) is (
        ExpiryAction.FORCE_CLOSE
    )
    assert expiry_verdict(utc(2025, 7, 3, 17, 0), QQQ_0DTE, 1, 1.10, None) is (
        ExpiryAction.HALT_AND_PAGE
    )


# ── an ISO expiry is a date, and the date is Eastern ─────────────────────────
def test_a_later_expiry_holds_even_after_the_bell():
    verdict = verdict_detail(monday(20, 30), "SPY|CALL|500|2025-09-09", 1, 2.0, 510.0)
    assert verdict.action is ExpiryAction.HOLD
    assert verdict.reason == "not_expiring"
    assert verdict.expiry == date(2025, 9, 9)


def test_0130_utc_is_still_monday_evening_in_new_york():
    moment = utc(2025, 9, 9, 1, 30)                              # 21:30 ET on 2025-09-08
    tomorrow = verdict_detail(moment, "SPY|CALL|500|2025-09-09", 1, 2.0, 510.0)
    assert tomorrow.action is ExpiryAction.HOLD               # a UTC date compare would close
    today = verdict_detail(moment, "SPY|CALL|500|2025-09-08", 1, 2.0, 510.0)
    assert today.action is ExpiryAction.HALT_AND_PAGE         # that bell has rung
    assert today.reason == "at_or_after_close"


def test_an_iso_expiry_inside_the_cutoff_behaves_like_zero_dte():
    verdict = verdict_detail(monday(19, 45), "SPY|CALL|500|2025-09-08", 1, 2.0, 510.0)
    assert verdict.action is ExpiryAction.FORCE_CLOSE


def test_a_past_expiry_on_a_live_position_pages_for_reconciliation():
    verdict = verdict_detail(utc(2025, 9, 10, 15, 0), "QQQ|CALL|719|2025-09-08", 1, None, None)
    assert verdict.action is ExpiryAction.HALT_AND_PAGE
    assert verdict.reason == "expired_unreconciled"
    assert verdict.expiry == date(2025, 9, 8)


@pytest.mark.parametrize("token", ["Dec 5", "", "2025-13-01", "Friday", "?"])
def test_an_unreadable_expiry_pages_instead_of_assuming_a_horizon(token):
    verdict = verdict_detail(monday(14, 0), f"QQQ|CALL|719|{token}", 1, 1.0, 725.0)
    assert verdict.action is ExpiryAction.HALT_AND_PAGE
    assert verdict.reason == "expiry_unparsed"
    assert verdict.expiry is None


@pytest.mark.parametrize("key", ["QQQ", "QQQ|CALL|719", "QQQ|CALL|719|0DTE|extra", "|CALL|1|0DTE"])
def test_an_unreadable_key_pages(key):
    verdict = verdict_detail(monday(14, 0), key, 1, 1.0, 725.0)
    assert verdict.action is ExpiryAction.HALT_AND_PAGE
    assert verdict.reason in {"contract_key_unparsed", "expiry_unparsed"}


@pytest.mark.parametrize("moment", [utc(2025, 9, 7, 15, 0), utc(2025, 11, 27, 19, 0)])
def test_zero_dte_on_a_closed_day_pages(moment):
    verdict = verdict_detail(moment, QQQ_0DTE, 1, 1.0, 725.0)
    assert verdict.action is ExpiryAction.HALT_AND_PAGE
    assert verdict.reason == "expiry_without_session"


def test_an_unknown_year_fails_closed_instead_of_assuming_a_close():
    with pytest.raises(CalendarError):
        expiry_verdict(utc(2027, 9, 8, 15, 0), QQQ_0DTE, 1, 1.0, 725.0)


def test_a_naive_now_fails_closed():
    with pytest.raises(CalendarError):
        expiry_verdict(datetime(2025, 9, 8, 19, 45), QQQ_0DTE, 1, 1.0, 725.0)


def test_an_epoch_clock_means_the_same_thing_as_the_datetime():
    moment = monday(19, 45)
    from_float = verdict_detail(moment.timestamp(), QQQ_0DTE, 1, 1.10, 725.0)
    from_datetime = verdict_detail(moment, QQQ_0DTE, 1, 1.10, 725.0)
    assert from_float.to_dict() == from_datetime.to_dict()
    assert from_float.action is ExpiryAction.FORCE_CLOSE


@pytest.mark.parametrize("clock", [float("nan"), float("inf")])
def test_a_non_finite_clock_is_refused(clock):
    with pytest.raises(ValueError):
        expiry_verdict(clock, QQQ_0DTE, 1, 1.0, 725.0)


def test_a_negative_cutoff_is_a_bug_not_a_policy():
    with pytest.raises(ValueError):
        expiry_verdict(monday(19, 45), QQQ_0DTE, 1, 1.0, 725.0, cutoff_minutes=-1)


def test_a_zero_cutoff_is_the_bell_itself():
    assert expiry_verdict(monday(19, 59), QQQ_0DTE, 1, 1.0, 725.0, cutoff_minutes=0) is (
        ExpiryAction.HOLD
    )
    assert expiry_verdict(monday(20, 0), QQQ_0DTE, 1, 1.0, 725.0, cutoff_minutes=0) is (
        ExpiryAction.HALT_AND_PAGE
    )


# ── parsing ──────────────────────────────────────────────────────────────────
def test_contract_terms_reads_the_key():
    terms = contract_terms("qqq|call|719.5|0DTE")
    assert terms is not None
    assert (terms.ticker, terms.side, terms.strike, terms.expiry) == (
        "QQQ", "CALL", 719.5, "0DTE"
    )
    assert terms.is_call and terms.same_day
    assert contract_terms("QQQ|CALL|719|0DTE") is not None
    assert contract_terms("QQQ|SPREAD|719|0DTE") is None


def test_expiry_day_separates_a_date_from_a_relative_token():
    assert expiry_day("QQQ|CALL|719|2025-09-08") == date(2025, 9, 8)
    assert expiry_day(QQQ_0DTE) is None
    assert expiry_day("QQQ|CALL|719|Dec 5") is None
    assert resolve_expiry(QQQ_0DTE, date(2025, 9, 8)) == date(2025, 9, 8)
    assert resolve_expiry("QQQ|CALL|719|2025-09-12", date(2025, 9, 8)) == date(2025, 9, 12)


# ── assignment: the overnight risk the verdict is protecting ─────────────────
@pytest.mark.parametrize(
    ("key", "contracts", "price", "expected"),
    [
        ("QQQ|CALL|719|0DTE", 1, 725.0, 100),        # ITM call: stock comes to you
        ("QQQ|CALL|719|0DTE", 3, 719.01, 300),       # scaled by contract
        ("QQQ|CALL|719|0DTE", 1, 719.0, 0),          # exactly at the strike: a pin, not ITM
        ("QQQ|CALL|719|0DTE", 1, 715.0, 0),          # OTM call: worthless
        ("SPY|PUT|500|2025-09-19", 1, 490.0, -100),  # ITM put: stock leaves you
        ("SPY|PUT|500|2025-09-19", 2, 499.99, -200),
        ("SPY|PUT|500|2025-09-19", 1, 510.0, 0),     # OTM put
        ("SPY|PUT|500|2025-09-19", 1, 500.0, 0),
        (QQQ_0DTE, 0, 725.0, 0),                     # nothing held
    ],
)
def test_assignment_shares_signs_the_delivery(key, contracts, price, expected):
    assert assignment_shares(key, contracts, price) == expected


@pytest.mark.parametrize(
    ("key", "price"),
    [
        ("QQQ|CALL|?|0DTE", 725.0),                  # strike unreadable
        (QQQ_0DTE, None),                            # underlying unreadable
        (QQQ_0DTE, float("nan")),
        ("QQQ", 725.0),                              # key unreadable
    ],
)
def test_assignment_shares_is_none_when_it_cannot_be_computed(key, price):
    assert assignment_shares(key, 1, price) is None


def test_silence_is_never_out_of_the_money():
    verdict = verdict_detail(monday(20, 1), QQQ_0DTE, 1, None, None)
    assert verdict.settlement == "unknown"
    assert verdict.assignment_shares is None
    assert "not OTM" in page_text(verdict)


def test_a_known_underlying_states_the_assignment_on_the_page():
    verdict = verdict_detail(monday(20, 1), QQQ_0DTE, 2, None, 725.0)
    assert verdict.action is ExpiryAction.HALT_AND_PAGE
    assert verdict.settlement == "itm"
    assert verdict.assignment_shares == 200
    text = page_text(verdict)
    assert "HALT_AND_PAGE" in text
    assert "+200 shares" in text
    assert "expiry 2025-09-08" in text


def test_a_known_otm_underlying_is_named_but_still_pages_at_the_close():
    verdict = verdict_detail(monday(20, 1), QQQ_0DTE, 1, None, 715.0)
    assert verdict.action is ExpiryAction.HALT_AND_PAGE
    assert verdict.settlement == "out_of_the_money"
    assert verdict.assignment_shares == 0


def test_a_pin_at_the_strike_is_named_and_still_pages():
    # Exactly at the strike is 0 intrinsic, but the assignment decision happens after the
    # bell: calling that "worthless" is the silence-as-a-fact this module refuses.
    verdict = verdict_detail(monday(20, 1), QQQ_0DTE, 1, None, 719.0)
    assert verdict.action is ExpiryAction.HALT_AND_PAGE
    assert verdict.settlement == "at_the_money"
    assert verdict.assignment_shares == 0


# ── the evidence the caller keeps ────────────────────────────────────────────
def test_expiry_verdict_and_verdict_detail_cannot_disagree():
    detail = verdict_detail(monday(19, 45), QQQ_0DTE, 1, 1.1, 725.0)
    assert expiry_verdict(monday(19, 45), QQQ_0DTE, 1, 1.1, 725.0) is detail.action


def test_the_dict_is_serialisable_and_says_what_it_saw():
    verdict = verdict_detail(monday(19, 45), QQQ_0DTE, 1, 1.1, None)
    payload = verdict.to_dict()
    assert payload["action"] == "force_close"
    assert payload["reason"] == "cutoff_with_live_bid"
    assert payload["expiry"] == "2025-09-08"
    assert payload["close_utc"] == "2025-09-08T20:00:00+00:00"
    assert payload["minutes_to_close"] == 15.0
    assert payload["settlement"] == "unknown"


# ── the book ─────────────────────────────────────────────────────────────────
def pos(key: str, contracts: int = 1, ticker: str = "QQQ") -> Position:
    return Position(contract_key=key, ticker=ticker, side=Side.CALL, contracts=contracts,
                    avg_price=1.0, opened_ts=0.0)


def test_sweep_reports_pages_first_and_omits_positions_that_are_not_expiring():
    book = [
        pos(QQQ_0DTE, 1),
        pos("IWM|CALL|200|2025-09-05", 2, "IWM"),
        pos("SPY|PUT|500|2025-09-09", 1, "SPY"),
        pos("QQQ|CALL|720|0DTE", 0),
    ]
    verdicts = sweep(
        monday(19, 45),
        book,
        bids={QQQ_0DTE: 1.10},
        underlying={"QQQ": 725.0, "IWM": 205.0},
    )
    assert [v.contract_key for v in verdicts] == ["IWM|CALL|200|2025-09-05", QQQ_0DTE]
    assert verdicts[0].action is ExpiryAction.HALT_AND_PAGE
    assert verdicts[0].reason == "expired_unreconciled"
    assert verdicts[1].action is ExpiryAction.FORCE_CLOSE


def test_sweep_never_invents_a_bid_or_a_price():
    verdicts = sweep(monday(19, 45), [pos(QQQ_0DTE)], bids={}, underlying={})
    assert len(verdicts) == 1
    assert verdicts[0].action is ExpiryAction.HALT_AND_PAGE
    assert verdicts[0].settlement == "unknown"
