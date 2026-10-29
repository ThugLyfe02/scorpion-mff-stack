"""Alert-to-fill drift: each hop measured, and every suggestion the sample cannot support.

The failure this guards is not a wrong arithmetic result. It is a *policy chosen before
the distribution exists* - a latency ceiling picked because it sounded fast, or a
correlation quoted from five fills. So the tests are in two families:

* the seam: the listener must stamp the intent at the send instant and the terminal at
  the answer instant, with the round trip in `latency_ms`. Without that, the measurement
  is impossible rather than merely unbuilt (the ack used to reuse the send timestamp);
* the refusal: under `MIN_SAMPLES` timed/priced orders, the report prints the numbers it
  has and withholds every suggestion, naming the floor. A p99 over three samples is the
  maximum in a percentile's clothing.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from scorpion_mff import cli, drift
from scorpion_mff.broker import Ack, QuoteScript
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.listener import Listener, TapeTransport
from scorpion_mff.types import OrderIntent, RawMessage

T0 = 1_757_340_000.0
QQQ = "QQQ|CALL|719|0DTE"
KEY = f"{QQQ}:buy:1757340000123"


def row(
    log: FillLog, *, event: str, ts: float, contract_key: str = QQQ, qty: int = 1,
    price: float = 1.05, reason: str = "", note: str = KEY,
    latency_ms: int | None = None, accepted: bool = True,
) -> None:
    log.record(event=event, contract_key=contract_key, qty=qty, price=price,
               reason=reason, ts=ts, note=note, latency_ms=latency_ms,
               accepted=accepted)


def one_order(log: FillLog, *, decision: float = T0, send_delay: float = 0.002,
              rtt: float = 0.180, fill: float = 1.06,
              reason: str = "entry:pipe:his_bid=1.01:ask=1.05") -> None:
    """The four rows one order contributes, with a known shape."""
    row(log, event="buy", ts=decision, reason=reason)
    row(log, event="intent", ts=decision + send_delay, price=1.05, reason=reason)
    row(log, event="ack", ts=decision + send_delay + rtt, qty=1, price=fill,
        reason=reason, latency_ms=int(rtt * 1000))


# ── the seam: an answer has its own instant ──────────────────────────────────
class TickingClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlowBroker:
    """A broker that takes time, so the two instants are provably different."""

    def __init__(self, clock: TickingClock, delay_s: float = 0.25) -> None:
        self.clock = clock
        self.delay_s = delay_s
        self.calls = 0

    def place(self, intent: OrderIntent) -> Ack:
        self.calls += 1
        self.clock.advance(self.delay_s)
        return Ack(ok=True, contract_key=intent.contract_key, qty=intent.qty,
                   price=intent.limit, broker_order_id="slow-1")


def test_the_listener_stamps_the_answer_instant_not_the_send_instant(tmp_path):
    clock = TickingClock()
    fills = FillLog()
    listener = Listener(
        transport=TapeTransport([]),
        engine=Engine(config=EngineConfig(), fills=fills, clock=clock),
        broker=SlowBroker(clock),
        quotes=QuoteScript({QQQ: [(1.01, 1.05)]}),
        budget=LatencyBudget(),
        heartbeat=Heartbeat(path=tmp_path / "hb.json", clock=clock),
        kill=KillSwitch(path=tmp_path / "NO_TRADE", clock=clock),
        clock=clock,
    )
    listener.handle(RawMessage(channel="etf-options", channel_id="1231301953972207667",
                               ts=T0,
                               text="CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.01"))

    intent = next(f for f in fills.records if f.event == "intent")
    ack = next(f for f in fills.records if f.event == "ack")
    assert ack.ts - intent.ts == pytest.approx(0.25)
    assert ack.latency_ms == 250
    assert intent.latency_ms is None            # the send has no round trip yet

    timing = drift.build(fills.records)[0]
    assert timing.round_trip_ms == 250
    assert timing.total_ms is not None and timing.total_ms >= 250


def test_a_fast_answer_and_a_slow_answer_are_different_rows(tmp_path):
    fast, slow = FillLog(), FillLog()
    one_order(fast, rtt=0.040)
    one_order(slow, rtt=3.500)
    assert drift.build(fast.records)[0].round_trip_ms == 40
    assert drift.build(slow.records)[0].round_trip_ms == 3500


# ── the join ─────────────────────────────────────────────────────────────────
def test_one_order_joins_decision_send_answer_and_price():
    log = FillLog()
    one_order(log)
    timing = drift.build(log.records)[0]

    assert timing.action == "buy"
    assert timing.outcome == "ack"
    assert timing.decision_ms == 2
    assert timing.round_trip_ms == 180
    assert timing.total_ms == 182
    assert timing.fill_price == 1.06
    assert timing.his_bid == 1.01 and timing.ask_at_entry == 1.05
    # paid a cent above our limit: adverse, positive
    assert timing.slippage_bps == pytest.approx(95.24, abs=0.01)
    # and about 495bps over the bid the post showed
    assert timing.fill_vs_bid_bps == pytest.approx(495.05, abs=0.01)
    # the limit itself was ~396bps through his bid (the +15% rule, measured)
    assert timing.through_bid_bps == pytest.approx(396.04, abs=0.01)


def test_a_sell_pays_the_same_sign_for_an_adverse_fill():
    log = FillLog()
    key = f"{QQQ}:close:1"
    row(log, event="sell", ts=T0, price=1.11, reason="close:pct=10%", note=key)
    row(log, event="intent", ts=T0 + 0.002, price=1.11, reason="close:pct=10%", note=key)
    row(log, event="ack", ts=T0 + 0.202, price=1.08, reason="close:pct=10%", note=key,
        latency_ms=200)
    timing = drift.build(log.records)[0]
    assert timing.action == "sell"
    assert timing.slippage_bps == pytest.approx((1.11 - 1.08) / 1.11 * 10_000, abs=0.01)
    assert timing.slippage_bps > 0


def test_an_unanswered_intent_is_unresolved_not_fast():
    log = FillLog()
    row(log, event="buy", ts=T0, reason="entry:pipe")
    row(log, event="intent", ts=T0 + 0.002, reason="entry:pipe")
    result = drift.report(log.records)

    assert result.orders == 1
    assert result.unresolved and result.unresolved[0].key == KEY
    timing = result.unresolved[0]
    assert timing.outcome == "unresolved"
    assert timing.round_trip_ms is None and timing.total_ms is None
    assert any("no venue answer" in reason for reason in result.withheld())


def test_a_breaker_refusal_is_local_and_has_no_round_trip():
    log = FillLog()
    row(log, event="buy", ts=T0, reason="entry:pipe")
    row(log, event="intent", ts=T0 + 0.002, reason="entry:pipe")
    row(log, event="reject", ts=T0 + 0.002, reason="breaker_open", note=KEY,
        latency_ms=0, accepted=False)
    timing = drift.build(log.records)[0]

    assert timing.outcome == "breaker"
    assert timing.answered_ts is None
    assert timing.round_trip_ms is None
    assert not timing.priced


def test_a_business_reject_is_an_answer_with_no_price():
    log = FillLog()
    row(log, event="buy", ts=T0, reason="entry:pipe")
    row(log, event="intent", ts=T0 + 0.002, reason="entry:pipe")
    row(log, event="reject", ts=T0 + 0.120, reason="broker:no_buying_power",
        note=KEY, latency_ms=118, accepted=False)
    timing = drift.build(log.records)[0]

    assert timing.outcome == "reject"
    assert timing.round_trip_ms == 118
    assert not timing.priced
    assert timing.slippage_bps is None


def test_an_intent_without_a_decision_row_is_named_not_zeroed():
    log = FillLog()
    row(log, event="intent", ts=T0, reason="entry:pipe")
    row(log, event="ack", ts=T0 + 0.150, price=1.05, reason="entry:pipe", latency_ms=150)
    result = drift.report(log.records)
    timing = result.timings[0]

    assert timing.decision_ts is None
    assert timing.decision_ms is None
    assert timing.total_ms is None
    assert timing.round_trip_ms == 150         # the venue hop is still measurable
    assert any("no decision row" in reason for reason in result.withheld())


def test_entry_prices_are_optional_not_invented():
    log = FillLog()
    one_order(log, reason="add:standard:1/1")
    timing = drift.build(log.records)[0]
    assert timing.his_bid is None and timing.ask_at_entry is None
    assert timing.through_bid_bps is None
    assert timing.fill_vs_bid_bps is None


# ── the refusal floor ────────────────────────────────────────────────────────
def test_under_the_floor_every_suggestion_is_withheld():
    log = FillLog()
    for index in range(3):
        key = f"{QQQ}:buy:{index}"
        base = T0 + index
        row(log, event="buy", ts=base, reason="entry:pipe:his_bid=1.01:ask=1.05")
        row(log, event="intent", ts=base + 0.002, reason="entry:pipe:his_bid=1.01:ask=1.05",
            note=key)
        row(log, event="ack", ts=base + 0.202, price=1.06,
            reason="entry:pipe:his_bid=1.01:ask=1.05", note=key, latency_ms=200)

    result = drift.report(log.records, floor=20)
    assert len(result.priced) == 3
    assert result.suggest_latency_budget_ms() is None
    assert result.latency_to_edge() is None
    reasons = " ".join(result.withheld())
    assert "floor 20" in reasons and "priced order(s)" in reasons


def test_at_the_floor_a_budget_is_suggested_from_the_observed_p99():
    log = FillLog()
    for index in range(20):
        base = T0 + index
        key = f"{QQQ}:buy:{index}"
        rtt = 0.100 + index * 0.010          # 100ms .. 290ms
        row(log, event="buy", ts=base, reason="entry:pipe:his_bid=1.01:ask=1.05")
        row(log, event="intent", ts=base + 0.002,
            reason="entry:pipe:his_bid=1.01:ask=1.05", note=key)
        row(log, event="ack", ts=base + 0.002 + rtt, price=1.05,
            reason="entry:pipe:his_bid=1.01:ask=1.05", note=key, latency_ms=int(rtt * 1000))

    result = drift.report(log.records, floor=20)
    budget = result.suggest_latency_budget_ms()
    assert budget is not None
    p99_total = result.total["p99"]
    assert budget == math.ceil(float(p99_total) / 250.0) * 250
    assert result.total["n"] == 20


def test_latency_to_edge_needs_priced_and_timed_orders_together():
    log = FillLog()
    for index in range(20):
        base = T0 + index
        key = f"{QQQ}:buy:{index}"
        rtt = 0.050 + index * 0.020
        fill = 1.05 + index * 0.001          # slower orders paid more
        row(log, event="buy", ts=base, reason="entry:pipe:his_bid=1.00:ask=1.04")
        row(log, event="intent", ts=base + 0.002,
            reason="entry:pipe:his_bid=1.00:ask=1.04", note=key)
        row(log, event="ack", ts=base + 0.002 + rtt, price=fill,
            reason="entry:pipe:his_bid=1.00:ask=1.04", note=key, latency_ms=int(rtt * 1000))

    result = drift.report(log.records, floor=20)
    edge = result.latency_to_edge()
    assert edge is not None
    assert edge["n"] == 20
    assert edge["spearman_ms_vs_paid"] == pytest.approx(1.0, abs=1e-9)
    assert edge["fast_median_paid_bps"] < edge["slow_median_paid_bps"]
    assert "not a causal slope" in edge["claim"]


def test_a_rank_correlation_is_undefined_when_one_side_never_moves():
    assert drift._spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert drift._spearman([1.0, 2.0], [1.0, 2.0]) is None       # too few samples
    assert drift._spearman([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_the_report_counts_every_outcome_class():
    log = FillLog()
    one_order(log)
    row(log, event="intent", ts=T0 + 10, note=f"{QQQ}:buy:2", reason="entry:pipe")
    row(log, event="reject", ts=T0 + 10.1, note=f"{QQQ}:buy:2", reason="broker:no",
        accepted=False)
    result = drift.report(log.records)

    counts = result.to_dict()["counts"]
    assert counts["ack"] == 1
    assert counts["reject"] == 1
    assert counts["unresolved"] == 0


# ── the CLI ──────────────────────────────────────────────────────────────────
def write_log(tmp_path: Path, orders: int) -> Path:
    path = tmp_path / "fills.jsonl"
    log = FillLog(path)
    for index in range(orders):
        base = T0 + index
        key = f"{QQQ}:buy:{index}"
        row(log, event="buy", ts=base, reason="entry:pipe:his_bid=1.01:ask=1.05")
        row(log, event="intent", ts=base + 0.002,
            reason="entry:pipe:his_bid=1.01:ask=1.05", note=key)
        row(log, event="ack", ts=base + 0.202, price=1.06,
            reason="entry:pipe:his_bid=1.01:ask=1.05", note=key, latency_ms=200)
    return path


def test_cli_drift_refuses_under_the_floor_and_exits_zero(tmp_path, capsys):
    path = write_log(tmp_path, 3)
    code = cli.main(["drift", "--fills", str(path)])
    out = capsys.readouterr().out

    assert code == 0
    assert "REFUSED" in out
    assert "floor 20" in out
    assert "send -> answer" in out


def test_cli_drift_json_carries_the_refusals(tmp_path, capsys):
    path = write_log(tmp_path, 3)
    code = cli.main(["drift", "--fills", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["orders"] == 3
    assert payload["suggested_latency_budget_ms"] is None
    assert payload["latency_to_edge"] is None
    assert payload["withheld"]
    assert payload["source"] == str(path)


def test_cli_drift_with_no_log_is_a_fact_not_an_error(tmp_path, capsys):
    code = cli.main(["drift", "--fills", str(tmp_path / "missing.jsonl")])
    out = capsys.readouterr().out
    assert code == 0
    assert "orders=0" in out
    assert "(looked in" in out


def test_cli_drift_refuses_an_impossible_floor(tmp_path, capsys):
    path = write_log(tmp_path, 1)
    code = cli.main(["drift", "--fills", str(path), "--floor", "0"])
    assert code == 2
    assert "invalid drift request" in capsys.readouterr().err


def test_a_percentile_of_one_sample_is_that_sample():
    assert drift._percentile([7.0], 0.99) == 7.0
