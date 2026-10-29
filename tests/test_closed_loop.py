"""Closed-loop coverage: the one number, and the missing input behind every miss.

The properties under test are the ones that make the number worth publishing:

* an alert counts only when ALL FOUR segments pass - three is not a closed loop;
* an alert stops at the FIRST segment it cannot pass, with a stable reason code, so the report
  names an input to fetch rather than an opinion;
* a missing venue log is `no_entry_ack`, never "execute skipped"; an unrun replay is
  `no_replay`, never "replay assumed clean";
* the live lane's artifact is read in both shapes it emits and refused when it is neither;
* the corpus number is the honest one the direction document quotes (0 closed, 3 measurable).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from scorpion.bars import Bar, BarSet
from scorpion.cli import main
from scorpion.closed_loop import (
    ClosedLoopError,
    Stage,
    VenueRecord,
    assess,
    assess_alert,
    load_venue_records,
    unclosed_detail,
)

D = Decimal
DAY = date(2026, 9, 1)
TICKER, SIDE, STRIKE = "QQQ", "PUT", D("710")
CONTRACT = f"{TICKER}|{SIDE}|{STRIKE}|{DAY.isoformat()}"
REPO = Path(__file__).resolve().parents[1]


def trade(*, expiry_kind: str = "today", exit_pct=50, exit_price=None, day=DAY):
    return SimpleNamespace(
        identity=f"chan|{day}|{TICKER}|{SIDE}|{STRIKE}|09:31",
        channel="etf-options", ticker=TICKER, side=SIDE, strike=STRIKE, day=day,
        expiry_kind=expiry_kind, entry_bid=D("2.00"),
        ts_utc=datetime(day.year, day.month, day.day, 13, 31, tzinfo=UTC),
        exit_price=D(str(exit_price)) if exit_price is not None else None,
        lifecycle=SimpleNamespace(exit_pct=exit_pct),
    )


def barset(*, day=DAY, prices=(2.00, 2.10, 2.20), start_hour=13, minute_step=0) -> BarSet:
    bars = []
    for index, price in enumerate(prices):
        ts = datetime(day.year, day.month, day.day, start_hour, 31, tzinfo=UTC) + timedelta(
            minutes=minute_step * index)
        bars.append(Bar(ts_utc=ts, open=D(str(price)), high=D(str(price)),
                        low=D(str(price)), close=D(str(price))))
    return BarSet(ticker=TICKER, side=SIDE, strike=STRIKE, day=day, bars=tuple(bars))


def venue(action: str, *, day=DAY, qty: int = 1, price: float = 2.0,
          token: str = "0DTE") -> VenueRecord:
    return VenueRecord(
        contract_key=f"{TICKER}|{SIDE}|{STRIKE}|{token}", action=action, qty=qty,
        price=price, ts=datetime(day.year, day.month, day.day, 13, 36,
                                 tzinfo=UTC).timestamp(),
        order_key=f"{CONTRACT}:{action}:1", source="fill")


def replay_row(trade_row, *, status: str = "taken", reason: str = "", ok: bool = True):
    return SimpleNamespace(
        trades=[SimpleNamespace(identity=trade_row.identity, status=status, reason=reason)],
        verification={"ok": ok})


# ── the chain ────────────────────────────────────────────────────────────────
def test_all_four_segments_are_required_before_an_alert_counts_as_closed():
    row = trade()
    report = assess(SimpleNamespace(trades=[row]), {barset().contract_key: barset()},
                    venue=[venue("buy"), venue("sell")],
                    replay_result=replay_row(row))

    assert report.total == 1
    assert report.closed and report.closed[0].identity == row.identity
    assert report.coverage_pct == 100.0
    assert [stage.value for stage in report.closed[0].reached] == [
        "parse", "price", "execute", "replay"]
    assert report.blocking() == {}


def test_three_of_four_is_not_a_closed_loop():
    row = trade()
    report = assess(SimpleNamespace(trades=[row]), {barset().contract_key: barset()},
                    venue=[venue("buy"), venue("sell")], replay_result=None)

    assert report.closed == [] and report.coverage_pct == 0.0
    assert report.alerts[0].missing is Stage.REPLAY
    assert report.alerts[0].reason == "no_replay"
    assert report.reached(Stage.EXECUTE) == 1        # it got there, it did not finish


def test_an_alert_stops_at_the_first_segment_it_cannot_pass():
    row = trade()
    # no bars: the price segment stops it, and the later segments are never claimed
    report = assess(SimpleNamespace(trades=[row]), {}, venue=[venue("buy")])
    assert report.alerts[0].missing is Stage.PRICE
    assert report.alerts[0].reason == "no_bars"
    assert report.reached(Stage.PARSE) == 1
    assert report.reached(Stage.EXECUTE) == 0


def test_an_alert_with_no_venue_record_is_unmeasured_not_passed():
    row = trade()
    report = assess(SimpleNamespace(trades=[row]), {barset().contract_key: barset()}, venue=[])
    assert report.alerts[0].missing is Stage.EXECUTE
    assert report.alerts[0].reason == "no_entry_ack"
    assert "not passed" in report.to_text()


def test_an_entry_without_an_exit_ack_is_named_no_exit_ack():
    row = trade()
    report = assess(SimpleNamespace(trades=[row]), {barset().contract_key: barset()},
                    venue=[venue("buy")])
    assert report.alerts[0].reason == "no_exit_ack"
    assert "never closed" in report.alerts[0].detail


def test_a_malformed_ack_is_named_rather_than_counted_as_a_fill():
    row = trade()
    bad = VenueRecord(contract_key=f"{TICKER}|{SIDE}|{STRIKE}|0DTE", action="buy",
                      qty=0, price=0.0, ts=datetime(DAY.year, DAY.month, DAY.day, 13, 36,
                                                    tzinfo=UTC).timestamp(),
                      source="malformed")
    report = assess(SimpleNamespace(trades=[row]), {barset().contract_key: barset()},
                    venue=[bad, venue("buy"), venue("sell")])
    assert report.alerts[0].reason == "venue_record_malformed"


def test_a_replay_refusal_and_a_replay_divergence_are_separate_reasons():
    row = trade()
    refused = assess(SimpleNamespace(trades=[row]), {barset().contract_key: barset()},
                     venue=[venue("buy"), venue("sell")],
                     replay_result=replay_row(row, status="refused", reason="risk"))
    assert refused.alerts[0].reason == "replay_refused"
    assert "replay:replay_refused" in refused.blocking()

    diverged = assess(SimpleNamespace(trades=[row]), {barset().contract_key: barset()},
                      venue=[venue("buy"), venue("sell")],
                      replay_result=replay_row(row, ok=False))
    assert diverged.alerts[0].reason == "replay_divergence"

    missing = assess(SimpleNamespace(trades=[row]), {barset().contract_key: barset()},
                     venue=[venue("buy"), venue("sell")],
                     replay_result=SimpleNamespace(trades=[], verification={"ok": True}))
    assert missing.alerts[0].reason == "replay_unavailable"


# ── identity, by value not by string ────────────────────────────────────────
def test_the_parse_stage_compares_contracts_by_value_not_by_formatting():
    """The ledger writes 227.50; the parser writes 227.5. They are the same strike."""
    row = SimpleNamespace(
        identity="options-king|2026-08-13|NVDA|CALL|227.50|13:45",
        channel="options-king", ticker="NVDA", side="CALL", strike=D("227.50"),
        day=date(2026, 8, 13), expiry_kind="2026-08-17", entry_bid=D("1.99"),
        ts_utc=datetime(2026, 8, 13, 13, 45, tzinfo=UTC),
        exit_price=D("2.50"), lifecycle=SimpleNamespace(exit_pct=25),
    )
    coverage = assess_alert(row, None)
    assert coverage.reason != "parse_contract_mismatch"


def test_a_dated_expiry_is_not_compared_with_the_trade_day():
    """54 of 107 corpus contracts are dated. Comparing them with the trade day would report
    a measurement bug as a parse defect, which is the worst possible outcome."""
    row = trade(expiry_kind="2026-09-18")
    coverage = assess_alert(row, None)
    assert coverage.reason != "parse_contract_mismatch"


def test_a_same_day_venue_key_matches_the_trade_day():
    row = trade()
    coverage = assess_alert(row, barset(), venue=[venue("buy"), venue("sell")])
    assert coverage.reached[:3] == (Stage.PARSE, Stage.PRICE, Stage.EXECUTE)


def test_a_venue_record_from_a_previous_session_does_not_satisfy_today():
    row = trade()
    old = venue("buy", day=date(2026, 8, 31))
    old_sell = venue("sell", day=date(2026, 8, 31))
    coverage = assess_alert(row, barset(), venue=[old, old_sell])
    assert coverage.reason == "no_entry_ack"


# ── the live lane's artifact ────────────────────────────────────────────────
def write_jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_a_fill_log_is_read_as_confirmed_answers_only(tmp_path):
    ts = datetime(DAY.year, DAY.month, DAY.day, 13, 36, tzinfo=UTC).timestamp()
    path = write_jsonl(tmp_path / "fills.jsonl", [
        {"event": "buy", "contract_key": CONTRACT, "qty": 1, "price": 2.0, "ts": ts},
        {"event": "intent", "contract_key": CONTRACT, "qty": 1, "price": 2.0, "ts": ts,
         "note": f"{CONTRACT}:buy:1"},
        {"event": "ack", "contract_key": CONTRACT, "qty": 1, "price": 2.05, "ts": ts,
         "note": f"{CONTRACT}:buy:1"},
    ])
    records = load_venue_records(path)

    assert len(records) == 1                      # a decision is not an execution
    assert records[0].action == "buy"
    assert records[0].qty == 1 and records[0].price == 2.05
    assert records[0].day == DAY


def test_an_execution_log_gets_its_contract_key_from_the_intent(tmp_path):
    ts = datetime(DAY.year, DAY.month, DAY.day, 13, 36, tzinfo=UTC).timestamp()
    path = write_jsonl(tmp_path / "execution.jsonl", [
        {"v": 1, "t": "intent", "cid": "k1", "ts": ts, "action": "buy",
         "contract_key": CONTRACT, "qty": 1, "limit": 2.0},
        {"v": 1, "t": "ack", "cid": "k1", "ts": ts, "qty": 1, "price": 2.0},
        {"v": 1, "t": "intent", "cid": "k2", "ts": ts, "action": "sell",
         "contract_key": CONTRACT, "qty": 1, "limit": 2.2},
        {"v": 1, "t": "ack", "cid": "k2", "ts": ts, "qty": 1, "price": 2.2},
    ])
    records = load_venue_records(path)

    assert {record.action for record in records} == {"buy", "sell"}
    assert all(record.contract_key == CONTRACT for record in records)
    assert all(record.source == "execution" for record in records)


def test_an_ack_for_an_order_the_log_never_recorded_is_not_adopted(tmp_path):
    ts = datetime(DAY.year, DAY.month, DAY.day, 13, 36, tzinfo=UTC).timestamp()
    path = write_jsonl(tmp_path / "execution.jsonl", [
        {"v": 1, "t": "ack", "cid": "orphan", "ts": ts, "qty": 5, "price": 2.0},
    ])
    assert load_venue_records(path) == []


def test_a_venue_log_that_is_neither_shape_is_refused(tmp_path):
    path = write_jsonl(tmp_path / "weird.jsonl", [{"hello": "world"}])
    with pytest.raises(ClosedLoopError, match="neither a fill log"):
        load_venue_records(path)


def test_a_torn_venue_log_is_refused_with_its_line(tmp_path):
    path = tmp_path / "torn.jsonl"
    path.write_text('{"event": "ack"}\n{not json\n', encoding="utf-8")
    with pytest.raises(ClosedLoopError, match=":2 is not JSON"):
        load_venue_records(path)


def test_an_unreadable_venue_log_is_refused_not_treated_as_empty(tmp_path):
    with pytest.raises(ClosedLoopError, match="cannot read"):
        load_venue_records(tmp_path / "missing.jsonl")


def test_an_ack_with_no_usable_price_is_recorded_as_malformed(tmp_path):
    path = write_jsonl(tmp_path / "fills.jsonl", [
        {"event": "ack", "contract_key": CONTRACT, "qty": 0, "price": 0.0, "ts": 1.0},
    ])
    records = load_venue_records(path)
    assert len(records) == 1 and records[0].source == "malformed"


# ── the corpus number, and the CLI ─────────────────────────────────────────
def test_the_corpus_is_zero_closed_and_the_report_says_which_input_is_missing(capsys):
    corpus = REPO / "mff-backtest" / "trades.csv"
    bars = REPO / "mff-backtest" / "week_bars.json"
    if not corpus.exists() or not bars.exists():
        pytest.skip("the corpus or the bar file is not in this checkout")

    code = main(["closed-loop", "--corpus", str(corpus), "--bars", str(bars)])
    out = capsys.readouterr().out

    assert code == 0
    assert " 0 / 107" in out
    assert "(0.0%)" in out
    assert "execute 0" in out
    assert "no confirmed records on record" in out
    assert "not passed" in out


def test_the_corpus_report_is_machine_readable(capsys):
    corpus = REPO / "mff-backtest" / "trades.csv"
    bars = REPO / "mff-backtest" / "week_bars.json"
    if not corpus.exists() or not bars.exists():
        pytest.skip("the corpus or the bar file is not in this checkout")

    code = main(["closed-loop", "--corpus", str(corpus), "--bars", str(bars), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["alerts"] == 107
    assert payload["closed"] == 0
    assert payload["stages"]["parse"] == 105        # two are excluded tickers, by design
    assert payload["stages"]["price"] == 3          # the 3/107 measurable corpus
    assert payload["stages"]["execute"] == 0
    assert payload["venue"]["records"] == 0
    assert all(row["missing"] is not None for row in payload["unclosed"])


def test_a_malformed_venue_log_is_exit_two(capsys, tmp_path):
    corpus = REPO / "mff-backtest" / "trades.csv"
    bars = REPO / "mff-backtest" / "week_bars.json"
    if not corpus.exists() or not bars.exists():
        pytest.skip("the corpus or the bar file is not in this checkout")
    path = write_jsonl(tmp_path / "weird.jsonl", [{"hello": "world"}])

    code = main(["closed-loop", "--corpus", str(corpus), "--bars", str(bars),
                 "--venue", str(path)])
    assert code == 2
    assert "venue log refused" in capsys.readouterr().err


def test_unclosed_detail_names_alerts_by_identity_and_reason():
    rows = [assess_alert(trade(), None)]
    report = assess(SimpleNamespace(trades=rows and [trade()]), {})
    lines = list(unclosed_detail(report, limit=1))
    assert len(lines) == 1
    assert "price" in lines[0] and "no_bars" in lines[0]


def test_the_stage_order_is_the_chain_and_never_reordered():
    from scorpion.closed_loop import STAGES
    assert [stage.value for stage in STAGES] == ["parse", "price", "execute", "replay"]
