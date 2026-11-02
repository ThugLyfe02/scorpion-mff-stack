"""exec-core-v1: the 11 deterministic MASTER_SPEC tests plus parser/rules/session coverage.

No network, no sleeps, injected clocks. Fixtures are synthetic.
"""

from __future__ import annotations

import ast
import asyncio
import csv
import io
import json
import logging
import os
import sys
import time
import types
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scorpion import exec_cli
from scorpion.alerts import Page, Pager, pagerduty_payload
from scorpion.broker import ExecutionMode, Quote, StaleQuote
from scorpion.config import (
    CHANNELS,
    EXECUTION_COMPONENT,
    FORWARD_RULES_SHA256,
    GUILD_ID,
    STALE_POLICY,
    load_forward_rules,
)
from scorpion.databento_feed import (
    API_KEY_ENV,
    CostGuardRefused,
    DatabentoFeed,
    FeedMessage,
    PullRequest,
    ReconnectSchedule,
    guarded_pull,
)
from scorpion.domain import EventKind, RawDiscordMessage
from scorpion.fail_safe_control import NoTradeSafetyLatch, SafetyMode
from scorpion.fastpath import QuoteCache
from scorpion.fill_logger import parity_diff
from scorpion.health import FeedHealthMonitor
from scorpion.parser import parse_message
from scorpion.pricing import equity_tick, guarded_entry_limit
from scorpion.session_calendar import session_hours
from scorpion.session_service import LatchMirror, PaperExecutionSession, percentile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SRC = ROOT / "src" / "scorpion"
OK = CHANNELS["options-king"]


class FakeClock:
    def __init__(self) -> None:
        self.ns = 1_000_000_000_000

    def __call__(self) -> int:
        return self.ns

    def advance_ms(self, ms: float) -> None:
        self.ns += int(ms * 1_000_000)


def _post(
    ticker: str, *, at: datetime, seen: datetime | None = None, mid: str = "1"
) -> RawDiscordMessage:
    text = (
        f"CALL / TICKER {ticker} / STRIKE $7.5 / Exp Nov 20 / Current BID Price $.45 / "
        "SL at 25% / @everyone"
    )
    return RawDiscordMessage(mid, GUILD_ID, OK, "author", text, at, seen or at)


def _armed_latch(tmp_path: Path) -> LatchMirror:
    latch = NoTradeSafetyLatch(tmp_path / "latch.db")
    latch.trip_no_trade(EXECUTION_COMPONENT, reason="init")
    latch.clear_no_trade(EXECUTION_COMPONENT, operator="test-operator", reason="arm for test")
    return LatchMirror(latch)


def _session(
    tmp_path: Path, clock: FakeClock, cache: QuoteCache | None = None
) -> PaperExecutionSession:
    return PaperExecutionSession(
        rules=load_forward_rules(),
        cache=cache or QuoteCache(mono_ns=clock),
        latch=_armed_latch(tmp_path),
        pager=Pager(routing_key=""),
        mono_ns=clock,
        now_utc=lambda: datetime(2026, 10, 29, 14, 0, tzinfo=UTC),
    )


# 1 ------------------------------------------------------------------------------------------
def test_quote_cache_per_symbol() -> None:
    clock = FakeClock()
    cache = QuoteCache(mono_ns=clock)
    symbols = [f"S{i:02d}" for i in range(50)]
    for i, symbol in enumerate(symbols):
        cache.update(
            symbol,
            bid=Decimal(10 + i),
            ask=Decimal(10 + i) + Decimal("0.01"),
            observed_ts_utc=datetime.now(UTC),
        )
    for i, symbol in enumerate(symbols):
        quote = cache.fresh(symbol, max_age_ms=STALE_POLICY.quote_max_age_ms)
        assert isinstance(quote, Quote)
        assert quote.bid == Decimal(10 + i)
    unknown = cache.fresh("NOPE", max_age_ms=STALE_POLICY.quote_max_age_ms)
    assert isinstance(unknown, StaleQuote)
    assert unknown.reason == "unknown_symbol"
    assert not hasattr(unknown, "bid")


