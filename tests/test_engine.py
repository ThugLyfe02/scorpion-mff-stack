"""The engine: the locked rules as behaviour, one test per rule."""

from __future__ import annotations

import pytest

from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.parser import parse
from scorpion_mff.types import Quote, RawMessage

ET = "etf-options"
KING = "options-king"
HIGH = "high-confidence-options"


def raw(text: str, channel: str = ET) -> RawMessage:
    return RawMessage(channel=channel, channel_id={"etf-options": "1", "options-king": "2",
                                                   "high-confidence-options": "3"}.get(channel, "9"),
                      text=text, ts=1000.0)


def entry(ticker="QQQ", strike="$719", bid="$1.01", channel=ET, expiry="Today") -> RawMessage:
    side = "CALL"
    return raw(f"{side} TICKER {ticker} STRIKE {strike} Exp {expiry} Current BID Price {bid}",
               channel=channel)


def engine(**cfg) -> Engine:
    return Engine(config=EngineConfig(**cfg), fills=FillLog(), clock=lambda: 1000.0)


def reasons(eng: Engine) -> list[str]:
    return [f.reason for f in eng.fills.records if not f.accepted]


# ── entry sizing ─────────────────────────────────────────────────────────────
def test_first_trade_of_the_day_is_a_one_contract_pipe_test():
    eng = engine()
    intents = eng.on_signal(parse(entry()), Quote(bid=1.01, ask=1.05))
    assert len(intents) == 1
    assert intents[0].qty == 1
    assert intents[0].limit == 1.05
    assert "pipe" in intents[0].reason


def test_second_trade_uses_the_channel_clip():
    eng = engine()
    eng.on_signal(parse(entry()), Quote(bid=1.01, ask=1.05))
    intents = eng.on_signal(parse(entry(ticker="SPY", strike="$700")), Quote(bid=1.00, ask=1.00))
    # $1500 clip / ($1.00 * 100) = 15 contracts
    assert intents[0].qty == 15
    assert "standard" in intents[0].reason


def test_high_confidence_channel_gets_the_bigger_clip():
    eng = engine(pipe_test=False)
    intents = eng.on_signal(parse(entry(channel=HIGH)), Quote(bid=1.00, ask=1.00))
    # $3000 clip / $100 = 30 contracts
    assert intents[0].qty == 30
    assert "high_confidence" in intents[0].reason


def test_without_the_pipe_test_the_first_trade_sizes_at_full_clip():
    eng = engine(pipe_test=False)
    intents = eng.on_signal(parse(entry()), Quote(bid=1.05, ask=1.05))
    # $1500 / ($1.05*100) = 14
    assert intents[0].qty == 14


# ── the gates ────────────────────────────────────────────────────────────────
def test_more_than_25_percent_off_his_bid_is_skipped():
    eng = engine()
    intents = eng.on_signal(parse(entry()), Quote(bid=1.30, ask=1.30))
    assert intents == []
    assert any(r.startswith("too_far_off_bid") for r in reasons(eng))


def test_max_two_concurrent_positions():
    eng = engine(pipe_test=False)
    assert eng.on_signal(parse(entry(ticker="QQQ", strike="$700")), Quote(1, 1))
    assert eng.on_signal(parse(entry(ticker="SPY", strike="$500")), Quote(1, 1))
    assert eng.on_signal(parse(entry(ticker="IWM", strike="$200")), Quote(1, 1)) == []
    assert "max_concurrent:2" in reasons(eng)


def test_missing_quote_rejects_rather_than_guesses_a_price():
    eng = engine()
    assert eng.on_signal(parse(entry()), None) == []
    assert "no_quote" in reasons(eng)


def test_missing_his_bid_rejects():
    eng = engine()
    assert eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $719 Exp Today")), Quote(1, 1)) == []
    assert "no_his_bid" in reasons(eng)


def test_kill_switch_blocks_buys():
    eng = engine()
    eng.blocked = lambda: True
    assert eng.on_signal(parse(entry()), Quote(1, 1)) == []
    assert "halted:kill-switch" in reasons(eng)


def test_stand_down_halts_new_buys_but_still_allows_exits():
    eng = engine()
    eng.on_signal(parse(entry()), Quote(bid=1.01, ask=1.05))
    eng.on_signal(parse(raw("STAND DOWN", KING)), Quote(1, 1))
    assert eng.halted
    assert eng.on_signal(parse(entry(ticker="SPY", strike="$500")), Quote(1, 1)) == []
    assert "halted:stand-down" in reasons(eng)
    # an open position must remain exitable while halted
    sells = eng.on_signal(parse(raw("Close at 1.20", ET)), Quote(bid=1.20, ask=1.25))
    assert len(sells) == 1 and sells[0].action == "sell"


# ── never chase ──────────────────────────────────────────────────────────────
def test_a_closed_contract_is_never_chased():
    eng = engine()
    eng.on_signal(parse(entry()), Quote(bid=1.01, ask=1.05))
    eng.on_signal(parse(raw("Close at 1.11", ET)), Quote(bid=1.11, ask=1.14))
    assert eng.on_signal(parse(entry()), Quote(bid=1.01, ask=1.05)) == []
    assert "already_closed:never_chase" in reasons(eng)


