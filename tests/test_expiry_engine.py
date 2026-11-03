"""The expiry guard on the LIVE path - the join, not the parts.

`calendar.py` and `expiry.py` already know how to decide. Task 2.a is the wiring that
makes the decision happen to a real book, and the tests here are about that join:

  * the day label is the ET trading date, so a same-day contract does not expire on
    yesterday's UTC date and the daily book does not roll at 20:00 ET;
  * `_expire_carried` still frees the concurrency slot - and can never do it silently;
  * `Engine.expiry_intents` flattens at a live bid or PAGES, never at a price it does
    not have, and never at all before the cutoff;
  * `Listener.tick` gives the guard its own clock, so 15:45 ET fires with no traffic,
    and the tick does not eat the message it interrupted;
  * the 93-alert corpus backtest and its $57,951.00 stay exactly as they were.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timezone

import pytest

from scorpion_mff import historic
from scorpion_mff.broker import PaperBroker, StaticQuotes
from scorpion_mff.calendar import et_offset, session_for
from scorpion_mff.cli import expiry_audit, main, pump_listener
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.parser import parse
from scorpion_mff.types import Quote, RawMessage

ET_CH = "etf-options"
ET_ID = "1231301953972207667"
FRIDAY = "2026-09-11"          # a regular session: close 16:00 ET, cutoff 15:45
HALF_DAY = "2026-11-27"        # published half day: close 13:00 ET, cutoff 12:45
QQQ = "QQQ|CALL|700|0DTE"


# ── clocks ───────────────────────────────────────────────────────────────────
def et_ts(day: str, hh: int, mm: int) -> float:
    """A wall clock in New York, as epoch seconds. DST comes from the calendar."""
    d = date.fromisoformat(day)
    return datetime.combine(d, time(hh, mm), tzinfo=timezone(et_offset(d))).timestamp()


def utc_ts(y: int, mo: int, d: int, hh: int, mm: int) -> float:
    return datetime(y, mo, d, hh, mm, tzinfo=UTC).timestamp()


def raw(text: str, ts: float) -> RawMessage:
    return RawMessage(channel=ET_CH, channel_id=ET_ID, text=text, ts=ts)


def entry(ticker: str = "QQQ", strike: str = "700", expiry: str = "0DTE",
          side: str = "CALL", bid: str = "1.00", ts: float = 0.0) -> RawMessage:
    return raw(f"{side} TICKER {ticker} STRIKE ${strike} Exp {expiry} "
               f"Current BID Price ${bid}", ts)


def engine(**cfg: object) -> Engine:
    cfg.setdefault("pipe_test", False)
    return Engine(config=EngineConfig(**cfg), fills=FillLog())          # type: ignore[arg-type]


def book(**cfg: object) -> Engine:
    """An engine holding ONE 0DTE QQQ call bought at 10:00 ET on `FRIDAY`.

    `pipe_test=True` (the default) is what makes it one contract: the arithmetic below
    is then about the guard, not about clip sizing.
    """
    cfg.setdefault("enforce_expiry", True)
    cfg.setdefault("pipe_test", True)
    eng = engine(**cfg)
    eng.on_signal(parse(entry()), quote=Quote(1.00, 1.05), now=et_ts(FRIDAY, 10, 0))
    assert list(eng.positions) == [QQQ]
    return eng


def reasons(eng: Engine, event: str) -> list[str]:
    return [f.reason for f in eng.fills.events(event)]


# ── the ET roll ──────────────────────────────────────────────────────────────
def test_the_day_label_is_the_et_trading_date_not_the_utc_date():
    """01:30 UTC on the 11th is still 21:30 on the 10th in New York."""
    eng = engine()
    eng.on_signal(parse(entry()), quote=Quote(1.0, 1.05), now=utc_ts(2026, 9, 11, 1, 30))
    assert eng.day == "2026-09-10"


def test_the_roll_happens_at_et_midnight_even_though_the_utc_date_is_unchanged():
    """Both instants are 2026-09-11 in UTC; only the ET trading date changes."""
    eng = engine(enforce_expiry=True)
    eng.on_signal(parse(entry()), quote=Quote(1.0, 1.05), now=utc_ts(2026, 9, 11, 3, 50))
    assert eng.day == "2026-09-10"
    assert eng.fills.events("day_roll") == []
    eng.on_signal(parse(entry(ticker="SPY", strike="600")), quote=Quote(1.0, 1.05),
                  now=utc_ts(2026, 9, 11, 4, 10))            # ET 00:10 on the 11th
    assert [f.reason for f in eng.fills.events("day_roll")] == ["new_day:2026-09-11"]
    assert eng.stats.trades_today == 1, "the new day's first trade is a pipe test again"
    assert eng.stats.expired == 1, "the previous ET day's 0DTE is released at ET midnight"


def test_an_iso_expiry_is_not_released_a_day_early_at_0130_utc():
    """The seam the UTC label got wrong: 2026-09-12T01:30Z is still the 11th in ET."""
    eng = engine(enforce_expiry=True, max_concurrent=2)
    eng.on_signal(parse(entry(expiry="2026-09-11")), quote=Quote(1.0, 1.05),
                  now=et_ts(FRIDAY, 9, 35))
    eng.on_signal(parse(entry(ticker="SPY", strike="600")), quote=Quote(1.0, 1.05),
                  now=utc_ts(2026, 9, 12, 1, 30))
    assert eng.stats.expired == 0, "a contract dated the 11th is not expired on the 11th"
    assert len(eng.open_positions) == 2
    assert eng.fills.events("day_roll") == []


def test_the_et_date_needs_no_holiday_table():
    """The ET date only needs the DST rule, so an unknown year labels instead of raising.

    (The 9/8 tape and a lot of this repo run at epoch 1000.) Failing closed belongs to
    the SESSION lookup, which is where the guard asks - see the test below.
    """
    eng = engine()
    eng.on_signal(parse(entry()), quote=Quote(1.0, 1.05), now=1000.0)
    assert eng.day == "1969-12-31", "epoch 1000 is still 31 Dec in New York"
    assert eng.calendar_warning == ""


def test_a_broken_clock_does_not_take_the_day_boundary_down_with_it():
    eng = engine(enforce_expiry=True)
    eng.on_signal(parse(entry()), quote=Quote(1.0, 1.05), now=1e18)
    assert eng.day == ""
    assert eng.calendar_warning.startswith("calendar_unusable")
    assert eng.fills.events("alert")[0].reason.startswith("calendar_unusable")


def test_the_guard_fails_closed_when_the_calendar_does_not_know_the_year():
    eng = engine(enforce_expiry=True)
    eng.on_signal(parse(entry()), quote=Quote(1.0, 1.05), now=1000.0)
    assert eng.expiry_intents(1000.0, {QQQ: 2.00}) == []
    assert len(eng.open_positions) == 1, "no calendar means no cutoff to be right about"
    page = eng.fills.events("alert")[-1]
    assert page.reason.startswith("HALT_AND_PAGE:expiry_calendar_unknown_year")


# ── the release is never silent ──────────────────────────────────────────────
def test_releasing_a_slot_records_an_expiry_unresolved_alert():
    eng = book()
    eng.on_signal(parse(entry(ticker="SPY", strike="600")), quote=Quote(1.0, 1.05),
                  now=et_ts("2026-09-14", 10, 0))
    assert eng.stats.expired == 1
    expire = eng.fills.events("expire")[0]
    unresolved = [f for f in eng.fills.events("alert")
                  if f.reason.startswith("expiry_unresolved")]
    assert len(unresolved) == 1, "a released slot must never be a silent slot"
    alert = unresolved[0]
    assert alert.contract_key == expire.contract_key
    assert alert.accepted is False, "unmarked: it is not in realised P&L"
    assert "same_day" in alert.reason
    assert "assignment=unknown" in alert.reason
    assert eng.stats.realized_usd == 0.0


def test_the_release_alert_projects_shares_when_the_spot_is_known():
    eng = book()
    eng.positions[QQQ].contracts = 3
    eng._roll_day(et_ts("2026-09-14", 10, 0), {"QQQ": 710.0})
    unresolved = [f.reason for f in eng.fills.events("alert")
                  if f.reason.startswith("expiry_unresolved")]
    assert unresolved == ["expiry_unresolved:same_day:0DTE:assignment=+300"]


def test_a_long_put_projects_negative_shares():
    eng = engine(enforce_expiry=True, pipe_test=True)
    eng.on_signal(parse(entry(side="PUT")), quote=Quote(1.0, 1.05),
                  now=et_ts(FRIDAY, 10, 0))
    eng._roll_day(et_ts("2026-09-14", 10, 0), {"QQQ": 690.0})      # put is ITM
    unresolved = [f.reason for f in eng.fills.events("alert")
                  if f.reason.startswith("expiry_unresolved")]
    assert unresolved == ["expiry_unresolved:same_day:0DTE:assignment=-100"]


def test_an_unreadable_expiry_keeps_the_slot_and_says_why():
    eng = engine(enforce_expiry=True)
    eng.on_signal(parse(entry(expiry="Dec 5")), quote=Quote(1.0, 1.05),
                  now=et_ts(FRIDAY, 10, 0))
    eng.on_signal(parse(entry(ticker="SPY", strike="600")), quote=Quote(1.0, 1.05),
                  now=et_ts("2026-09-14", 10, 0))
    assert eng.stats.expired == 0, "freeing a slot on an unreadable expiry is a guess"
    unparsed = [f.reason for f in eng.fills.events("alert")
                if f.reason.startswith("expiry_unparsed")]
    assert unparsed and unparsed[0].endswith("assignment=unknown")


# ── the force-close / halt matrix ────────────────────────────────────────────
def test_before_the_cutoff_nothing_happens():
    eng = book()
    before = len(eng.fills.records)
    assert eng.expiry_intents(et_ts(FRIDAY, 15, 44), {QQQ: 2.00}) == []
    assert len(eng.fills.records) == before
    assert len(eng.open_positions) == 1


def test_at_the_cutoff_with_a_live_bid_it_flattens_at_that_bid():
    eng = book()
    intents = eng.expiry_intents(et_ts(FRIDAY, 15, 45), {QQQ: 2.00})
    assert len(intents) == 1
    intent = intents[0]
    assert (intent.action, intent.qty, intent.limit) == ("sell", 1, 2.00)
    assert intent.contract_key == QQQ
    assert intent.reason.startswith("expiry:force_close:bid=2")
    assert eng.open_positions == []
    assert eng.stats.realized_usd == pytest.approx((2.00 - 1.05) * 100)
    assert QQQ in eng.closed, "a force-closed contract must never be chased back in"
    assert [f.event for f in eng.fills.records][-1] == "sell"


def test_a_second_tick_does_not_flatten_twice():
    eng = book()
    assert len(eng.expiry_intents(et_ts(FRIDAY, 15, 46), {QQQ: 2.00})) == 1
    assert eng.expiry_intents(et_ts(FRIDAY, 15, 47), {QQQ: 2.00}) == []
    assert len(eng.fills.events("sell")) == 1


def test_at_the_cutoff_without_a_bid_it_pages_and_places_nothing():
    eng = book()
    assert eng.expiry_intents(et_ts(FRIDAY, 15, 50), {}) == []
    assert len(eng.open_positions) == 1, "risk stays counted while a human decides"
    page = eng.fills.events("alert")[-1]
    assert page.reason.startswith("HALT_AND_PAGE:cutoff_without_live_bid")
    assert page.reason.endswith("assignment=unknown")
    assert page.qty == 1


@pytest.mark.parametrize("dead_bid", [0.0, -0.5, float("nan"), None])
def test_a_dead_bid_is_silence_not_a_price(dead_bid):
    eng = book()
    quotes = {} if dead_bid is None else {QQQ: dead_bid}
    assert eng.expiry_intents(et_ts(FRIDAY, 15, 50), quotes) == []
    assert len(eng.open_positions) == 1
    assert "cutoff_without_live_bid" in eng.fills.events("alert")[-1].reason


def test_after_the_close_it_pages_even_with_a_bid():
    eng = book()
    assert eng.expiry_intents(et_ts(FRIDAY, 16, 1), {QQQ: 2.00}) == []
    assert len(eng.open_positions) == 1
    assert "at_or_after_close" in eng.fills.events("alert")[-1].reason


def test_the_page_is_deduped_but_a_new_reason_pages_again():
    eng = book()
    for _ in range(5):
        eng.expiry_intents(et_ts(FRIDAY, 15, 50), {})
    assert eng.stats.expiry_pages == 1, "an alert every tick is not a page, it is noise"
    eng.expiry_intents(et_ts(FRIDAY, 16, 1), {})          # different reason
    assert eng.stats.expiry_pages == 2


def test_a_live_bid_at_the_cutoff_reaches_the_page_as_projected_shares():
    eng = book()
    eng.expiry_intents(et_ts(FRIDAY, 15, 50), {}, {"QQQ": 710.0})
    assert eng.fills.events("alert")[-1].reason.endswith("assignment=+100")


def test_a_position_that_is_not_expiring_is_left_alone():
    eng = engine(enforce_expiry=True)
    eng.on_signal(parse(entry(expiry="2026-10-16")), quote=Quote(1.0, 1.05),
                  now=et_ts(FRIDAY, 10, 0))
    assert eng.expiry_intents(et_ts(FRIDAY, 15, 50), {"QQQ|CALL|700|2026-10-16": 2.0}) == []
    assert eng.fills.events("alert") == []


def test_an_unreadable_key_pages_rather_than_guessing():
    eng = engine(enforce_expiry=True)
    eng.on_signal(parse(entry(expiry="?")), quote=Quote(1.0, 1.05), now=et_ts(FRIDAY, 10, 0))
    key = eng.open_positions[0].contract_key
    assert eng.expiry_intents(et_ts(FRIDAY, 11, 0), {key: 2.0}) == []
    assert "HALT_AND_PAGE:expiry_unparsed" in eng.fills.events("alert")[-1].reason
    assert len(eng.open_positions) == 1


def test_a_phantom_position_pages_instead_of_being_flattened():
    eng = engine(enforce_expiry=True)
    eng.on_signal(parse(entry(expiry="2026-09-04")), quote=Quote(1.0, 1.05),
                  now=et_ts(FRIDAY, 10, 0))
    key = eng.open_positions[0].contract_key
    assert eng.expiry_intents(et_ts(FRIDAY, 11, 0), {key: 2.0}) == []
    assert "expired_unreconciled" in eng.fills.events("alert")[-1].reason
    assert len(eng.open_positions) == 1


def test_a_half_day_cutoff_is_1245_not_1545():
    """The 16:00 assumption leaves three hours of unwatched exposure on a half day."""
    same_clock_regular = book()
    assert same_clock_regular.expiry_intents(et_ts(FRIDAY, 12, 50), {QQQ: 2.00}) == []

    eng = engine(enforce_expiry=True)
    eng.on_signal(parse(entry()), quote=Quote(1.00, 1.05), now=et_ts(HALF_DAY, 10, 0))
    assert session_for(date.fromisoformat(HALF_DAY)).close_utc.hour == 18      # 13:00 EST
    assert eng.expiry_intents(et_ts(HALF_DAY, 12, 44), {QQQ: 2.00}) == []
    assert len(eng.expiry_intents(et_ts(HALF_DAY, 12, 45), {QQQ: 2.00})) == 1
    assert eng.open_positions == []


def test_the_guard_is_off_by_default_so_a_replay_is_unchanged():
    eng = book(enforce_expiry=False)
    assert eng.config.enforce_expiry is False
    assert eng.expiry_intents(et_ts(FRIDAY, 15, 50), {QQQ: 2.00}) == []
    assert eng.fills.events("alert") == []
    assert len(eng.open_positions) == 1


def test_a_quote_object_is_accepted_as_the_bid():
    eng = book()
    intents = eng.expiry_intents(et_ts(FRIDAY, 15, 45), {QQQ: Quote(2.00, 2.10)})
    assert len(intents) == 1 and intents[0].limit == pytest.approx(2.00)


# ── the listener: a clock of its own ─────────────────────────────────────────
def listener_for(eng: Engine, transport: object, quotes: object,
                 tmp_path: object, clock: float) -> Listener:
    return Listener(transport=transport, engine=eng,                # type: ignore[arg-type]
                    broker=PaperBroker(), quotes=quotes,            # type: ignore[arg-type]
                    budget=LatencyBudget(),
                    heartbeat=Heartbeat(path=tmp_path / "hb.json"),   # type: ignore[operator]
                    kill=KillSwitch(path=tmp_path / "NO_TRADE"),      # type: ignore[operator]
                    clock=lambda: clock)


def test_tick_force_closes_through_the_broker(tmp_path):
    eng = book()
    cutoff = et_ts(FRIDAY, 15, 45)
    listener = listener_for(eng, TapeTransport([]), StaticQuotes({QQQ: (2.00, 2.10)}),
                            tmp_path, cutoff)
    intents = listener.tick(cutoff)
    assert len(intents) == 1 and intents[0].action == "sell"
    events = [f.event for f in eng.fills.records]
    assert events[-3:] == ["sell", "intent", "ack"]
    assert [e for e in events if e in {"buy", "sell", "ack"}][-2:] == ["sell", "ack"]
    assert eng.open_positions == []


def test_tick_does_not_place_an_unpriced_exit(tmp_path):
    eng = book()
    cutoff = et_ts(FRIDAY, 15, 50)
    listener = listener_for(eng, TapeTransport([]), StaticQuotes({}), tmp_path, cutoff)
    assert listener.tick(cutoff) == []
    assert listener.broker.acks == []                       # type: ignore[attr-defined]
    assert len(eng.open_positions) == 1
    assert eng.fills.events("alert")[-1].reason.startswith("HALT_AND_PAGE")


def test_tick_also_fires_the_breakeven_stop(tmp_path):
    """A stop must not need a Discord post to fire either."""
    eng = book(enforce_breakeven=True)
    eng.positions[QQQ].breakeven_stop = 1.50
    now = et_ts(FRIDAY, 11, 0)
    listener = listener_for(eng, TapeTransport([]), StaticQuotes({QQQ: (1.40, 1.45)}),
                            tmp_path, now)
    placed = listener.tick(now)
    assert [i.action for i in placed] == ["sell"]
    assert eng.open_positions == []


class _SilentTransport:
    """A connected Gateway that never speaks - the 15:40 ET case."""

    def __init__(self) -> None:
        self.pumped = 0

    async def messages(self) -> AsyncIterator[RawMessage]:
        await asyncio.sleep(3600)
        yield RawMessage(channel=ET_CH, channel_id=ET_ID, text="never sent")


def test_the_cutoff_fires_on_a_tick_with_no_discord_traffic(tmp_path):
    eng = book()
    cutoff = et_ts(FRIDAY, 15, 45)
    listener = listener_for(eng, _SilentTransport(), StaticQuotes({QQQ: (2.00, 2.10)}),
                            tmp_path, cutoff)
    listener.tick_s = 0.01
    seen: list[str] = []

    async def run() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(pump_listener(listener, out=seen.append), 0.25)

    asyncio.run(run())
    assert eng.open_positions == [], "the guard must not need a post to fire"
    assert [f.event for f in eng.fills.events("sell")] == ["sell"]
    assert any("tick" in line for line in seen)


class _SlowTransport:
    """One message arrives 60 ms in, another 60 ms later, with ticks in between."""

    def __init__(self) -> None:
        self.sent = 0

    async def messages(self) -> AsyncIterator[RawMessage]:
        for text in ("CALL TICKER QQQ STRIKE $700 Exp 0DTE Current BID Price $1.00",
                     "QQQ calls closed at 2.00"):
            self.sent += 1
            await asyncio.sleep(0.06)
            yield raw(text, 1000.0 + self.sent)


def test_a_tick_does_not_eat_the_message_it_interrupted(tmp_path):
    """`wait_for` alone cancels anext, which CLOSES the generator and kills the feed."""
    eng = engine()
    transport = _SlowTransport()
    listener = listener_for(eng, transport, StaticQuotes({QQQ: (1.00, 1.05)}),
                            tmp_path, 1000.0)
    listener.tick_s = 0.01
    handled = asyncio.run(pump_listener(listener, out=lambda _line: None))
    assert handled == 2, "both posts must survive the ticks between them"
    assert listener.processed == 2
    assert eng.fills.events("sell")


def test_the_message_limit_is_not_consumed_by_ticks(tmp_path):
    eng = engine()
    listener = listener_for(eng, _SlowTransport(), StaticQuotes({QQQ: (1.00, 1.05)}),
                            tmp_path, 1000.0)
    listener.tick_s = 0.01
    assert asyncio.run(pump_listener(listener, limit=1, out=lambda _line: None)) == 1


# ── the operator surface ─────────────────────────────────────────────────────
def test_arm_enables_the_guard_by_default_and_offers_an_opt_out():
    from scorpion_mff.cli import _parser

    default = _parser().parse_args(["arm"])
    assert default.no_expiry_guard is False, "armed means guarded"
    assert (default.tick_s, default.expiry_cutoff) == (15.0, 15)
    assert _parser().parse_args(["arm", "--no-expiry-guard"]).no_expiry_guard is True


def test_the_expiry_audit_reports_the_corpus_and_refuses_assignment():
    audit = expiry_audit()
    same_day = audit["same_day"]
    unclosed = audit["no_explicit_close"]
    cutoff = audit["cutoff"]
    assignment = audit["assignment"]
    assert isinstance(same_day, dict) and isinstance(unclosed, dict)
    assert isinstance(cutoff, dict) and isinstance(assignment, dict)
    assert audit["alerts"] == 93
    assert (same_day["count"], same_day["of"]) == (40, 93)
    assert unclosed["outcome_floor"] == 61, "57 win_partial + 4 unknown"
    assert unclosed["count"] >= unclosed["outcome_floor"]
    assert unclosed["consistent_with_floor"] is True
    assert cutoff["force_close"] == 0 and cutoff["no_exit_time"] == 10
    assert cutoff["cutoff_et"] == ["15:45"]
    assert assignment["status"] == "refused" and assignment["shares"] is None
    assert "no underlying close" in str(assignment["reason"])


def test_the_expiry_audit_command_prints_and_exits_zero(capsys):
    assert main(["expiry", "audit"]) == 0
    text = capsys.readouterr().out
    assert "same-day expiry" in text and "REFUSED" in text
    assert main(["expiry", "audit", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["assignment"]["status"] == "refused"


# ── the corpus: unchanged, and never silent ──────────────────────────────────
def _replay_corpus(enforce_expiry: bool) -> tuple[int, int, float, Engine]:
    """Drive the corpus exactly as `historic.run_backtest` does, with the flag set."""
    trades = historic.load_trades()
    eng = Engine(config=EngineConfig(enforce_expiry=enforce_expiry), fills=FillLog(),
                 quotes=historic.QuoteScript(historic.build_quotes(trades, 1.05)))
    taken = contracts = 0
    pnl = 0.0
    for trade in trades:
        intents = eng.on_signal(parse(historic.entry_message(trade)), now=trade.entry_ts)
        if not intents:
            continue
        taken += 1
        contracts += intents[0].qty
        exit_msg = historic.exit_message(trade)
        if exit_msg is None:
            continue
        sells = eng.on_signal(parse(exit_msg), now=trade.exit_ts)
        if sells:
            pnl += (sells[0].limit - intents[0].limit) * sells[0].qty * 100
    return taken, contracts, round(pnl, 2), eng


def test_the_corpus_backtest_is_unchanged_and_the_guard_does_not_touch_it():
    report = historic.run_backtest()
    assert (report.trades, report.days) == (93, 59)
    assert (report.taken, report.contracts) == (91, 405)
    assert round(report.pnl_usd, 2) == 57951.00
    assert report.skip_reasons == {"ignored:ticker_excluded": 2}
    assert _replay_corpus(enforce_expiry=False)[:3] == (91, 405, 57951.00)
    assert _replay_corpus(enforce_expiry=True)[:3] == (91, 405, 57951.00), \
        "arming the flag must not change a historical replay"


def test_every_slot_the_corpus_released_has_an_alert_on_record():
    _taken, _contracts, _pnl, eng = _replay_corpus(enforce_expiry=True)
    released = eng.fills.events("expire")
    unresolved = [f for f in eng.fills.events("alert")
                  if f.reason.startswith("expiry_unresolved")]
    assert released, "the corpus must still contain the unresolved-exit case"
    assert len(unresolved) == len(released)
    assert {f.contract_key for f in unresolved} == {f.contract_key for f in released}
    assert all(f.reason.endswith("assignment=unknown") for f in unresolved)