# 2 ------------------------------------------------------------------------------------------
def test_stale_quote_skips_entry(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = QuoteCache(mono_ns=clock)
    session = _session(tmp_path, clock, cache)
    cache.update(
        "ABCD",
        bid=Decimal("7.40"),
        ask=Decimal("7.42"),
        last=Decimal("7.41"),
        observed_ts_utc=datetime.now(UTC),
    )
    clock.advance_ms(STALE_POLICY.quote_max_age_ms + 1)
    stale = cache.fresh("ABCD", max_age_ms=STALE_POLICY.quote_max_age_ms)
    assert isinstance(stale, StaleQuote)
    assert guarded_entry_limit(stale, Decimal("7.41"), max_chase_bps=150) is None
    decision = session.on_post(_post("ABCD", at=datetime(2026, 10, 29, 13, 40, tzinfo=UTC)))
    assert decision.action == "schedule_entry"
    assert session.on_entry_due(decision.call_id) is None
    assert session.orders == []
    assert session.working[decision.call_id].status == "skip:stale_or_missing_quote"


# 3 ------------------------------------------------------------------------------------------
def test_feed_silence_latches(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = QuoteCache(mono_ns=clock)
    session = _session(tmp_path, clock, cache)
    monitor = FeedHealthMonitor(cache, STALE_POLICY, started_mono_ns=clock())
    clock.advance_ms(STALE_POLICY.feed_silence_ms - 1)
    assert session.health_tick(monitor, session_open=True) == []
    assert session.latch.armed
    clock.advance_ms(2)  # total 10.001 s with no message
    reasons = session.health_tick(monitor, session_open=True)
    assert reasons and reasons[0].startswith("feed_silence_ms=")
    assert not session.latch.armed
    assert session.latch.latch.state(EXECUTION_COMPONENT).mode is SafetyMode.NO_TRADE
    clock.advance_ms(1000)
    session.health_tick(monitor, session_open=True)
    assert session.pager.enqueued == 1  # dedup: exactly one page
    assert session.pager.pending() == 1


# 4 ------------------------------------------------------------------------------------------
def test_latch_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCORPION_STATE_DIR", str(tmp_path))
    first = LatchMirror(exec_cli.open_latch())
    assert not first.armed  # uninitialized fails closed
    exec_cli.main(["latch", "clear", "--operator", "ryan", "--reason", "pre-open check"])
    assert LatchMirror(exec_cli.open_latch()).armed
    LatchMirror(exec_cli.open_latch()).trip("feed_silence")
    rebuilt = LatchMirror(exec_cli.open_latch())  # "restart": everything rebuilt from disk
    assert not rebuilt.armed
    assert rebuilt.refresh() is False
    exec_cli.main(["latch", "clear", "--operator", "ryan", "--reason", "feed restored"])
    assert LatchMirror(exec_cli.open_latch()).armed
    # the session has no clear path at all
    assert not hasattr(PaperExecutionSession, "clear_latch")
    assert "clear_no_trade" not in (SRC / "session_service.py").read_text()


# 5 ------------------------------------------------------------------------------------------
LIVE_SINK_NAMES = {
    "place_equity_order",
    "place_option_order",
    "place_advanced_order",
    "place_crypto_order",
    "replace_option_order",
    "submit_order",
    "order_buy_market",
    "order_buy_limit",
    "order_sell_limit",
    "order_buy_option_limit",
    "robin_stocks",
    "LiveBroker",
}
BANNED_TEXT = ("api.robinhood.com", ".internal/", "Mozilla/5.0", "EXTRACTION_SECRET")


def test_no_live_sink() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.alias):
                name = node.name.split(".")[0]
            if name in LIVE_SINK_NAMES:
                offenders.append(f"{path.name}:{getattr(node, 'lineno', 0)}:{name}")
        offenders.extend(f"{path.name}:{token}" for token in BANNED_TEXT if token in text)
    assert offenders == []
    assert {mode.value for mode in ExecutionMode} == {"PAPER", "REVIEW_ONLY"}
    from scorpion.config import DEFAULT_POLICY

    assert DEFAULT_POLICY.live_submission_enabled is False
    with pytest.raises(ValueError):
        PaperExecutionSession(
            rules=load_forward_rules(),
            cache=QuoteCache(),
            latch=None,  # type: ignore[arg-type]
            pager=Pager(routing_key=""),
            mode="LIVE",
        )  # type: ignore[arg-type]


# 6 ------------------------------------------------------------------------------------------
prices = st.decimals(
    min_value=Decimal("0.05"),
    max_value=Decimal("900"),
    places=4,
    allow_nan=False,
    allow_infinity=False,
)


@settings(max_examples=400, deadline=None)
@given(
    bid=prices,
    spread=st.decimals(min_value=0, max_value=5, places=4),
    ref=prices,
    cap=st.integers(min_value=0, max_value=1000),
)
def test_pricing_never_above_cap(bid: Decimal, spread: Decimal, ref: Decimal, cap: int) -> None:
    quote = Quote(bid, bid + spread, datetime.now(UTC), recv_mono_ns=0)
    limit = guarded_entry_limit(quote, ref, max_chase_bps=cap)
    if limit is None:
        return
    assert limit <= quote.ask
    assert limit <= ref * (1 + Decimal(cap) / Decimal(10_000))
    assert limit % equity_tick(limit) == 0 or limit % Decimal("0.0001") == 0
    if limit >= 1:
        assert limit == limit.quantize(Decimal("0.01"))


# 7 ------------------------------------------------------------------------------------------
class DroppingClient:
    def __init__(self, drops: int) -> None:
        self.drops = drops
        self.snapshots: list[bool] = []

    def add_symbols(self, symbols: Sequence[str]) -> None:
        return None

    async def stream(self, symbols: Sequence[str], *, snapshot: bool) -> AsyncIterator[FeedMessage]:
        self.snapshots.append(snapshot)
        if False:  # pragma: no cover - makes this an async generator
            yield FeedMessage("X", None, None, None, 0)
        raise ConnectionError("gateway reset")


def test_reconnect_backoff() -> None:
    import random

    schedule = ReconnectSchedule()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    client = DroppingClient(5)
    feed = DatabentoFeed(
        client,
        QuoteCache(),
        ["QQQ"],
        schedule=schedule,
        rng=random.Random(7),
        sleep=fake_sleep,
        max_disconnects=5,
    )
    asyncio.run(feed.run())
    assert feed.stats.disconnects == 5
    assert client.snapshots == [False, True, True, True, True]  # snapshot on every reconnect
    assert len(slept) == 4
    for attempt, delay in enumerate(slept):
        low, high = schedule.bounds(attempt)
        assert low <= delay <= high <= schedule.cap_s
    assert schedule.bounds(20) == (15.0, 30.0)
    report = feed.integrity_report()
    assert report.provider_gap_events == 5
    assert not report.certified
    assert "provider_gap_events:5>0" in report.failures


# 8 ------------------------------------------------------------------------------------------
def test_replay_golden() -> None:
    produced = exec_cli.replay_csv(FIXTURES / "replay_fixture.json")
    assert produced.encode("utf-8") == (FIXTURES / "replay_golden.csv").read_bytes()
    ours = list(csv.DictReader(io.StringIO(produced)))
    frozen = list(csv.DictReader((FIXTURES / "replay_frozen_v21.csv").open(encoding="utf-8")))
    assert parity_diff(ours, frozen) == []  # cell-for-cell with the frozen research logger
    slip_rows = [r for r in ours if r["slip_vs_intended_pct"]]
    assert slip_rows and any(r["actual_fill_src"] == "supplied" for r in slip_rows)
    tracks = {r["track"] for r in ours}
    assert tracks == {"main", "shadow_post1100"}
    assert all(r["traded"] == "0" for r in ours if r["track"] == "shadow_post1100")
    one_day = exec_cli.replay_csv(FIXTURES / "replay_fixture.json", only_date="2026-10-30")
    assert all(r["date"] == "2026-10-30" for r in csv.DictReader(io.StringIO(one_day)))


# 9 ------------------------------------------------------------------------------------------
SENTINEL = "db-SENTINEL-not-a-real-key-7f3a"


def test_secrets_not_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(API_KEY_ENV, SENTINEL)
    caplog.set_level(logging.DEBUG)
    seen_keys: list[str] = []

    class LeakyMetadata:
        def get_cost(self, **_: object) -> float:
            raise RuntimeError(f"401 for key {SENTINEL}")

        def list_datasets(self) -> list[str]:
            raise RuntimeError(f"auth failed key={SENTINEL}")

    class FakeHistorical:
        def __init__(self, key: str) -> None:
            seen_keys.append(key)
            self.metadata = LeakyMetadata()

    fake = types.ModuleType("databento")
    fake.Historical = FakeHistorical  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "databento", fake)

    with pytest.raises(RuntimeError) as excinfo:
        guarded_pull(FakeHistorical(SENTINEL), PullRequest("XNAS.ITCH", "trades", ("AAA",), "a", "b"))
    assert SENTINEL not in str(excinfo.value)
    result = exec_cli._databento_auth_check(timeout_s=1.0)
    assert not result.ok and SENTINEL not in result.detail

    class LeakyClient(DroppingClient):
        async def stream(
            self, symbols: Sequence[str], *, snapshot: bool
        ) -> AsyncIterator[FeedMessage]:
            if False:  # pragma: no cover
                yield FeedMessage("X", None, None, None, 0)
            raise ConnectionError(f"bad key {SENTINEL}")

    async def no_sleep(_: float) -> None:
        return None

    asyncio.run(
        DatabentoFeed(LeakyClient(1), QuoteCache(), ["QQQ"], sleep=no_sleep, max_disconnects=2).run()
    )
    exec_cli._print({"check": result.detail})
    captured = capsys.readouterr()
    assert seen_keys and all(key == SENTINEL for key in seen_keys)  # key reaches the client only
    for text in (caplog.text, captured.out, captured.err):
        assert SENTINEL not in text


