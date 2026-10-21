"""Exit attribution and expiry release - the two things the corpus exposed.

Attribution: an exit post that names its ticker must close THAT position, so two
positions in one channel are no longer unexitable. A post that names nothing stays
ambiguous and raises an alert - guessing which contract to flatten is worse.

Release: a position whose contract has expired must stop holding a concurrency slot,
otherwise one missing exit silently disables the hone for the rest of the month.
"""

from __future__ import annotations

import pytest

from scorpion_mff.broker import QuoteScript
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.parser import parse
from scorpion_mff.types import AlertKind, Quote, RawMessage, Side

KING = "options-king"
KING_ID = "968352649437126676"
ET = "etf-options"
ET_ID = "1231301953972207667"
DAY1 = 1_757_000_000.0
DAY2 = DAY1 + 86_400.0


def raw(text: str, channel: str = KING, channel_id: str = KING_ID, ts: float = DAY1) -> RawMessage:
    return RawMessage(channel=channel, channel_id=channel_id, text=text, ts=ts)


def engine(**cfg) -> Engine:
    return Engine(config=EngineConfig(**cfg), fills=FillLog(), quotes=_AlwaysQuote())


class _AlwaysQuote:
    """A market that always prices: these tests are about attribution, not pricing."""

    def quote(self, key: str):
        return Quote(1.0, 1.05)


# ── the parser recovers a bare symbol on exit-side posts ─────────────────────
def test_close_that_names_the_ticker_keeps_it():
    s = parse(raw("QQQ puts closed at 2.85"))
    assert s.kind is AlertKind.CLOSE
    assert s.ticker == "QQQ" and s.side is Side.PUT
    assert s.price == pytest.approx(2.85)


def test_plural_side_words_are_understood():
    assert parse(raw("NVDA calls closed at 4.10")).side is Side.CALL
    assert parse(raw("SPY put closed at 1.05")).side is Side.PUT


def test_close_that_names_nothing_stays_nameless():
    s = parse(raw("Close at 2.85"))
    assert s.kind is AlertKind.CLOSE and s.ticker == "" and s.price == pytest.approx(2.85)


def test_english_words_are_not_mistaken_for_symbols():
    for text in ("Sold runners at 3.90", "All out here", "Trimmed half, BE from here",
                 "Nice 200% move, thanks", "Closing the rest at 2.10"):
        assert parse(raw(text)).ticker == "", f"{text!r} produced a fake ticker"


def test_two_symbols_in_one_post_is_left_ambiguous():
    """'QQQ and SPY out' must not pick one: that is an alert, not a trade."""
    s = parse(raw("QQQ and SPY closed at 1.20"))
    assert s.ticker == ""


def test_entry_posts_are_untouched_by_the_bare_symbol_rule():
    s = parse(raw("CALL TICKER QQQ STRIKE $700 Exp 0DTE Current BID Price $1.00"))
    assert s.kind is AlertKind.ENTRY and s.ticker == "QQQ" and s.strike == 700


# ── the engine attributes an exit to the right position ──────────────────────
def _two_positions() -> Engine:
    eng = engine(pipe_test=False)
    eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $700 Exp 0DTE Current BID Price $1.00",
                            channel=ET, channel_id=ET_ID)), now=DAY1)
    eng.on_signal(parse(raw("CALL TICKER SPY STRIKE $600 Exp 0DTE Current BID Price $1.00",
                            channel=ET, channel_id=ET_ID)), now=DAY1 + 60)
    assert len(eng.open_positions) == 2
    return eng


def test_a_named_exit_closes_only_that_position():
    eng = _two_positions()
    sells = eng.on_signal(parse(raw("QQQ calls closed at 2.00", channel=ET, channel_id=ET_ID)),
                          now=DAY1 + 600)
    assert len(sells) == 1 and sells[0].action == "sell"
    assert sells[0].contract_key.startswith("QQQ|")
    assert [p.contract_key.split("|")[0] for p in eng.open_positions] == ["SPY"]


