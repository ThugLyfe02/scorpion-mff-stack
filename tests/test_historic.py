"""The executor, driven by the historical corpus.

Two classes of test here:
  * the corpus loader / tape builder - arithmetic that must not drift (exit levels,
    clock times, contract keys, quote sequencing);
  * the end-to-end backtest - invariants plus the exact numbers, so a silent
    behaviour change in the parser, pricing, sizing or engine shows up as a red
    test instead of a different report.
"""

from __future__ import annotations

import pytest

from scorpion_mff import historic
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.parser import parse
from scorpion_mff.types import Quote

# The corpus publishes his own outcomes: 57 win_partial, 24 win, 6 loss,
# 2 scratch, 4 unknown. Those are claims about the CHANNEL, not the executor.
CORPUS_TOTAL = 93
CORPUS_DAYS = 59


# ── corpus + tape arithmetic ─────────────────────────────────────────────────
def test_corpus_is_loaded_sorted_and_complete():
    trades = historic.load_trades()
    assert len(trades) == CORPUS_TOTAL
    assert len({t.date for t in trades}) == CORPUS_DAYS
    assert trades == sorted(trades, key=lambda t: (t.date, t.time_et))
    assert all(t.entry_bid > 0 for t in trades)


def test_exit_level_is_entry_times_one_plus_pct():
    t = historic.load_trades()[0]                    # 0.95, +200%
    assert (t.exit_pct, t.exit_price) == (200.0, pytest.approx(2.85))
    negative = [x for x in historic.load_trades() if (x.exit_pct or 0) < 0][0]
    assert negative.exit_price < negative.entry_bid, "a negative exit must mark below entry"


def test_exit_time_comes_from_the_notes_when_they_state_it():
    """'10% ready-to-close 10:35; ... all out 10:36' -> the close is 10:36."""
    t = [x for x in historic.load_trades() if x.date == "2026-03-12"][0]
    assert t.time_et == "10:32"
    assert t.exit_ts - t.entry_ts == pytest.approx(4 * 60)   # 10:32 -> 10:36


def test_exit_time_falls_back_to_the_default_hold():
    """A note with no plausible clock time gets entry + 20 minutes."""
    t = historic.Trade(date="2026-05-05", time_et="10:00", channel="etf-options",
                       ticker="QQQ", side="call", strike="600", expiry="2026-05-05",
                       entry_bid=1.0, exit_pct=50.0, outcome="win", notes="all out")
    assert t.exit_ts - t.entry_ts == pytest.approx(historic.DEFAULT_HOLD_MINUTES * 60)


def test_contract_key_normalises_the_strike_format():
    """The corpus writes 227.50; the parser derives 227.5. Same contract."""
    t = historic.Trade(date="2026-08-13", time_et="06:45", channel="options-king",
                       ticker="NVDA", side="call", strike="227.50", expiry="2026-08-17",
                       entry_bid=1.0, exit_pct=10.0, outcome="win", notes="")
    assert t.contract_key == "NVDA|CALL|227.5|2026-08-17"
    assert parse(historic.entry_message(t)).contract_key == t.contract_key


def test_entry_and_exit_posts_parse_into_what_the_engine_expects():
    t = historic.load_trades()[0]
    entry = parse(historic.entry_message(t))
    assert entry.actionable and entry.contract_key == t.contract_key
    exit_ = parse(historic.exit_message(t))
    assert exit_.kind.value == "close" and exit_.price == pytest.approx(t.exit_price)


def test_bare_exit_post_names_nothing_and_named_one_names_the_ticker():
    t = historic.load_trades()[0]
    bare = parse(historic.exit_message(t, named=False))
    named = parse(historic.exit_message(t))
    assert bare.ticker == "" and bare.price == pytest.approx(t.exit_price)
    assert named.ticker == t.ticker and named.side is not None


def test_quote_script_alternates_entry_then_exit_per_contract():
    trades = [historic.load_trades()[0]]
    script = historic.build_quotes(trades, ask_multiple=1.05)
    pairs = script[trades[0].contract_key]
    assert pairs[0][0] == pytest.approx(trades[0].entry_bid)
    assert pairs[0][1] == pytest.approx(trades[0].entry_bid * 1.05)
    assert pairs[1][0] == pytest.approx(trades[0].exit_price)