# 10 -----------------------------------------------------------------------------------------
class PricedClient:
    def __init__(self, cost: float) -> None:
        self.cost = cost
        self.pulled = 0
        outer = self

        class Metadata:
            def get_cost(self, **_: object) -> float:
                return outer.cost

        class Timeseries:
            def get_range(self, **_: object) -> object:
                outer.pulled += 1
                return object()

        self.metadata = Metadata()
        self.timeseries = Timeseries()


def test_cost_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    request = PullRequest("XNAS.ITCH", "ohlcv-1m", ("AAA",), "2026-10-30", "2026-10-31")
    client = PricedClient(31.0)
    with pytest.raises(CostGuardRefused):
        guarded_pull(client, request)
    assert client.pulled == 0
    estimate = guarded_pull(client, request, estimate_only=True)
    assert estimate.estimated_cost_usd == Decimal("31.0") and not estimate.executed
    assert client.pulled == 0
    assert guarded_pull(client, request, approve_cost=True).executed and client.pulled == 1
    cheap = PricedClient(0.42)
    assert guarded_pull(cheap, request).executed and cheap.pulled == 1
    import scorpion.databento_feed as feed_module

    monkeypatch.setattr(feed_module, "historical_client", lambda: PricedClient(31.0))
    argv = [
        "pull",
        "--dataset",
        "XNAS.ITCH",
        "--schema",
        "ohlcv-1m",
        "--symbols",
        "AAA",
        "--start",
        "2026-10-30",
        "--end",
        "2026-10-31",
    ]
    assert exec_cli.main(argv) == 3


