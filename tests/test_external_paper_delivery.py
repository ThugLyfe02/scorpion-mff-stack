"""P0 paper lifecycle fault tests. No authenticated or live network calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from threading import Barrier

import pytest

from scorpion.config import load_forward_rules
from scorpion.delivery import (
    PaperAccountSnapshot,
    PaperDeliveryError,
    PaperDeliveryLedger,
    PaperOrderRequest,
    PaperRiskPolicy,
    PaperVenueOrder,
)
from scorpion.execution_model import strict_paper_schedule
from scorpion.hot_path_benchmark import benchmark_paper_capture
from scorpion.paper_venue import (
    AlpacaEquityPaperVenue,
    cancel_paper_order,
    dispatch_paper_intent,
    recover_paper_orders,
    request_from_live_journal,
    sweep_overdue_paper_orders,
)
from scorpion.session_service import Journal, journal_receipt_path

DAY = date(2026, 11, 24)
OPEN = datetime(2026, 11, 24, 14, 30, tzinfo=UTC)
CLOSE = datetime(2026, 11, 24, 21, tzinfo=UTC)
NOW = OPEN + timedelta(minutes=12)
ACCOUNT = "00000000-0000-0000-0000-000000000001"
VENUE = "00000000-0000-0000-0000-000000000002"
RULES = load_forward_rules()
POLICY = PaperRiskPolicy(
    ACCOUNT, DAY, Decimal("100"), Decimal("150"), Decimal("100"), Decimal("100"),
    Decimal("5"), 4, (("ABC", "g1"), ("XYZ", "g1"), ("ZZZ", "g2")),
)


def snapshot(orders=(), positions=(), cash="1000", now=NOW):
    return PaperAccountSnapshot(
        ACCOUNT, now, Decimal(cash), Decimal(cash), tuple(orders), tuple(positions), "a" * 64, False,
    )


def request(call="a", symbol="XYZ", role="INITIAL", qty=6, now=NOW):
    return PaperOrderRequest(
        call, role, symbol, qty, Decimal("10"), "b" * 64, "c" * 64, 2,
        now, now + timedelta(minutes=3),
    )


def observed(req, state="WORKING", filled=0, *, updated=NOW, average=None):
    return PaperVenueOrder(
        req.client_id(POLICY), VENUE, req.symbol, req.side, req.quantity, req.limit,
        filled, Decimal(average) if average else None, state, NOW, updated,
    )


@pytest.fixture
def ledger(tmp_path):
    obj = PaperDeliveryLedger(tmp_path / "paper.db", POLICY)
    return obj, obj.initialize(snapshot(), now=NOW).chain_sha256


def test_ambiguous_dispatch_reservation_never_released_on_missing_order(ledger):
    obj, head = ledger
    req = request()
    first = obj.reserve_dispatch(req, snapshot(), now=NOW, expected_head=head)
    assert first.orders[0].state == "DISPATCH_UNCERTAIN"
    assert first.session_buy_commitment_usd == 60
    recovered = obj.reconcile(snapshot(), now=NOW, expected_head=first.chain_sha256)
    assert recovered.chain_sha256 == first.chain_sha256
    assert "unresolved_order_state" in recovered.blockers
    assert recovered.session_buy_commitment_usd == 60
    with pytest.raises(PaperDeliveryError):
        obj.reserve_dispatch(req, snapshot(), now=NOW, expected_head=recovered.chain_sha256)
    with pytest.raises(PaperDeliveryError, match="reconciliation"):
        obj.reserve_dispatch(request("b", "ZZZ"), snapshot(),
                             now=NOW, expected_head=recovered.chain_sha256)


def test_concurrent_reservations_cas_and_group_cap(ledger):
    obj, head = ledger
    barrier = Barrier(2)

    def attempt(symbol):
        barrier.wait(timeout=5)
        try:
            return obj.reserve_dispatch(request(symbol, symbol), snapshot(),
                                        now=NOW, expected_head=head)
        except PaperDeliveryError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["ABC", "XYZ"]))
    assert sum(row is not None for row in results) == 1
    committed = obj.status()
    assert committed.session_buy_commitment_usd == 60
    req = committed.orders[0].request
    venue = observed(req)
    confirmed = obj.reconcile(snapshot([venue]), now=NOW, expected_head=committed.chain_sha256)
    assert not confirmed.blockers
    other = "XYZ" if req.symbol == "ABC" else "ABC"
    with pytest.raises(PaperDeliveryError, match="exposure reservation"):
        obj.reserve_dispatch(request("other", other), snapshot([venue]),
                             now=NOW, expected_head=confirmed.chain_sha256)


def test_partial_fill_cancel_terminal_and_long_only_sell_reservation(ledger):
    obj, head = ledger
    req = request()
    pending = obj.reserve_dispatch(req, snapshot(), now=NOW, expected_head=head)
    partial = observed(req, "PARTIAL", 3, average="9.90")
    updated = obj.reconcile(snapshot([partial], [("XYZ", 3)], "970.30"),
                            now=NOW, expected_head=pending.chain_sha256)
    assert updated.session_buy_commitment_usd == Decimal("59.70")
    claimed = obj.claim_cancel(req.client_id(POLICY), now=NOW, expected_head=updated.chain_sha256)
    assert claimed.orders[0].state == "CANCEL_UNCERTAIN"
    assert claimed.session_buy_commitment_usd == Decimal("59.70")
    cancelled = replace(partial, state="CANCELLED", updated=NOW + timedelta(seconds=1))
    tick = NOW + timedelta(seconds=1)
    current = obj.reconcile(snapshot([cancelled], [("XYZ", 3)], "970.30", tick),
                            now=tick, expected_head=claimed.chain_sha256)
    assert current.session_buy_commitment_usd == Decimal("29.70")
    assert not current.blockers
    with pytest.raises(PaperDeliveryError, match="zero initial"):
        obj.reserve_dispatch(request(role="CHASE", now=tick),
                             snapshot([cancelled], [("XYZ", 3)], "970.30", tick),
                             now=tick, expected_head=current.chain_sha256)
    with pytest.raises(PaperDeliveryError, match="exact terminal"):
        obj.reserve_dispatch(request(role="EXIT", qty=4, now=tick),
                             snapshot([cancelled], [("XYZ", 3)], "970.30", tick),
                             now=tick, expected_head=current.chain_sha256)
    sell = obj.reserve_dispatch(request(role="EXIT", qty=3, now=tick),
                                snapshot([cancelled], [("XYZ", 3)], "970.30", tick),
                                now=tick, expected_head=current.chain_sha256)
    assert dict(sell.pending_sell_quantities) == {"XYZ": 3}
    assert sell.session_buy_commitment_usd == Decimal("29.70")


def test_venue_regression_and_external_drift_do_not_authorize_new_orders(ledger):
    obj, head = ledger
    req = request()
    dispatched = obj.reserve_dispatch(req, snapshot(), now=NOW, expected_head=head)
    filled = observed(req, "FILLED", 6, average="9.90")
    good = obj.reconcile(snapshot([filled], [("XYZ", 6)], "940.60"),
                         now=NOW, expected_head=dispatched.chain_sha256)
    bad = replace(filled, state="PARTIAL", filled=2)
    with pytest.raises(PaperDeliveryError, match="regression"):
        obj.reconcile(snapshot([bad], [("XYZ", 2)], "980.20"),
                      now=NOW, expected_head=good.chain_sha256)
    drift = obj.reconcile(snapshot([filled], [("XYZ", 7)], "940.60"),
                          now=NOW, expected_head=good.chain_sha256)
    assert "external_position_mismatch" in drift.blockers
    assert drift.chain_sha256 == good.chain_sha256


def test_terminal_recovery_after_midnight_is_read_only(ledger):
    obj, head = ledger
    req = request()
    dispatched = obj.reserve_dispatch(req, snapshot(), now=NOW, expected_head=head)
    tomorrow = NOW + timedelta(days=1)
    working = observed(req)
    overdue = obj.reconcile(snapshot([working], now=tomorrow), now=tomorrow,
                            expected_head=dispatched.chain_sha256)
    assert overdue.overdue_working_ids == (req.client_id(POLICY),)
    with pytest.raises(PaperDeliveryError, match="regular-session"):
        obj.reserve_dispatch(request("b", now=tomorrow), snapshot([working], now=tomorrow),
                             now=tomorrow, expected_head=overdue.chain_sha256)


def live_prefix(path: Path) -> int:
    source = NOW - timedelta(minutes=2)
    schedule = strict_paper_schedule(source, source, RULES, session_open=OPEN, session_close=CLOSE)
    assert schedule.entry_ts == NOW
    ns = int(NOW.timestamp()) * 1_000_000_000
    journal = Journal(path, require_lossless=True)
    journal.submit({
        "kind": "session_start", "mode": "PAPER", "session_date": str(DAY),
        "rules_sha256": RULES.sha256, "signal_ingress_enabled": True, "latch_armed": True,
        "ts_utc": OPEN.isoformat(),
    })
    journal.submit({
        "kind": "decision", "action": "schedule_entry", "call_id": "a", "ticker": "XYZ",
        "rules_sha256": RULES.sha256, "source_ts_utc": source.isoformat(),
        "received_ts_utc": source.isoformat(), "ts_utc": source.isoformat(),
        "revision_id": "d" * 64, "content_sha256": "e" * 64,
    })
    seq = journal.submit({
        "kind": "intent", "call_id": "a", "ticker": "XYZ", "rules_sha256": RULES.sha256,
        "mode": "PAPER", "status": "PAPER_INTENT_ONLY", "chase": False,
        "conditional_on": None, "limit": "10.00", "quantity": 6, "reference_price": "10.00",
        "ts_utc": NOW.isoformat(), "scheduled_entry_ts_utc": schedule.entry_ts.isoformat(),
        "scheduled_chase_ts_utc": schedule.chase_ts.isoformat(),
        "scheduled_exit_ts_utc": schedule.exit_ts.isoformat(),
        "quote_evidence": {
            "bid": "9.99", "ask": "10.01", "observed_ts_utc": NOW.isoformat(),
            "ts_event_ns": ns, "recv_mono_ns": 100, "decision_mono_ns": 100, "source_age_ns": 0,
        },
    })
    journal.close()
    # Fixture reproduces a durable prefix of a still-open writer. No production mutation.
    journal_receipt_path(path).unlink()
    return seq


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setenv("SCORPION_ALPACA_PAPER_KEY", "fixture-key")
    monkeypatch.setenv("SCORPION_ALPACA_PAPER_SECRET", "fixture-secret")
    monkeypatch.setattr("scorpion.paper_venue._latch_ok", lambda _: True)
    remote = {"orders": [], "positions": [], "cash": "1000", "posts": 0, "deletes": 0,
              "lost_ack": True, "now": NOW}

    def transport(req, timeout):
        assert req.full_url.startswith("https://paper-api.alpaca.markets/v2/")
        method = req.get_method()
        if method == "POST":
            remote["posts"] += 1
            payload = json.loads(req.data)
            assert payload["extended_hours"] is False and payload["time_in_force"] == "day"
            row = {
                **payload, "id": VENUE, "asset_class": "us_equity",
                "submitted_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
                "filled_qty": "0", "filled_avg_price": None, "status": "new",
            }
            remote["orders"].append(row)
            if remote["lost_ack"]:
                raise OSError("synthetic lost response")
            return 200, json.dumps(row).encode()
        if method == "DELETE":
            remote["deletes"] += 1
            return 204, b""
        url = req.full_url
        if "/v2/account" in url:
            payload = {
                "id": ACCOUNT, "currency": "USD", "status": "ACTIVE",
                "cash": remote["cash"], "buying_power": remote["cash"],
                "account_blocked": False, "trading_blocked": False,
            }
        elif "/v2/clock" in url:
            payload = {"is_open": True, "timestamp": remote["now"].isoformat()}
        elif "/v2/positions" in url:
            payload = remote["positions"]
        elif "/v2/orders:by_client_order_id" in url:
            if not remote["orders"]:
                return 404, b""
            payload = remote["orders"][0]
        elif "status=open" in url:
            payload = [row for row in remote["orders"] if row["status"] == "new"]
        else:
            payload = remote["orders"]
        return 200, json.dumps(payload).encode()

    return remote, AlpacaEquityPaperVenue(ACCOUNT, transport=transport, clock=lambda: remote["now"])


def test_lost_ack_then_recovery_and_cancel_never_resubmit(tmp_path, ledger, adapter):
    obj, head = ledger
    remote, venue = adapter
    journal = tmp_path / "capture.jsonl"
    seq = live_prefix(journal)
    report = dispatch_paper_intent(
        obj, venue, journal=journal, seq=seq, expected_head=head,
        latch_path=tmp_path / "synthetic", clock=lambda: NOW,
    )
    assert remote["posts"] == 1 and report.orders[0].state == "DISPATCH_UNCERTAIN"
    with pytest.raises(PaperDeliveryError, match="duplicate"):
        dispatch_paper_intent(obj, venue, journal=journal, seq=seq,
                              expected_head=report.chain_sha256,
                              latch_path=tmp_path / "synthetic", clock=lambda: NOW)
    recovered = recover_paper_orders(obj, venue, expected_head=report.chain_sha256,
                                     clock=lambda: NOW)
    assert remote["posts"] == 1 and recovered.orders[0].state == "WORKING"
    assert not recovered.blockers
    pending = cancel_paper_order(obj, venue, client_id=recovered.orders[0].client_id,
                                 expected_head=recovered.chain_sha256, clock=lambda: NOW)
    assert remote["deletes"] == 1 and pending.orders[0].state == "CANCEL_UNCERTAIN"
    assert pending.session_buy_commitment_usd == 60
    remote["orders"][0]["status"] = "canceled"
    done = recover_paper_orders(obj, venue, expected_head=pending.chain_sha256, clock=lambda: NOW)
    assert done.orders[0].state == "CANCELLED"
    assert done.session_buy_commitment_usd == 0
    assert remote["posts"] == remote["deletes"] == 1


def test_stale_or_sealed_intention_cannot_dispatch(tmp_path, ledger):
    obj, _ = ledger
    path = tmp_path / "capture.jsonl"
    seq = live_prefix(path)
    with pytest.raises(PaperDeliveryError, match="stale"):
        request_from_live_journal(path, seq, policy=POLICY, now=NOW + timedelta(seconds=4),
                                  ledger_report=obj.status())
    journal_receipt_path(path).write_text("{}")
    with pytest.raises(PaperDeliveryError, match="closed"):
        request_from_live_journal(path, seq, policy=POLICY, now=NOW,
                                  ledger_report=obj.status())


def test_capture_benchmark_verifies_every_sequence(tmp_path):
    report = benchmark_paper_capture(samples=128, batch=16, workspace=tmp_path)
    assert report.exact_receipt_verified and not report.failures
    assert report.requested_records == report.admitted_records == report.durable_records == 128
    assert report.records_per_second > 0 and report.bytes_per_second > 0
    assert not report.production_capacity_certified


def _crash_after_commit(path: str, head: str) -> None:
    obj = PaperDeliveryLedger(Path(path), POLICY)
    obj.reserve_dispatch(request(), snapshot(), now=NOW, expected_head=head)
    # Actual abrupt process exit: no Python teardown or delivery acknowledgement.
    os._exit(73)


def test_process_exit_after_dispatch_commit_does_not_release_or_retry(ledger):
    obj, head = ledger
    child = subprocess.run(
        [sys.executable, "-c",
         "import runpy,sys;runpy.run_path(sys.argv[1])['_crash_after_commit'](*sys.argv[2:])",
         __file__, str(obj.path), head],
        timeout=15, check=False, capture_output=True,
    )
    assert child.returncode == 73, child.stderr.decode()
    reopened = PaperDeliveryLedger(obj.path, POLICY)
    report = reopened.status()
    assert report.orders[0].state == "DISPATCH_UNCERTAIN"
    assert report.session_buy_commitment_usd == Decimal("60")
    with pytest.raises(PaperDeliveryError, match="duplicate"):
        reopened.reserve_dispatch(request(), snapshot(), now=NOW, expected_head=report.chain_sha256)


def test_deadline_sweep_cancels_once_without_new_submissions(tmp_path, ledger, adapter):
    obj, head = ledger
    remote, venue = adapter
    journal = tmp_path / "capture.jsonl"
    seq = live_prefix(journal)
    report = dispatch_paper_intent(
        obj, venue, journal=journal, seq=seq, expected_head=head,
        latch_path=tmp_path / "synthetic", clock=lambda: NOW,
    )
    remote["now"] = NOW + timedelta(minutes=4)
    late = remote["now"]
    swept = sweep_overdue_paper_orders(
        obj, venue, expected_head=report.chain_sha256, clock=lambda: late,
    )
    assert remote["posts"] == remote["deletes"] == 1
    assert swept.orders[0].state == "CANCEL_UNCERTAIN"
    again = sweep_overdue_paper_orders(
        obj, venue, expected_head=swept.chain_sha256, clock=lambda: late,
    )
    assert remote["posts"] == remote["deletes"] == 1
    assert again.session_buy_commitment_usd == Decimal("60")
    remote["orders"][0]["status"] = "canceled"
    terminal = sweep_overdue_paper_orders(
        obj, venue, expected_head=again.chain_sha256, clock=lambda: late,
    )
    assert terminal.orders[0].state == "CANCELLED"
    assert terminal.session_buy_commitment_usd == 0


def test_expired_send_window_refused_before_dispatch(ledger):
    obj, head = ledger
    req = replace(request(), admit_until=NOW + timedelta(milliseconds=500))
    clock = NOW + timedelta(seconds=1)
    with pytest.raises(PaperDeliveryError, match="validity expired"):
        obj.reserve_dispatch(req, snapshot(now=clock), now=clock, expected_head=head)
    assert not obj.status().orders


def test_reservations_not_rounded_by_caller_decimal_context(ledger):
    obj, head = ledger
    req = request()
    dispatch = obj.reserve_dispatch(req, snapshot(), now=NOW, expected_head=head)
    partial = observed(req, "PARTIAL", 3, average="9.87654321")
    report = obj.reconcile(
        snapshot([partial], [("XYZ", 3)], "970.37037037"),
        now=NOW, expected_head=dispatch.chain_sha256,
    )
    assert report.session_buy_commitment_usd == Decimal("59.62962963")
    with localcontext() as ctx:
        ctx.prec = 5
        assert obj.status().session_buy_commitment_usd == Decimal("59.62962963")
