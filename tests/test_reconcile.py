"""Venue reconciliation: the engine's book vs the venue's book, named not guessed."""

from __future__ import annotations

import json

import pytest

from scorpion_mff.cli import main
from scorpion_mff.engine import Engine, EngineConfig
from scorpion_mff.fills import FillLog
from scorpion_mff.reconcile import (
    ReconcileError,
    VenuePosition,
    load_snapshot,
    reconcile,
    snapshot_from_payload,
)
from scorpion_mff.types import AlertKind, Position, Quote, Side, Signal

DAY = 1_757_000_000.0
KING = "options-king"


def pos(key: str = "QQQ|CALL|600|0DTE", contracts: int = 5) -> Position:
    ticker, side, strike, expiry = key.split("|")
    return Position(contract_key=key, ticker=ticker, side=Side(side), contracts=contracts,
                    avg_price=1.0, opened_ts=DAY, channel=KING)


def test_two_books_that_agree_are_clean():
    report = reconcile([pos()], [VenuePosition("QQQ|CALL|600|0DTE", 5)])
    assert report.clean is True
    assert report.reason_text == "clean"
    assert report.local_positions == 1 and report.venue_positions == 1
    assert report.local_contracts == report.venue_contracts == 5


def test_a_local_holding_the_venue_does_not_have_is_phantom():
    report = reconcile([pos()], [])
    assert [d.kind for d in report.drifts] == ["phantom_local"]
    assert report.drifts[0].local == 5 and report.drifts[0].venue == 0


def test_a_venue_holding_the_engine_forgot_is_missing_local():
    report = reconcile([], [VenuePosition("SPY|PUT|500|0DTE", 3)])
    assert [d.kind for d in report.drifts] == ["missing_local"]
    assert report.drifts[0].local == 0 and report.drifts[0].venue == 3


def test_a_partial_fill_recorded_as_a_full_clip_is_a_quantity_mismatch():
    report = reconcile([pos(contracts=5)], [VenuePosition("QQQ|CALL|600|0DTE", 2)])
    assert [d.kind for d in report.drifts] == ["quantity_mismatch"]
    assert report.drifts[0].local == 5 and report.drifts[0].venue == 2


def test_all_three_drifts_are_named_in_one_report():
    local = [pos("QQQ|CALL|600|0DTE", 5), pos("SPY|PUT|500|0DTE", 4)]
    venue = [VenuePosition("QQQ|CALL|600|0DTE", 2), VenuePosition("IWM|CALL|200|0DTE", 1)]
    report = reconcile(local, venue)
    assert {d.kind for d in report.drifts} == {
        "phantom_local", "missing_local", "quantity_mismatch"}
    assert report.local_contracts == 9 and report.venue_contracts == 3
    assert "quantity_mismatch:QQQ" in report.reason_text


def test_a_closed_local_position_is_not_a_holding():
    closed = pos(contracts=0)
    assert reconcile([closed], []).clean is True


def test_a_duplicate_venue_line_is_two_answers_and_is_refused():
    with pytest.raises(ReconcileError, match="duplicate venue contract"):
        reconcile([], [VenuePosition("QQQ|CALL|600|0DTE", 1),
                       VenuePosition("QQQ|CALL|600|0DTE", 2)])


@pytest.mark.parametrize("bad", [-1, True, 1.5, "5"])
def test_a_malformed_venue_count_is_refused_not_clamped(bad):
    with pytest.raises(ReconcileError):
        VenuePosition("QQQ|CALL|600|0DTE", bad)  # type: ignore[arg-type]


def test_a_venue_line_without_a_contract_key_is_refused():
    with pytest.raises(ReconcileError, match="without a contract_key"):
        VenuePosition("", 1)


def test_snapshot_parser_accepts_both_operator_spellings():
    payload = [{"contract_key": "QQQ|CALL|600|0DTE", "contracts": 2},
               {"key": "SPY|PUT|500|0DTE", "qty": 1}]
    parsed = snapshot_from_payload(payload)
    assert [item.contract_key for item in parsed] == [
        "QQQ|CALL|600|0DTE", "SPY|PUT|500|0DTE"]
    assert [item.contracts for item in parsed] == [2, 1]


