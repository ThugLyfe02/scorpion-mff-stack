"""Parser tests. Every case is taken from a real post or the locked rules."""

from __future__ import annotations

import pytest

from scorpion_mff.parser import CHANNEL_IDS, EXCLUDED_CHANNEL_IDS, channel_name, parse
from scorpion_mff.types import AlertKind, IgnoreReason, RawMessage, Side


def msg(text: str, channel: str = "etf-options", channel_id: str = "") -> RawMessage:
    return RawMessage(channel=channel, channel_id=channel_id or CHANNEL_IDS.get(channel, ""),
                      text=text, ts=1000.0)


# ── entries ──────────────────────────────────────────────────────────────────
def test_canonical_entry(): 
    s = parse(msg("CALL TICKER NVDA STRIKE $180 Exp Dec 5 Current BID Price $3.25"))
    assert s.kind is AlertKind.ENTRY
    assert s.side is Side.CALL
    assert s.ticker == "NVDA"
    assert s.strike == 180
    # normalise() uppercases the post, so the expiry is canonical-uppercase too:
    # the contract key must be byte-stable across posts, caps or not.
    assert s.expiry == "DEC 5"
    assert s.his_bid == 3.25
    assert s.contract_key == "NVDA|CALL|180|DEC 5"


def test_put_with_platinum_tag_and_no_dollar_signs():
    s = parse(msg("PUT TICKER AMD STRIKE 240 Exp Nov Current BID Price 1.80 @PLATINUM",
                  channel="platinum-options"))
    assert s.kind is AlertKind.ENTRY
    assert s.side is Side.PUT
    assert s.strike == 240.0
    assert s.his_bid == 1.80
    assert s.tags == ("PLATINUM",)


def test_zero_dte_variants_are_the_same_day():
    for text in ("CALL TICKER TSLA STRIKE $265 Exp Today Current BID Price $1.90",
                 "CALL/ ODTE LOTTO TICKER TSLA STRIKE $265 Exp Today Current BID Price $1.90",
                 "PUT/ ODTE TICKER QQQ STRIKE $513 Exp Dec 3 Current BID Price $.35"):
        s = parse(msg(text, channel="options-king"))
        assert s.expiry in {"0DTE", "DEC 3"}


def test_ocr_isms_that_a_human_reads_correctly():
    # "PUTI IDTE" is OCR of "PUT/ ODTE"; the parser must not lose the side.
    s = parse(msg("PUTI IDTE TICKER QQQ STRIKE $513 Exp Dec 3 Current BID Price $.35",
                  channel="options-king"))
    assert s.side is Side.PUT
    assert s.kind is AlertKind.ENTRY


def test_fractional_strike_and_expiry_year():
    s = parse(msg("PUT TICKER NVDA STRIKE $227.5 Exp 2026-09-11 Current BID Price $2.63",
                  channel="options-king"))
    assert s.strike == 227.5
    assert s.expiry == "2026-09-11"
    assert s.contract_key == "NVDA|PUT|227.5|2026-09-11"


def test_ticker_word_that_is_not_a_ticker_is_refused():
    s = parse(msg("CALL TICKER STRIKE $100 Exp Dec 5 Current BID Price $1.00"))
    assert s.ticker == ""
    # no ticker means no contract key, and the engine refuses to trade it
    assert s.kind is AlertKind.ENTRY
    assert s.contract_key == ""


def test_entry_without_strike_is_not_tradeable():
    s = parse(msg("CALL TICKER PLTR Ctdive tonn", channel="options-king"))
    assert s.kind is AlertKind.IGNORE
    assert s.ignore_reason is IgnoreReason.NO_SIGNAL


# ── updates ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,kind,field,value", [
    ("Close at 2.65", AlertKind.CLOSE, "price", 2.65),
    ("Close remaining for 10%", AlertKind.CLOSE, "pct", 10.0),
    ("Close remaining contracts for 14%", AlertKind.CLOSE, "pct", 14.0),
    ("Runners at $3.00 closed fully", AlertKind.CLOSE, "price", 3.0),
    ("Runners @ 25% close or keep as lotto", AlertKind.TRIM, "pct", 25.0),
    ("Adding at 3.00", AlertKind.ADD, "price", 3.0),
    ("New avg 1.39", AlertKind.ADD, "price", 1.39),
    ("Set SL @ breakeven price", AlertKind.TRIM, "pct", None),
])
def test_update_classification(text, kind, field, value):
    s = parse(msg(text, channel="options-king"))
    assert s.kind is kind, f"{text!r} -> {s.kind}"
    assert getattr(s, field) == value


def test_stand_down_is_a_stop_not_an_exit():
    s = parse(msg("STAND DOWN - do not buy anything", channel="options-king"))
    assert s.kind is AlertKind.STOP


def test_chatter_is_ignored():
    s = parse(msg("Printing BANGER", channel="options-king"))
    assert s.kind is AlertKind.IGNORE
    assert s.ignore_reason is IgnoreReason.NO_SIGNAL


# ── policy ───────────────────────────────────────────────────────────────────
def test_excluded_channel_is_refused_even_with_a_perfect_entry():
    s = parse(msg("CALL TICKER TSLA STRIKE $265 Exp Today Current BID Price $1.90",
                  channel="lotto-options", channel_id=EXCLUDED_CHANNEL_IDS["lotto-options"]))
    assert s.kind is AlertKind.IGNORE
    assert s.ignore_reason is IgnoreReason.EXCLUDED_CHANNEL


def test_excluded_ticker_is_refused():
    s = parse(msg("CALL TICKER NKE STRIKE $70 Exp Dec 5 Current BID Price $1.10"))
    assert s.ignore_reason is IgnoreReason.TICKER_EXCLUDED


def test_lotto_flag_is_refused_by_default():
    s = parse(msg("CALL/ ODTE LOTTO TICKER TSLA STRIKE $265 Exp Today Current BID Price $1.90",
                  channel="options-king"))
    assert s.ignore_reason is IgnoreReason.LOTTO_FLAG


def test_channel_id_resolves_to_a_name():
    s = parse(msg("PUT TICKER AMD STRIKE 240 Exp Nov Current BID Price 1.80",
                  channel="", channel_id=CHANNEL_IDS["platinum-options"]))
    assert s.channel == "platinum-options"
    assert channel_name(CHANNEL_IDS["etf-options"]) == "etf-options"


def test_parse_never_raises_on_garbage():
    for text in ("", "   ", "!!!", "\x00", "TICKER", "STRIKE", "@everyone"):
        assert parse(msg(text)) is not None


# ── strike vs price (harvested from feat/risk-budget-context) ────────────────
def test_a_strike_dollar_amount_is_not_an_exit_price():
    s = parse(msg("NVDA STRIKE $180 CALLS CLOSED"))
    assert s.kind is AlertKind.CLOSE
    assert s.strike == 180
    assert s.price is None


def test_a_dollar_strike_without_the_word_strike_is_not_an_exit_price():
    s = parse(msg("NVDA $180 CALLS CLOSED"))
    assert s.price is None


def test_an_anchored_exit_price_still_wins_over_the_strike():
    s = parse(msg("NVDA STRIKE $180 CALLS CLOSED AT 2.65"))
    assert s.strike == 180
    assert s.price == 2.65


def test_an_entry_strike_does_not_become_the_posted_price():
    s = parse(msg("CALL TICKER NVDA STRIKE $180 Exp Today Current BID Price $1.00"))
    assert s.strike == 180
    assert s.his_bid == 1.0
    assert s.price == 1.0
