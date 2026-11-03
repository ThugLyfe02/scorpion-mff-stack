"""Quote staleness and fill-log integrity: the two fail-closed seams a money path
must close even when nothing is wrong.

A missing quote already refuses an entry (`no_quote`). These tests pin the two
failures that are easier to miss: a quote that exists but is too old to trade on,
and a fill log that can no longer be read back into the book it describes.
"""

from __future__ import annotations

import json
from pathlib import Path

from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.parser import parse
from scorpion_mff.types import Quote, RawMessage


def raw(text: str) -> RawMessage:
    return RawMessage(channel="etf-options", channel_id="1", text=text, ts=1000.0)


def entry() -> RawMessage:
    return raw("CALL TICKER QQQ STRIKE $719 Exp Today Current BID Price $1.01")


def engine(**cfg) -> Engine:
    return Engine(config=EngineConfig(**cfg), fills=FillLog(), clock=lambda: 1000.0)


# ── quote staleness ──────────────────────────────────────────────────────────
def test_a_stale_quote_is_refused_with_a_named_alert():
    eng = engine(max_quote_age_s=5.0)
    # clock is fixed at 1000.0; the quote is 10s older than the ceiling
    intents = eng.on_signal(parse(entry()), Quote(bid=1.05, ask=1.05, ts=990.0))
    assert intents == []
    alerts = [f for f in eng.fills.records if f.event == "alert"]
    assert alerts and alerts[0].reason.startswith("stale_quote:")
    # and the decision itself is still a named rejection, not silence
    assert any(f.reason == "no_quote" and not f.accepted for f in eng.fills.records)


def test_a_fresh_quote_is_accepted_under_the_same_ceiling():
    eng = engine(max_quote_age_s=5.0)
    intents = eng.on_signal(parse(entry()), Quote(bid=1.01, ask=1.05, ts=1000.0))
    assert len(intents) == 1


def test_no_ceiling_means_no_staleness_check():
    eng = engine()                      # max_quote_age_s is None: off, as shipped
    intents = eng.on_signal(parse(entry()), Quote(bid=1.01, ask=1.05, ts=990.0))
    assert len(intents) == 1


def test_a_stale_bid_does_not_fire_the_breakeven_stop():
    eng = engine(enforce_breakeven=True, max_quote_age_s=5.0)
    # open a position whose breakeven stop is armed at cost
    eng.on_signal(parse(entry()), Quote(bid=1.00, ask=1.00, ts=1000.0))
    position = next(iter(eng.positions.values()))
    position.breakeven_stop = position.avg_price
    # a bid through the stop, but 10s stale: the stop must NOT fire on a phantom price
    intents = eng.on_quote(position.contract_key, Quote(bid=0.50, ask=0.55, ts=990.0))
    assert intents == []
    assert position.is_open


# ── fill-log integrity ───────────────────────────────────────────────────────
def _write_log(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )


def _row(seq: int, **overrides: object) -> dict:
    base: dict[str, object] = {
        "seq": seq, "ts": 1000.0, "event": "buy", "contract_key": "QQQ|CALL|719|0DTE",
        "qty": 1, "price": 1.05, "reason": "entry", "accepted": True,
        "latency_ms": None, "note": "",
    }
    base.update(overrides)
    return base


def test_verify_file_returns_nothing_for_a_contiguous_log(tmp_path: Path):
    path = tmp_path / "fills.jsonl"
    _write_log(path, [_row(1), _row(2), _row(3)])
    assert FillLog().verify_file(path) == []


def test_verify_file_detects_a_sequence_gap(tmp_path: Path):
    path = tmp_path / "fills.jsonl"
    _write_log(path, [_row(1), _row(3)])          # seq 2 is missing: a lost decision
    problems = FillLog().verify_file(path)
    assert len(problems) == 1
    assert "seq 3 after 1" in problems[0]


def test_verify_file_detects_a_duplicate_sequence(tmp_path: Path):
    path = tmp_path / "fills.jsonl"
    _write_log(path, [_row(1), _row(1)])          # double-counted fill
    problems = FillLog().verify_file(path)
    assert any("seq 1 after 1" in p for p in problems)


def test_verify_file_detects_a_torn_last_line(tmp_path: Path):
    path = tmp_path / "fills.jsonl"
    _write_log(path, [_row(1), _row(2)])
    with open(path, "a", encoding="utf-8") as fh:  # crash mid-append
        fh.write('{"seq": 3, "ts": 1000.0, "event": "b')
    problems = FillLog().verify_file(path)
    assert any("unreadable row" in p for p in problems)
