"""Strict shorthand contract anchor + the close-adjective guard.

Live traffic can carry a one-line contract with no keywords at all:

    QQQ 719C Sep 11 @ 1.01     SPY 600P 0DTE @ 1.05

These tests pin that anchor down: it must recover ticker/side/strike/expiry/
his_bid, normalise a month/day expiry to ISO with a year-roll, refuse a year
(2026) as a strike or price, and never let commentary become an entry. They also
pin the "close" fix: the adjective in "we are close to target" is commentary,
while "Close at ...", "Closed ...", "Added ..." and "all out" keep their meaning.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.parser import parse
from scorpion_mff.types import AlertKind, Quote, RawMessage, Side

ET = "etf-options"
ET_ID = "1231301953972207667"


def _ts(year: int, month: int, day: int, hour: int = 13, minute: int = 50) -> float:
    """A fixed UTC instant, so the year-roll tests are deterministic."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()


SEP8 = _ts(2026, 9, 8)


def raw(text: str, ts: float = SEP8, channel: str = ET, channel_id: str = ET_ID) -> RawMessage:
    return RawMessage(channel=channel, channel_id=channel_id, text=text, ts=ts)


class _AlwaysQuote:
    def quote(self, key: str) -> Quote:
        return Quote(1.0, 1.05)


# ── positives: the anchor reads the whole contract ───────────────────────────
def test_call_shorthand_with_month_day():
    s = parse(raw("QQQ 719C Sep 11 @ 1.01"))
    assert s.kind is AlertKind.ENTRY
    assert s.ticker == "QQQ"
    assert s.side is Side.CALL
    assert s.strike == 719
    assert s.expiry == "2026-09-11"
    assert s.his_bid == 1.01
    assert s.contract_key == "QQQ|CALL|719|2026-09-11"


def test_put_shorthand_with_0dte():
    s = parse(raw("SPY 600P 0DTE @ 1.05"))
    assert s.kind is AlertKind.ENTRY
    assert s.ticker == "SPY"
    assert s.side is Side.PUT
    assert s.strike == 600
    assert s.expiry == "0DTE"
    assert s.his_bid == 1.05
    assert s.contract_key == "SPY|PUT|600|0DTE"


def test_shorthand_is_case_insensitive():
    s = parse(raw("qqq 719c sep 11 @ 1.01"))
    assert s.contract_key == "QQQ|CALL|719|2026-09-11"


def test_fractional_strike():
    s = parse(raw("NVDA 227.5P Sep 11 @ 2.63"))
    assert s.side is Side.PUT
    assert s.strike == 227.5
    assert s.expiry == "2026-09-11"
    assert s.his_bid == 2.63


def test_iso_expiry_is_not_read_as_strike_or_price():
    s = parse(raw("QQQ 719C 2026-09-11 @ 1.01"))
    assert s.strike == 719
    assert s.his_bid == 1.01
    assert s.expiry == "2026-09-11"


def test_numeric_month_day_expiry():
    s = parse(raw("QQQ 719C 9/11 @ 1.01"))
    assert s.expiry == "2026-09-11"


def test_year_roll_when_the_month_day_has_passed():
    s = parse(raw("QQQ 719C Jan 5 @ 1.01", ts=_ts(2026, 12, 20)))
    assert s.expiry == "2027-01-05"


def test_dollar_sign_on_the_bid():
    s = parse(raw("SPY 600P 0DTE @ $1.05"))
    assert s.his_bid == 1.05


@pytest.mark.parametrize("token", ["ODTE", "TODAY", "0-DTE"])
def test_same_day_tokens_normalise_to_0dte(token):
    s = parse(raw(f"SPY 600P {token} @ 1.05"))
    assert s.expiry == "0DTE"


def test_full_month_name():
    s = parse(raw("QQQ 719C September 11 @ 1.01"))
    assert s.expiry == "2026-09-11"


# ── negatives: the anchor is strict, commentary is never an entry ────────────
def test_commentary_without_price_is_not_an_entry():
    s = parse(raw("QQQ 719C Sep 11 looking strong"))
    assert s.kind is AlertKind.IGNORE
    assert s.strike is None and s.side is None


def test_missing_expiry_is_not_an_entry():
    s = parse(raw("QQQ 719C @ 1.01"))
    assert s.kind is AlertKind.IGNORE
    assert s.strike is None


def test_missing_glued_side_is_not_an_entry():
    s = parse(raw("QQQ 719 Sep 11 @ 1.01"))
    assert s.kind is AlertKind.IGNORE
    assert s.strike is None


def test_year_is_never_a_strike():
    s = parse(raw("QQQ 2026C Sep 11 @ 1.01"))
    assert s.kind is AlertKind.IGNORE
    assert s.strike is None


def test_year_is_never_a_price():
    s = parse(raw("QQQ 719C Sep 11 @ 2026"))
    assert s.kind is AlertKind.IGNORE
    assert s.his_bid is None


def test_trailing_commentary_breaks_the_anchor():
    s = parse(raw("QQQ 719C Sep 11 @ 1.01 let's go"))
    assert s.kind is AlertKind.IGNORE
    assert s.strike is None


# ── "close" is an adjective in commentary, a verb in a close phrase ──────────
@pytest.mark.parametrize("text", [
    "we are close to target",
    "close to the money now",
    "so close",
])
def test_the_adjective_close_is_not_an_exit(text):
    s = parse(raw(text))
    assert s.kind is AlertKind.IGNORE
    assert s.kind is not AlertKind.CLOSE


@pytest.mark.parametrize("text", [
    "Close at 2.65",
    "Close remaining for 10%",
    "Close remaining contracts for 14%",
    "Close out the rest",
])
def test_close_as_a_verb_still_fires(text):
    assert parse(raw(text)).kind is AlertKind.CLOSE


def test_closed_still_fires():
    assert parse(raw("QQQ puts closed at 2.85")).kind is AlertKind.CLOSE


def test_added_still_fires():
    assert parse(raw("Added at 1.30")).kind is AlertKind.ADD


def test_all_out_is_still_a_close():
    s = parse(raw("all out"))
    assert s.kind is AlertKind.CLOSE
    assert s.ticker == ""


# ── "all out" stays resolvable (or ambiguous) in the engine ──────────────────
def _engine() -> Engine:
    return Engine(config=EngineConfig(pipe_test=False), fills=FillLog(), quotes=_AlwaysQuote())


def test_all_out_resolves_a_single_position():
    eng = _engine()
    eng.on_signal(parse(raw("SPY 600P 0DTE @ 1.05")), now=SEP8)
    sells = eng.on_signal(parse(raw("all out")), now=SEP8 + 60)
    assert len(sells) == 1
    assert not eng.open_positions


def test_all_out_is_ambiguous_with_two_positions():
    eng = _engine()
    eng.on_signal(parse(raw("SPY 600P 0DTE @ 1.05")), now=SEP8)
    eng.on_signal(parse(raw("QQQ 719C 0DTE @ 1.01")), now=SEP8 + 60)
    assert eng.on_signal(parse(raw("all out")), now=SEP8 + 120) == []
    assert any("ambiguous_exit" in r.reason for r in eng.fills.records)