def test_snapshot_parser_refuses_a_typo_instead_of_reading_it_as_flat():
    with pytest.raises(ReconcileError, match=r"snapshot\[0\]: missing contract_key"):
        snapshot_from_payload([{"ticker": "QQQ", "contracts": 2}])


def test_load_snapshot_refuses_a_missing_file(tmp_path):
    with pytest.raises(ReconcileError, match="not found"):
        load_snapshot(tmp_path / "absent.json")


# ── engine consequence: drift halts buys, never exits ────────────────────────
def _entry() -> Signal:
    return Signal(kind=AlertKind.ENTRY, channel=KING, ticker="QQQ", side=Side.CALL,
                  strike=600.0, expiry="0DTE", his_bid=1.0, raw="entry")


def _close() -> Signal:
    return Signal(kind=AlertKind.CLOSE, channel=KING, ticker="QQQ", side=Side.CALL,
                  strike=600.0, expiry="0DTE", price=1.1, raw="all out")


def test_a_clean_reconcile_writes_a_row_and_does_not_halt():
    eng = Engine(config=EngineConfig(pipe_test=False), fills=FillLog())
    eng.on_signal(_entry(), Quote(1.0, 1.0), now=DAY)
    venue = [VenuePosition("QQQ|CALL|600|0DTE", 15)]
    report = eng.reconcile_positions(venue, now=DAY + 30)
    assert report.clean is True
    assert eng.halted is False
    assert eng.fills.records[-1].event == "reconcile"


def test_drift_halts_new_buys_but_an_exit_still_runs():
    eng = Engine(config=EngineConfig(pipe_test=False), fills=FillLog())
    eng.on_signal(_entry(), Quote(1.0, 1.0), now=DAY)
    report = eng.reconcile_positions([], now=DAY + 30)
    assert report.clean is False
    assert eng.halted is True and eng.halt_reason == "reconcile-drift"
    alert = eng.fills.records[-1]
    assert alert.event == "alert" and alert.reason.startswith("reconcile_drift:")

    assert eng.on_signal(_entry(), Quote(1.0, 1.0), now=DAY + 60) == []
    assert eng.fills.records[-1].reason == "halted:reconcile-drift"

    closed = eng.on_signal(_close(), Quote(1.1, 1.1), now=DAY + 90)
    assert closed and closed[0].action == "sell"


# ── the operator surface ─────────────────────────────────────────────────────
def test_cli_reconcile_is_clean_when_both_books_agree(tmp_path, capsys):
    snapshot = tmp_path / "venue.json"
    snapshot.write_text("[]", encoding="utf-8")
    fills = tmp_path / "fills.jsonl"
    assert main(["reconcile", "--snapshot", str(snapshot), "--fills", str(fills)]) == 0
    assert "CLEAN" in capsys.readouterr().out


def test_cli_reconcile_exits_1_and_names_the_drift(tmp_path, capsys):
    snapshot = tmp_path / "venue.json"
    snapshot.write_text(
        json.dumps([{"contract_key": "QQQ|CALL|600|0DTE", "contracts": 2}]),
        encoding="utf-8")
    fills = tmp_path / "fills.jsonl"
    assert main(["reconcile", "--snapshot", str(snapshot), "--fills", str(fills)]) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out and "missing_local" in out


def test_cli_reconcile_exits_2_on_a_bad_snapshot(tmp_path, capsys):
    # NOTE: the wording changed when `venue.py` widened the accepted shapes to
    # `[{...}]` OR `{"positions": [...]}` with a timestamp. The invariant this test
    # pins is unchanged: a payload that is not a book exits 2 and names the problem.
    snapshot = tmp_path / "venue.json"
    snapshot.write_text('{"not": "a book"}', encoding="utf-8")
    assert main(["reconcile", "--snapshot", str(snapshot)]) == 2
    err = capsys.readouterr().err
    assert "cannot use venue snapshot" in err
    assert "position list" in err
