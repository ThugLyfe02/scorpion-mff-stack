"""Synthetic capture fixtures exercise causality, depth conservation and crash evidence."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from scorpion.config import load_forward_rules
from scorpion.databento_feed import DatabentoLiveClient, FeedMessage
from scorpion.exec_cli import _probe_live_quote
from scorpion.execution_model import strict_paper_schedule
from scorpion.paper_equity import replay_equity_market_journal
from scorpion.recovery import recover_paper_session_journal
from scorpion.session_service import Journal, journal_receipt_path

DAY = "2026-11-24"
OPEN = datetime(2026, 11, 24, 14, 30, tzinfo=UTC)
CLOSE = datetime(2026, 11, 24, 21, 0, tzinfo=UTC)
SOURCE = OPEN + timedelta(minutes=10)
RULES = load_forward_rules()
SCHEDULE = strict_paper_schedule(SOURCE, SOURCE, RULES, session_open=OPEN, session_close=CLOSE)


def ns(t: datetime) -> int:
    return int(t.timestamp()) * 1_000_000_000 + t.microsecond * 1000


def fixture(path: Path, *, gap: bool = False, close: bool = True) -> Path:
    journal = Journal(path)
    journal.submit({
        "kind": "session_start", "session_date": DAY, "mode": "PAPER",
        "rules_sha256": RULES.sha256, "ts_utc": (OPEN - timedelta(seconds=1)).isoformat(),
        "market_capture_enabled": True, "market_dataset": "XNAS.ITCH", "market_schema": "mbp-1",
        "signal_ingress_enabled": True, "latch_armed": True,
        "safety_integrity_valid": True, "safety_event_count": 1,
    })
    journal.submit({
        "kind": "decision", "action": "schedule_entry", "call_id": "a", "ticker": "XYZ",
        "rules_sha256": RULES.sha256, "source_ts_utc": SOURCE.isoformat(),
        "received_ts_utc": SOURCE.isoformat(), "ts_utc": SOURCE.isoformat(),
        "revision_id": "a" * 64, "content_sha256": "b" * 64,
    })
    common = {
        "kind": "intent", "call_id": "a", "ticker": "XYZ", "rules_sha256": RULES.sha256,
        "mode": "PAPER", "status": "PAPER_INTENT_ONLY", "quantity": 10, "limit": "10.00",
        "reference_price": "10.00",
        "scheduled_entry_ts_utc": SCHEDULE.entry_ts.isoformat(),
        "scheduled_chase_ts_utc": SCHEDULE.chase_ts.isoformat(),
        "scheduled_exit_ts_utc": SCHEDULE.exit_ts.isoformat(),
    }

    def intent(chase: bool) -> None:
        t = SCHEDULE.chase_ts if chase else SCHEDULE.entry_ts
        journal.submit({
            **common, "chase": chase, "conditional_on": "initial_unfilled" if chase else None,
            "ts_utc": t.isoformat(),
            "quote_evidence": {
                "bid": "9.90", "ask": "9.99", "observed_ts_utc": t.isoformat(),
                "ts_event_ns": ns(t), "recv_mono_ns": 100, "decision_mono_ns": 100,
                "source_age_ns": 0,
            },
        })

    count = 0

    def quote(t: datetime, bid: str, ask: str, bid_size: int, ask_size: int) -> None:
        nonlocal count
        count += 1
        journal.submit({
            "kind": "market_quote", "symbol": "XYZ", "ts_utc": t.isoformat(),
            "ts_event_ns": ns(t), "recv_wall_ns": ns(t), "recv_mono_ns": ns(t) - ns(OPEN),
            "bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size,
            "publisher_id": 1, "instrument_id": 2, "provider_sequence": count,
        })

    intent(False)
    t = SCHEDULE.entry_ts + timedelta(seconds=1)
    quote(t, "9.90", "9.99", 10, 4)
    # Identical newer quote cannot refill the consumed displayed queue.
    quote(t + timedelta(seconds=1), "9.90", "9.99", 10, 4)
    quote(t + timedelta(seconds=2), "9.90", "9.99", 10, 6)
    intent(True)
    quote(SCHEDULE.chase_ts + timedelta(seconds=1), "9.90", "9.99", 10, 20)
    if gap:
        t = SCHEDULE.chase_ts + timedelta(seconds=2)
        journal.submit({
            "kind": "market_gap", "symbol": None, "ts_utc": t.isoformat(),
            "reason": "transport_disconnect", "recv_wall_ns": ns(t),
        })
        count += 1
    if close:
        journal.submit({
            "kind": "conditional_exit_intent", "call_id": "a", "status": "PAPER_INTENT_ONLY",
            "conditional_on": "confirmed_entry_fill", "quantity": 10,
            "scheduled_exit_ts_utc": SCHEDULE.exit_ts.isoformat(),
            "ts_utc": SCHEDULE.exit_ts.isoformat(),
        })
        quote(SCHEDULE.exit_ts + timedelta(seconds=1), "10.50", "10.51", 6, 10)
        journal.submit({
            "kind": "session_end", "session_date": DAY, "mode": "PAPER",
            "rules_sha256": RULES.sha256, "ts_utc": (CLOSE + timedelta(minutes=5)).isoformat(),
            "market_records": count, "feed_disconnects": int(gap), "latch_armed": True,
            "ingress_overflow": False, "pending_ingress": 0, "ingress_callback_failures": 0,
            "safety_integrity_valid": True, "safety_event_count": 1,
            "feed_first_usable_recv_wall_ns": ns(OPEN),
            "ingress_scope_ready_ts_utc": OPEN.isoformat(), "ingress_first_disconnect_ts_utc": None,
        })
    journal.close()
    return path


def test_partial_fill_shared_depth_chase_suppression_and_round_trip(tmp_path: Path) -> None:
    path = fixture(tmp_path / "journal.jsonl")
    report = replay_equity_market_journal(path)
    assert report == replay_equity_market_journal(path)
    assert not report.blockers
    assert [(f.role, f.quantity) for f in report.modeled_fills] == [
        ("INITIAL", 4), ("INITIAL", 2), ("EXIT", 6),
    ]
    call = report.calls[0]
    assert call.bought == call.sold == 6
    assert call.initial_target == 10 and not report.open_inventory
    assert call.gross_round_trip_usd == Decimal("3")
    assert call.assumed_round_trip_cost_usd == Decimal("0.06")
    assert report.modeled_net_round_trip_usd == Decimal("2.94")
    assert not report.certifies_venue_fills and not report.authorizes_live_orders


def test_gap_preserves_inventory_not_fake_liquidation(tmp_path: Path) -> None:
    report = replay_equity_market_journal(fixture(tmp_path / "journal.jsonl", gap=True))
    assert report.open_inventory == (("XYZ", 6),)
    assert report.modeled_net_round_trip_usd is None
    assert "market_or_safety_continuity_lost" in report.blockers
    assert all(f.side == "BUY" for f in report.modeled_fills)


def test_recovered_prefix_reports_inventory_without_resume(tmp_path: Path) -> None:
    path = fixture(tmp_path / "journal.jsonl", close=False)
    journal_receipt_path(path).unlink()
    tail = b'{"unverified":'
    path.write_bytes(path.read_bytes() + tail)
    dest = tmp_path / "recovered"
    recover_paper_session_journal(path, dest)
    report = replay_equity_market_journal(dest, recovered=True)
    assert report.open_inventory == (("XYZ", 6),)
    assert report.modeled_net_round_trip_usd is None and not report.resume_authorized
    assert "recovered_prefix_not_complete_session" in report.blockers


def test_strict_schedule_minute_rounding_and_early_close() -> None:
    start = SOURCE + timedelta(seconds=1)
    schedule = strict_paper_schedule(start, start, RULES, session_open=OPEN, session_close=CLOSE)
    assert schedule.entry_ts == SOURCE + timedelta(minutes=3)
    early = OPEN + timedelta(hours=3, minutes=30)
    post = early - timedelta(minutes=30)
    schedule = strict_paper_schedule(post, post, RULES, session_open=OPEN, session_close=early)
    assert schedule.exit_ts == early - timedelta(minutes=5)


def test_live_probe_requires_fresh_displayed_depth_and_closes_stream(monkeypatch) -> None:
    closed = []

    class FixtureClient(DatabentoLiveClient):
        async def stream(self, symbols, *, snapshot):
            assert not snapshot and symbols == ["SPY"]
            try:
                yield FeedMessage("SPY", Decimal("10"), Decimal("10.1"), None, 1)
                yield FeedMessage("SPY", Decimal("10"), Decimal("10.1"), None, 100, 5, 6, 1, 2)
            finally:
                closed.append(True)

    monkeypatch.setattr("scorpion.exec_cli.time.time_ns", lambda: 101)
    report = asyncio.run(_probe_live_quote(FixtureClient(), "SPY", 1))
    assert report["ok"] and report["usable_displayed_depth"]
    assert not report["certifies_continuity"] and not report["authorizes_orders"]
    assert closed == [True]
    with pytest.raises(ValueError):
        asyncio.run(_probe_live_quote(FixtureClient(), "SPY", 0))