# ── the backtest itself ──────────────────────────────────────────────────────
def test_backtest_accounts_for_every_alert():
    r = historic.run_backtest()
    assert r.taken + r.skipped + r.rejected == r.trades, "every alert must be classified"
    assert r.trades == CORPUS_TOTAL and r.days == CORPUS_DAYS
    assert all(x.entry_limit > 0 for x in r.results if x.taken), "no unpriced entry"
    assert r.pnl_usd == pytest.approx(sum(x.pnl_usd for x in r.results), abs=0.01)


def test_backtest_is_deterministic():
    a, b = historic.run_backtest(), historic.run_backtest()
    assert (a.taken, a.contracts, a.pnl_usd) == (b.taken, b.contracts, b.pnl_usd)


def test_locked_rules_exclude_only_nke():
    """2 of 93 alerts are NKE, which the locked rules keep off the hone."""
    r = historic.run_backtest()
    off = [x for x in r.results if not x.taken]
    assert [x.trade.ticker for x in off] == ["NKE", "NKE"]
    assert all(x.decision == "ignored:ticker_excluded" for x in off)


def test_backtest_numbers_are_pinned():
    """The whole pipeline, one number each. A change here is a change in behaviour."""
    r = historic.run_backtest()
    assert r.taken == 91
    assert r.contracts == 405
    assert r.pnl_usd == pytest.approx(57_951.00, abs=0.01)
    assert r.winners == 80 and r.losers == 8


def test_bare_exits_do_not_change_this_corpus():
    """Nothing in the corpus holds two same-channel positions into an exit, so the
    named/bare distinction costs nothing here. It is insurance, not a fix for a
    number - the two-position case is covered in test_exit_attribution.py."""
    named = historic.run_backtest(named_exits=True)
    bare = historic.run_backtest(named_exits=False)
    assert (named.taken, named.pnl_usd) == (bare.taken, bare.pnl_usd)


def test_the_gate_refuses_the_corpus_when_the_ask_is_far_off():
    """At 30% through his bid every entry is past the +25% gate: the rule decides
    whether the hone trades at all, which is why it is not tuned away."""
    far = historic.run_backtest(ask_multiple=1.30)
    assert far.taken <= 2
    assert far.skip_reasons.get("too_far_off_bid")


def test_exit_slippage_reduces_pnl_monotonically():
    base = historic.run_backtest().pnl_usd
    cut = historic.run_backtest(exit_slippage=0.90).pnl_usd
    assert cut < base


# ── the defect the corpus found: a carried position never released its slot ──
def test_a_position_with_no_exit_does_not_halt_the_next_days():
    """The corpus has alerts with no recorded exit. Before the expiry sweep they
    held both concurrency slots for the rest of the record and every later entry
    was skipped - the hone would have gone silent, which is the 9/8 failure again.
    """
    trades = historic.load_trades()
    unresolved = [t for t in trades if t.exit_pct is None and t.ticker != "NKE"]
    assert unresolved, "the corpus must still contain this case for the test to mean anything"

    engine = Engine(config=EngineConfig(pipe_test=False), fills=FillLog(),
                    quotes=_AlwaysQuote())
    first = unresolved[0]
    entry = parse(historic.entry_message(first))
    # Price it off its own posted bid: the point of this test is the slot, not pricing.
    engine.on_signal(entry, quote=Quote(entry.his_bid, entry.his_bid * 1.02),
                     now=first.entry_ts)
    assert len(engine.open_positions) == 1

    later = [t for t in trades if t.date > first.date][0]
    later_sig = parse(historic.entry_message(later))
    engine.on_signal(later_sig, quote=Quote(later_sig.his_bid, later_sig.his_bid * 1.02),
                     now=later.entry_ts)
    assert engine.stats.expired >= 1, "the unresolved position must have been released"
    assert any(f.event == "buy" for f in engine.fills.records[-2:]), \
        "the next day's entry must still be taken"


class _AlwaysQuote:
    def quote(self, key: str):
        from scorpion_mff.types import Quote
        return Quote(1.0, 1.05)