def test_an_open_contract_is_not_duplicated():
    eng = engine()
    eng.on_signal(parse(entry()), Quote(bid=1.01, ask=1.05))
    assert eng.on_signal(parse(entry()), Quote(bid=1.01, ask=1.05)) == []
    assert "already_open" in reasons(eng)


# ── adds ─────────────────────────────────────────────────────────────────────
def test_add_is_allowed_once_and_only_with_a_position():
    eng = engine()
    eng.on_signal(parse(entry()), Quote(bid=1.01, ask=1.05))
    first = eng.on_signal(parse(raw("Adding at 1.20", ET)), Quote(bid=1.20, ask=1.22))
    assert len(first) == 1 and first[0].action == "buy"
    second = eng.on_signal(parse(raw("New avg 1.30", ET)), Quote(bid=1.30, ask=1.32))
    assert second == []
    assert "add_already_used" in reasons(eng)


def test_add_without_a_position_is_skipped():
    eng = engine()
    assert eng.on_signal(parse(raw("Adding at 1.20", ET)), Quote(1.2, 1.22)) == []
    assert "add_without_position" in reasons(eng)


def test_add_rolls_the_average_price():
    eng = engine()
    eng.on_signal(parse(entry()), Quote(bid=1.00, ask=1.00))          # pipe: 1 @ 1.00
    intents = eng.on_signal(parse(raw("Adding at 2.00", ET)), Quote(bid=2.00, ask=2.00))
    # $2250 add clip / ($2.00 * 100) = 11 contracts
    assert intents[0].qty == 11
    pos = next(iter(eng.positions.values()))
    assert pos.contracts == 12
    assert pos.adds == 1
    assert pos.avg_price == pytest.approx((1.00 * 1 + 2.00 * 11) / 12)


# ── exits ────────────────────────────────────────────────────────────────────
def test_trim_to_one_and_move_the_stop_to_breakeven():
    eng = engine(pipe_test=False)
    eng.on_signal(parse(entry()), Quote(bid=1.00, ask=1.00))
    pos = next(iter(eng.positions.values()))
    assert pos.contracts == 15
    sells = eng.on_signal(parse(raw("Runners @ 25% close or keep as lotto", ET)),
                          Quote(bid=1.30, ask=1.35))
    assert sells[0].qty == 14                    # 15 -> trim target 1
    assert pos.contracts == 1
    assert pos.breakeven_stop == pos.avg_price
    assert eng.stats.realized_usd == pytest.approx((1.30 - 1.00) * 14 * 100)


def test_trim_with_nothing_to_trim_is_skipped():
    eng = engine()
    eng.on_signal(parse(entry()), Quote(bid=1.00, ask=1.00))     # 1 contract
    assert eng.on_signal(parse(raw("Runners at $3.00", ET)), Quote(1.3, 1.35)) == []
    assert "nothing_to_trim" in reasons(eng)


def test_close_marks_the_position_closed_and_realises_pnl():
    eng = engine()
    eng.on_signal(parse(entry()), Quote(bid=1.00, ask=1.00))
    sells = eng.on_signal(parse(raw("Close remaining for 10%", ET)), Quote(bid=1.10, ask=1.12))
    assert sells[0].qty == 1
    assert eng.stats.realized_usd == pytest.approx(10.0)
    assert not eng.open_positions


def test_close_without_a_position_is_skipped_not_invented():
    eng = engine()
    assert eng.on_signal(parse(raw("Close at 2.65", ET)), Quote(2.6, 2.7)) == []
    assert reasons(eng)


def test_exit_with_no_contract_resolves_by_channel():
    eng = engine()
    eng.on_signal(parse(entry()), Quote(bid=1.00, ask=1.00))
    sells = eng.on_signal(parse(raw("Close at 1.25", ET)), Quote(bid=1.25, ask=1.30))
    assert len(sells) == 1


def test_ambiguous_exit_does_nothing():
    """Two open positions in one channel and an exit that names neither."""
    eng = engine(pipe_test=False)
    eng.on_signal(parse(entry(ticker="QQQ", strike="$700")), Quote(1, 1))
    eng.on_signal(parse(entry(ticker="SPY", strike="$500")), Quote(1, 1))
    assert eng.on_signal(parse(raw("Close at 1.25", ET)), Quote(1.25, 1.3)) == []


# ── audit ────────────────────────────────────────────────────────────────────
def test_every_decision_is_logged_with_a_reason():
    eng = engine()
    eng.on_signal(parse(entry()), Quote(bid=1.01, ask=1.05))
    eng.on_signal(parse(raw("Close at 1.11", ET)), Quote(1.11, 1.14))
    eng.on_signal(parse(entry()), Quote(1.01, 1.05))
    assert [f.event for f in eng.fills.records] == ["buy", "sell", "skip"]
    assert all(f.reason for f in eng.fills.records)


def test_ignored_posts_are_logged_with_the_policy_reason():
    eng = engine()
    eng.on_signal(parse(raw("CALL TICKER NKE STRIKE $70 Exp Dec 5 Current BID Price $1.10")), Quote(1, 1))
    assert "ignored:ticker_excluded" in reasons(eng)