def test_an_unnamed_exit_with_two_open_still_alerts():
    eng = _two_positions()
    sells = eng.on_signal(parse(raw("Close at 2.00", channel=ET, channel_id=ET_ID)), now=DAY1 + 600)
    assert sells == []
    assert eng.fills.records[-1].event == "alert"
    assert eng.fills.records[-1].reason.startswith("ambiguous_exit")
    assert len(eng.open_positions) == 2, "nothing may be flattened on a guess"


def test_a_named_exit_for_the_wrong_ticker_does_nothing():
    eng = _two_positions()
    sells = eng.on_signal(parse(raw("TSLA calls closed at 2.00", channel=ET, channel_id=ET_ID)),
                          now=DAY1 + 600)
    assert sells == [] and len(eng.open_positions) == 2


# ── expiry releases the slot ─────────────────────────────────────────────────
def test_zero_dte_position_expires_on_the_next_day():
    eng = engine(pipe_test=False)
    eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $700 Exp 0DTE Current BID Price $1.00")),
                  now=DAY1)
    assert len(eng.open_positions) == 1
    eng.on_signal(parse(raw("CALL TICKER SPY STRIKE $600 Exp 0DTE Current BID Price $1.00")),
                  now=DAY2)
    expires = [f for f in eng.fills.records if f.event == "expire"]
    assert len(expires) == 1 and expires[0].contract_key.startswith("QQQ|")
    assert expires[0].accepted is False, "an unmarked expiry must not enter realised P&L"
    assert eng.stats.expired == 1
    assert eng.stats.realized_usd == 0.0


def test_a_dated_position_that_has_expired_is_released():
    eng = engine(pipe_test=False)
    # 2025-09-11 is one day after DAY1, so a jump forward expires it for certain.
    eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $700 Exp 2025-09-11 Current BID Price $1.00")),
                  now=DAY1)
    eng.on_signal(parse(raw("CALL TICKER SPY STRIKE $600 Exp 0DTE Current BID Price $1.00")),
                  now=DAY1 + 10 * 86_400)
    assert eng.stats.expired == 1


def test_a_dated_position_with_time_left_is_not_released():
    """Releasing a live position would under-count risk, which is worse than
    blocking: the slot is only freed when the contract provably cannot exist."""
    eng = engine(pipe_test=False, max_concurrent=2)
    eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $700 Exp 2026-09-11 Current BID Price $1.00")),
                  now=DAY1)
    eng.on_signal(parse(raw("CALL TICKER SPY STRIKE $600 Exp 0DTE Current BID Price $1.00")),
                  now=DAY1 + 60)
    assert eng.stats.expired == 0
    assert len(eng.open_positions) == 2
    # and the third entry is refused, because both slots are genuinely in use
    eng.on_signal(parse(raw("CALL TICKER NVDA STRIKE $200 Exp 0DTE Current BID Price $1.00")),
                  now=DAY1 + 120)
    assert eng.fills.records[-1].reason.startswith("max_concurrent")


def test_an_unparseable_expiry_alerts_instead_of_being_released():
    eng = engine(pipe_test=False)
    eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $700 Exp Dec 5 Current BID Price $1.00")),
                  now=DAY1)
    eng.on_signal(parse(raw("CALL TICKER SPY STRIKE $600 Exp 0DTE Current BID Price $1.00")),
                  now=DAY1 + 86_400)
    # A further day forces the roll that sweeps the book.
    eng.on_signal(parse(raw("CALL TICKER IWM STRIKE $220 Exp 0DTE Current BID Price $1.00")),
                  now=DAY1 + 2 * 86_400)
    reasons = [f.reason for f in eng.fills.records if f.event == "alert"]
    assert any(r.startswith("expiry_unparsed") for r in reasons)
    assert eng.stats.expired == 1, "the 0DTE SPY position releases; 'Dec 5' must not"