# 11 -----------------------------------------------------------------------------------------
def test_decision_path_perf_smoke(tmp_path: Path) -> None:
    """Informational: decision-path p99 on 10k synthetic events, written as a CI artifact."""

    clock = FakeClock()
    cache = QuoteCache(mono_ns=clock)
    session = _session(tmp_path, clock, cache)
    base = datetime(2026, 10, 29, 13, 35, tzinfo=UTC)
    tickers = [f"T{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(200)]
    for ticker in tickers:
        cache.update(
            ticker,
            bid=Decimal("7.40"),
            ask=Decimal("7.42"),
            last=Decimal("7.41"),
            observed_ts_utc=datetime.now(UTC),
        )
    totals: list[int] = []
    for i in range(10_000):
        raw = _post(tickers[i % 200], at=base + timedelta(milliseconds=i), mid=str(i))
        started = time.perf_counter_ns()
        decision = session.on_post(raw)
        session.on_entry_due(decision.call_id)
        totals.append(time.perf_counter_ns() - started)
        session.working.pop(decision.call_id, None)
    ordered = sorted(totals)
    report = {
        "events": len(ordered),
        "p50_ms": percentile(ordered, 50) / 1e6,
        "p99_ms": percentile(ordered, 99) / 1e6,
        "intents": len(session.orders),
    }
    out = Path(os.environ.get("SCORPION_PERF_ARTIFACT_DIR", tmp_path)) / "decision_path_perf.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    assert report["intents"] == 10_000
    assert report["p99_ms"] < 50.0  # spec bound (milliseconds, not microseconds)


# --------------------------------------------------------------------------- extra coverage
def test_rules_lock_is_single_source_and_hashed() -> None:
    rules = load_forward_rules()
    assert rules.sha256 == FORWARD_RULES_SHA256
    assert rules.hold_min == 60 and rules.limit_window_min == 3
    assert rules.skip_if_seen_after_min == 15 and rules.cost_bps_round_trip == 10
    assert {"TSLA", "NVDA"} <= rules.excluded_tickers
    for module in ("execution_model.py", "session_service.py", "fill_logger.py"):
        text = (SRC / module).read_text()
        assert "TSLA" not in text and "NVDA" not in text


def test_rules_lock_rejects_a_stop(tmp_path: Path) -> None:
    text = (ROOT / "live" / "RULES.lock.md").read_text().replace('"stop": "none"', '"stop": "-25%"')
    lock = tmp_path / "RULES.lock.md"
    lock.write_text(text)
    with pytest.raises(ValueError, match="forbid a stop"):
        load_forward_rules(lock)


def test_options_king_template_parses_as_entry() -> None:
    at = datetime(2026, 10, 28, 18, 22, tzinfo=UTC)
    cases = {
        "CALL / TICKER ABCD / STRIKE $140 / Exp Oct 30 / Current BID Price $2.70 / SL at 25% /": (
            "ABCD",
            "CALL",
            Decimal("140"),
            date(2026, 10, 30),
            Decimal("2.70"),
        ),
        "PUT TICKER WXYZ STRIKE $162.5 Exp Jan 9 2027 Current BID Price! $.85 SL at 25%": (
            "WXYZ",
            "PUT",
            Decimal("162.5"),
            date(2027, 1, 9),
            Decimal(".85"),
        ),
        "CALL/ swing could pay great TICKER QRS STRIKE 46 Exp Nov 6 Current BID Price 1.19 SL 25%": (
            "QRS",
            "CALL",
            Decimal("46"),
            date(2026, 11, 6),
            Decimal("1.19"),
        ),
    }
    for text, (ticker, side, strike, expiry, price) in cases.items():
        event = parse_message(RawDiscordMessage("1", GUILD_ID, OK, "a", text, at, at))
        assert event.kind is EventKind.ENTRY, (text, event.reason)
        assert (event.ticker, event.option_side, event.strike, event.expiry) == (
            ticker,
            side,
            strike,
            expiry,
        )
        assert event.referenced_price == price  # never the "SL at 25" stop
    negated = parse_message(
        RawDiscordMessage(
            "1", GUILD_ID, OK, "a", "Do not buy CALL TICKER ABCD STRIKE $1 Exp Nov 6 Price $1", at, at
        )
    )
    assert negated.kind is EventKind.AMBIGUOUS


def test_post_after_1100_is_shadow_only_and_late_is_skipped(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = QuoteCache(mono_ns=clock)
    session = _session(tmp_path, clock, cache)
    cache.update(
        "ABCD",
        bid=Decimal("7.40"),
        ask=Decimal("7.42"),
        last=Decimal("7.41"),
        observed_ts_utc=datetime.now(UTC),
    )
    shadow = session.on_post(_post("ABCD", at=datetime(2026, 10, 29, 15, 5, tzinfo=UTC)))
    assert shadow.action == "shadow_log_only" and shadow.track == "shadow_post1100"
    assert session.on_entry_due(shadow.call_id) is None and session.orders == []
    at = datetime(2026, 10, 29, 13, 40, tzinfo=UTC)
    late = session.on_post(_post("ABCD", at=at, seen=at + timedelta(minutes=16), mid="2"))
    assert late.action == "skip:late_gt15min"
    main = session.on_post(_post("ABCD", at=at, mid="3"))
    assert main.action == "schedule_entry"
    result = session.on_entry_due(main.call_id)
    assert result is not None and result.mode is ExecutionMode.PAPER
    assert result.fill_price == Decimal("7.41")  # first limit rests at the current price
    chase = session.on_entry_due(main.call_id, chase=True)
    assert chase is not None and chase.fill_price == Decimal("7.42")  # chase to the ask, capped


def test_alert_payload_schema() -> None:
    body = pagerduty_payload(Page("k", "summary"), "rk")
    assert body["event_action"] == "trigger" and body["routing_key"] == "rk"
    assert body["payload"] == {
        "summary": "summary",
        "source": "scorpion-paper",
        "severity": "critical",
    }


def test_pager_failure_never_raises() -> None:
    async def failing(_: dict[str, object]) -> int:
        raise TimeoutError

    async def no_sleep(_: float) -> None:
        return None

    pager = Pager(routing_key="rk", transport=failing, sleep=no_sleep)
    assert pager.page(Page("k1", "s")) and not pager.page(Page("k1", "s"))
    asyncio.run(pager.drain_once())
    assert pager.failed == ["k1"] and pager.sent == []


def test_xnys_calendar() -> None:
    assert session_hours(date(2026, 11, 26)) is None  # Thanksgiving
    assert session_hours(date(2026, 11, 1)) is None  # Sunday
    hours = session_hours(date(2026, 11, 27))
    assert hours is not None and hours[1].hour == 13  # early close
    regular = session_hours(date(2026, 11, 2))
    assert regular is not None and (regular[0].hour, regular[0].minute) == (9, 30)


def test_preflight_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scorpion.exec_cli import CheckResult, preflight_checks

    monkeypatch.setenv("SCORPION_STATE_DIR", str(tmp_path))
    ok_feed = lambda: CheckResult("databento_auth", True, "fake")  # noqa: E731
    results, market_open = preflight_checks(
        today=date(2026, 11, 2), feed_check=ok_feed, environ={"SCORPION_BROKER_MODE": "PAPER"}
    )
    by_name = {r.name: r for r in results}
    assert market_open and not by_name["latch"].ok  # uninitialized latch keeps the unit down
    assert by_name["rules_lock"].ok and by_name["golden_replay"].ok
    exec_cli.main(["latch", "clear", "--operator", "ryan", "--reason", "test"])
    results, _ = preflight_checks(
        today=date(2026, 11, 2), feed_check=ok_feed, environ={"SCORPION_BROKER_MODE": "LIVE"}
    )
    by_name = {r.name: r for r in results}
    assert by_name["latch"].ok and not by_name["broker_mode"].ok
    closed, open_flag = preflight_checks(today=date(2026, 11, 26), feed_check=ok_feed)
    assert not open_flag and all(r.ok for r in closed)
