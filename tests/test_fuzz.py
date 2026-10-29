"""Adversarial fuzzing of the hot path, and the two bugs it found.

The value of a fuzzer is entirely in its invariants. Each test here is either the
campaign itself (deterministic, so it can run in CI) or a specific money-losing
behaviour the campaign surfaced.
"""

from __future__ import annotations

import random

import pytest

from scorpion_mff import fuzz
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.parser import parse
from scorpion_mff.types import AlertKind, Quote, RawMessage

KING = "options-king"
KING_ID = "968352649437126676"
ET = "etf-options"
ET_ID = "1231301953972207667"
DAY = 1_757_000_000.0


class Quotes:
    def quote(self, key):
        return Quote(1.0, 1.05)


def raw(text, channel=KING, channel_id=KING_ID):
    return RawMessage(channel=channel, channel_id=channel_id, text=text, ts=DAY)


def engine(blocked=False):
    return Engine(config=EngineConfig(pipe_test=False), fills=FillLog(), quotes=Quotes(),
                  blocked=lambda: blocked)


# ── the campaign itself ────────────────────────────────────────────────────
def test_a_campaign_is_clean_and_deterministic():
    a = fuzz.run_campaign(seed=7, iterations=1500)
    b = fuzz.run_campaign(seed=7, iterations=1500)
    assert a.clean, a.to_text()
    assert a.statuses == b.statuses and a.intents == b.intents
    assert fuzz.determinism_check(seed=7, iterations=300)


def test_the_generator_reaches_every_classifier_branch():
    """A campaign that only ever produced ignorable posts would prove nothing."""
    campaign = fuzz.run_campaign(seed=11, iterations=1500)
    seen = set(campaign.statuses) - {"open_at_end"}
    assert {AlertKind.ENTRY.value, AlertKind.CLOSE.value, AlertKind.IGNORE.value} <= seen


def test_the_parser_never_raises_on_generated_posts():
    rng = random.Random(3)
    for _ in range(2000):
        post = fuzz.generate_post(rng)
        parse(post)                                   # must not raise


# ── the first bug: a named exit closed the wrong contract ──────────────────
def test_a_named_exit_for_an_unheld_ticker_does_not_close_a_held_position():
    """Fuzzer find: 'PLTR CALLs closing at 7.76' flattened a held NVDA put, because
    NVDA was the only position in that channel. A post that names a contract we do not
    hold must resolve to 'no position', never to 'some position'."""
    eng = engine()
    eng.on_signal(parse(raw("PUT TICKER NVDA STRIKE $390 Exp 0DTE Current BID Price $1.00")),
                  now=DAY)
    assert len(eng.open_positions) == 1

    sells = eng.on_signal(parse(raw("PLTR CALLs closing at 7.76")), now=DAY + 60)
    assert sells == [], "a named exit for a contract we do not hold must not sell anything"
    assert len(eng.open_positions) == 1
    assert eng.fills.records[-1].event == "skip"


def test_an_unnamed_exit_still_closes_the_only_position_in_its_channel():
    """The fix must not break the case it was designed for."""
    eng = engine()
    eng.on_signal(parse(raw("PUT TICKER NVDA STRIKE $390 Exp 0DTE Current BID Price $1.00")),
                  now=DAY)
    sells = eng.on_signal(parse(raw("Close at 1.80")), now=DAY + 60)
    assert len(sells) == 1 and sells[0].contract_key.startswith("NVDA|")
    assert not eng.open_positions


def test_a_named_exit_matching_one_position_still_works():
    eng = engine()
    eng.on_signal(parse(raw("PUT TICKER NVDA STRIKE $390 Exp 0DTE Current BID Price $1.00")),
                  now=DAY)
    sells = eng.on_signal(parse(raw("NVDA puts closed at 1.80")), now=DAY + 60)
    assert len(sells) == 1 and not eng.open_positions


# ── the second bug: the kill switch must not trap a position ───────────────
def test_the_kill_switch_blocks_entries():
    eng = engine(blocked=True)
    intents = eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $700 Exp 0DTE Current BID Price $1.00")),
                            now=DAY)
    assert intents == []
    assert "kill-switch" in eng.fills.records[-1].reason


def test_the_kill_switch_still_allows_an_exit():
    """Fuzzer find (as a wrong invariant, then a property worth pinning): a switch that
    traps you in a position is worse than no switch."""
    eng = engine(blocked=False)
    eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $700 Exp 0DTE Current BID Price $1.00")),
                  now=DAY)
    eng.blocked = lambda: True
    sells = eng.on_signal(parse(raw("QQQ calls closed at 1.50")), now=DAY + 60)
    assert len(sells) == 1, "exits must survive the kill switch"
    assert not eng.open_positions


# ── the percentage trap, which the campaign keeps attacking ────────────────
@pytest.mark.parametrize("text", [
    "Close at 19%", "Runners 200%", "Close remaining for 10%",
    "QQQ 610 strike closed for 1200%", "All out, up 300%",
])
def test_a_percentage_never_becomes_a_sale_price(text):
    eng = engine()
    eng.on_signal(parse(raw("CALL TICKER QQQ STRIKE $610 Exp 0DTE Current BID Price $3.13")),
                  now=DAY)
    sells = eng.on_signal(parse(raw(text)), now=DAY + 60)
    for sell in sells:
        assert sell.limit < 20, f"{text!r} priced a sell at {sell.limit}"
    if sells:
        assert sells[0].limit <= 610, "never above the strike"
