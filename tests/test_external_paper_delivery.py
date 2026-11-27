"""P0 paper lifecycle fault tests. No authenticated or live network calls."""
from __future__ import annotations

import hashlib
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
from types import SimpleNamespace

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


def test_supervised_guard_closes_after_commit_without_post(tmp_path, ledger, adapter):
    obj, head = ledger
    remote, venue = adapter
    path = tmp_path / "capture.jsonl"
    seq = live_prefix(path)
    checks = iter((True, True, False))
    report = dispatch_paper_intent(
        obj, venue, journal=path, seq=seq, expected_head=head,
        latch_path=tmp_path / "synthetic", clock=lambda: NOW,
        supervisor_guard=lambda: next(checks),
    )
    assert remote["posts"] == 0
    assert report.orders[0].state == "DISPATCH_UNCERTAIN"
    assert report.session_buy_commitment_usd == 60


def _worker_fixture(tmp_path, ledger, adapter, monkeypatch):
    from scorpion.paper_worker import SupervisedPaperWorker, load_worker_authorization

    obj, head = ledger
    remote, venue = adapter
    policy_path = tmp_path / "risk.json"
    policy_path.write_text(json.dumps({
        "schema": "scorpion.paper-risk.v1", "authority": "PAPER_ONLY",
        "account_id": ACCOUNT, "session_date": str(DAY),
        "maximum_order_usd": "100", "maximum_session_buy_usd": "150",
        "maximum_symbol_buy_usd": "100", "maximum_group_buy_usd": "100",
        "fee_cash_buffer_usd": "5", "maximum_working_orders": 4,
        "symbols_and_groups": dict(POLICY.symbols_and_groups),
    }))
    manifest = tmp_path / "worker.json"
    manifest.write_text(json.dumps({
        "schema": "scorpion.supervised-paper-worker.v1", "authority": "PAPER_ONLY",
        "risk_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        "opening_ledger_head": head,
        "exit_limits": {sym: "9.00" for sym, _ in POLICY.symbols_and_groups},
    }))
    auth = load_worker_authorization(manifest, hashlib.sha256(manifest.read_bytes()).hexdigest())
    path = tmp_path / "capture.jsonl"
    seq = live_prefix(path)
    row = json.loads(path.read_text().splitlines()[-1])
    source = SimpleNamespace(path=path, durable_sequence=seq, check=lambda: None)
    monkeypatch.setattr("scorpion.paper_worker.AlpacaEquityPaperVenue", lambda *a, **k: venue)
    worker = SupervisedPaperWorker(
        authorization=auth, risk_path=policy_path, ledger_path=obj.path,
        source=source, audit_path=tmp_path / "audit.jsonl", latch_path=tmp_path / "synthetic",
        clock=lambda: remote["now"],
    )
    worker._audit = Journal(tmp_path / "audit.jsonl", require_lossless=True)
    worker.heartbeat()
    worker._maintain()
    return worker, remote, source, seq, row


def test_worker_lost_ack_recovers_without_duplicate(tmp_path, ledger, adapter, monkeypatch):
    worker, remote, _, seq, row = _worker_fixture(tmp_path, ledger, adapter, monkeypatch)
    try:
        worker.offer(seq, row)
        worker._process(worker._queue.get_nowait())
        assert remote["posts"] == 1
        worker._maintain()
        assert not worker._blockers
        worker.offer(seq, row)
        worker._process(worker._queue.get_nowait())
        assert remote["posts"] == 1
        worker.stop_admission()
        report = worker._ledger.status(expected_head=worker._head)
        assert report.orders[0].state == "WORKING"
    finally:
        worker._audit.close()
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [r["disposition"] for r in audit if r["kind"] == "worker_disposition"] == [
        "DURABLE_DISPATCH", "NOT_DISPATCHED",
    ]


def test_worker_durability_deadline_and_supervision(tmp_path, ledger, adapter, monkeypatch):
    worker, remote, source, seq, row = _worker_fixture(tmp_path, ledger, adapter, monkeypatch)
    try:
        source.durable_sequence = seq - 1
        worker.offer(seq, row)
        remote["now"] = NOW + timedelta(seconds=4)
        worker._process(worker._queue.get_nowait())
        assert remote["posts"] == 0
        source.durable_sequence = seq
        remote["now"] = NOW
        worker._last_health = -1.0
        worker.offer(seq, row)
        worker._process(worker._queue.get_nowait())
        assert remote["posts"] == 0
    finally:
        worker._audit.close()


def test_worker_authorization_pin_refuses_modified_bytes(tmp_path):
    from scorpion.paper_worker import load_worker_authorization

    path = tmp_path / "manifest.json"
    path.write_text("{}")
    with pytest.raises(PaperDeliveryError, match="pin mismatch"):
        load_worker_authorization(path, "a" * 64)


def test_journal_durable_sequence_follows_checkpoint(tmp_path):
    from scorpion.session_service import journal_checkpoint_path

    path = tmp_path / "journal.jsonl"
    journal = Journal(path, require_lossless=True)
    seq = journal.submit({"kind": "test"})
    journal.close()
    assert journal.durable_sequence == seq
    assert json.loads(journal_checkpoint_path(path).read_text())["records"] == seq


def test_supervisor_record_binding_refuses_changed_candidate(tmp_path, ledger, adapter):
    obj, head = ledger
    remote, venue = adapter
    path = tmp_path / "capture.jsonl"
    seq = live_prefix(path)
    with pytest.raises(PaperDeliveryError, match="candidate differs"):
        dispatch_paper_intent(
            obj, venue, journal=path, seq=seq, expected_head=head,
            latch_path=tmp_path / "synthetic", clock=lambda: NOW,
            expected_record_sha256="f" * 64,
        )
    assert remote["posts"] == 0
    assert not obj.status(expected_head=head).orders


def test_campaign_probe_is_read_only_and_measures_combined_budget(
    tmp_path, ledger, adapter, monkeypatch,
):
    from scorpion.paper_campaign_probe import probe_campaign

    worker, remote, _, _, _ = _worker_fixture(tmp_path, ledger, adapter, monkeypatch)
    worker._audit.close()
    obj, head = ledger
    _, venue = adapter
    monkeypatch.setattr("scorpion.paper_campaign_probe.AlpacaEquityPaperVenue", lambda *a, **k: venue)
    monkeypatch.setattr("scorpion.paper_campaign_probe._latch_ok", lambda _: True)
    manifest = tmp_path / "worker.json"
    counter = iter((0, 600_000_000, 700_000_000, 1_700_000_000))
    result = probe_campaign(
        worker_config=manifest, worker_pin=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        risk_path=tmp_path / "risk.json", ledger_path=obj.path,
        latch_path=tmp_path / "synthetic", samples=1,
        clock=lambda: NOW, monotonic_ns=lambda: next(counter),
    )
    assert result.max_combined_ms == 1600
    assert result.blockers == ("read_latency_exceeds_probe_budget",)
    assert remote["posts"] == remote["deletes"] == 0
    assert obj.status(expected_head=head).events == 1


def test_preopen_probe_distinguishes_fresh_closed_clock(tmp_path, ledger, adapter, monkeypatch):
    from scorpion.paper_campaign_probe import probe_campaign

    worker, remote, _, _, _ = _worker_fixture(tmp_path, ledger, adapter, monkeypatch)
    worker._audit.close()
    obj, _ = ledger
    _, venue = adapter
    remote["now"] = OPEN - timedelta(minutes=5)
    monkeypatch.setattr(venue, "market_clock", lambda _: False)
    monkeypatch.setattr("scorpion.paper_campaign_probe.AlpacaEquityPaperVenue", lambda *a, **k: venue)
    monkeypatch.setattr("scorpion.paper_campaign_probe._latch_ok", lambda _: True)
    manifest = tmp_path / "worker.json"
    counter = iter((0, 100_000_000, 100_000_000, 200_000_000))
    result = probe_campaign(
        worker_config=manifest, worker_pin=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        risk_path=tmp_path / "risk.json", ledger_path=obj.path,
        latch_path=tmp_path / "synthetic", samples=1, pre_open=True,
        clock=lambda: remote["now"], monotonic_ns=lambda: next(counter),
    )
    assert result.phase == "PRE_OPEN" and not result.blockers
    assert remote["posts"] == remote["deletes"] == 0
    assert not result.authorizes_orders
    with pytest.raises(ValueError, match="session phase"):
        probe_campaign(
            worker_config=manifest, worker_pin=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            risk_path=tmp_path / "risk.json", ledger_path=obj.path,
            latch_path=tmp_path / "synthetic", samples=1,
            clock=lambda: remote["now"],
        )


def test_inspect_snapshot_never_merges_or_moves_head(ledger):
    obj, head = ledger
    req = request()
    sent = obj.reserve_dispatch(req, snapshot(), now=NOW, expected_head=head)
    candidate = snapshot((observed(req),))
    check = obj.inspect_snapshot(candidate, expected_head=sent.chain_sha256)
    assert "unresolved_order_state" in check.blockers
    assert obj.status(expected_head=sent.chain_sha256).orders[0].observation is None


def _capture_transport(remote, fills, *, extra=None):
    """Mock only the documented read paths; no credentials leave this test."""
    def transport(req, timeout):
        from urllib.parse import parse_qs, urlsplit

        assert req.get_method() == "GET"
        assert req.full_url.startswith("https://paper-api.alpaca.markets/v2/")
        url = urlsplit(req.full_url)
        params = parse_qs(url.query)
        if url.path == "/v2/account":
            body = {
                "id": ACCOUNT, "currency": "USD", "status": "ACTIVE",
                "cash": remote["cash"], "buying_power": remote["cash"],
                "trading_blocked": False, "account_blocked": False,
            }
        elif url.path == "/v2/orders":
            body = remote["orders"] if params["status"] == ["all"] else []
        elif url.path == "/v2/positions":
            body = remote["positions"]
        elif url.path == "/v2/account/activities":
            assert params["activity_types"] == ["FILL"]
            assert params["page_size"] == ["100"] and params["direction"] == ["asc"]
            if "page_token" in params:
                assert params["page_token"] == [fills[99]["id"]]
                body = fills[100:] if extra is None else extra
            else:
                body = fills[:100]
        else:
            raise AssertionError("unexpected request")
        return 200, json.dumps(body).encode()

    return transport


def _fill_activity(i, qty, cumulative, total, price="10.00"):
    return {
        "activity_type": "FILL", "id": f"fill-{i}", "order_id": VENUE,
        "qty": str(qty), "cum_qty": str(cumulative), "leaves_qty": str(total-cumulative),
        "price": price, "symbol": "XYZ", "side": "buy", "type": "partial_fill",
        "transaction_time": NOW.isoformat(),
    }


def test_collector_pagination_exact_bytes_and_tamper(tmp_path, monkeypatch):
    from scorpion.paper_evidence_collector import collect_venue_artifact, replay_venue_artifact

    monkeypatch.setenv("SCORPION_ALPACA_PAPER_KEY", "fixture-key")
    monkeypatch.setenv("SCORPION_ALPACA_PAPER_SECRET", "fixture-secret")
    order = {
        "id": VENUE, "client_order_id": "paper-id", "asset_class": "us_equity",
        "type": "limit", "time_in_force": "day", "extended_hours": False,
        "symbol": "XYZ", "side": "buy", "qty": "101", "limit_price": "10",
        "filled_qty": "101", "filled_avg_price": "10.00", "status": "filled",
        "submitted_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
        "filled_at": NOW.isoformat(),
    }
    remote = {
        "orders": [order], "positions": [{"asset_class": "us_equity", "side": "long",
                                         "symbol": "XYZ", "qty": "101"}], "cash": "1000",
    }
    fills = [_fill_activity(i, 1, i+1, 101) for i in range(101)]
    path = tmp_path / "source.json"
    now = CLOSE + timedelta(minutes=7)
    receipt = collect_venue_artifact(
        account_id=ACCOUNT, session_date=DAY, output=path,
        transport=_capture_transport(remote, fills), clock=lambda: now,
    )
    assert receipt.requests == 12 and receipt.fills == 101
    assert receipt.blockers == ("account_not_flat",)
    assert path.stat().st_mode & 0o077 == 0
    assert b"fixture-secret" not in path.read_bytes()
    verified, view = replay_venue_artifact(
        path, receipt.source_artifact_sha256, account=ACCOUNT, day=DAY,
    )
    assert receipt == verified and sum(f.quantity for f in view.fills) == 101
    with pytest.raises(ValueError, match="already exists"):
        collect_venue_artifact(
            account_id=ACCOUNT, session_date=DAY, output=path,
            transport=_capture_transport(remote, fills), clock=lambda: now,
        )
    data = json.loads(path.read_bytes())
    data["responses"][0]["sha256"] = "f" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="pin mismatch"):
        replay_venue_artifact(path, receipt.source_artifact_sha256, account=ACCOUNT, day=DAY)
    with pytest.raises(ValueError, match="response identity"):
        replay_venue_artifact(
            path, hashlib.sha256(path.read_bytes()).hexdigest(), account=ACCOUNT, day=DAY,
        )
    with pytest.raises(ValueError, match="duplicate fill"):
        collect_venue_artifact(
            account_id=ACCOUNT, session_date=DAY, output=tmp_path / "duplicate.json",
            transport=_capture_transport(remote, fills, extra=[fills[99]]), clock=lambda: now,
        )
    with pytest.raises(ValueError, match="aggregate mismatch"):
        collect_venue_artifact(
            account_id=ACCOUNT, session_date=DAY, output=tmp_path / "missing.json",
            transport=_capture_transport(remote, fills[:-1]), clock=lambda: now,
        )


def test_collector_unsigned_draft_then_independent_signed_admission(
    tmp_path, ledger, adapter, monkeypatch,
):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from scorpion.execution_meta_labels import PaperOutcome, PaperOutcomeAdmission
    from scorpion.paper_evidence_collector import (
        collect_venue_artifact,
        prepare_outcome_draft,
        verify_reviewed_outcomes,
    )
    from scorpion.provenance import stable_hash

    worker, remote, source, seq, row = _worker_fixture(tmp_path, ledger, adapter, monkeypatch)
    worker._audit.close()
    obj, head = ledger
    _, venue = adapter
    report = dispatch_paper_intent(
        obj, venue, journal=source.path, seq=seq, expected_head=head,
        latch_path=tmp_path / "synthetic", clock=lambda: NOW,
    )
    report = recover_paper_orders(
        obj, venue, expected_head=report.chain_sha256, clock=lambda: NOW,
    )
    report = cancel_paper_order(
        obj, venue, client_id=report.orders[0].client_id,
        expected_head=report.chain_sha256, clock=lambda: NOW,
    )
    remote["now"] = NOW + timedelta(seconds=1)
    remote["orders"][0].update(
        status="canceled", canceled_at=remote["now"].isoformat(),
        updated_at=remote["now"].isoformat(),
    )
    report = recover_paper_orders(
        obj, venue, expected_head=report.chain_sha256, clock=lambda: remote["now"],
    )
    assert not report.blockers
    end = CLOSE + timedelta(minutes=5)
    sealed_path = tmp_path / "sealed.jsonl"
    sealed = Journal(sealed_path)
    for line in source.path.read_text().splitlines():
        original = json.loads(line)
        original.pop("seq")
        sealed.submit(original)
    sealed.submit({"kind": "session_end", "ts_utc": end.isoformat()})
    sealed.close()
    audit_path = tmp_path / "sealed-audit.jsonl"
    audit = Journal(audit_path)
    auth = worker._authorization
    audit.submit({
        "kind": "worker_start", "authority": "RESEARCH_ONLY",
        "authorization_sha256": auth.digest, "policy_sha256": POLICY.digest,
        "ledger_head": head,
    })
    audit.submit({
        "kind": "worker_disposition", "authority": "RESEARCH_ONLY", "intent_seq": seq,
        "call_id": "a", "role": "INITIAL", "source_record_sha256": stable_hash(row),
        "disposition": "DURABLE_DISPATCH", "reason": "CANCELLED",
        "elapsed_from_offer_ns": 30_000_000,
    })
    audit.submit({
        "kind": "worker_latency", "authority": "RESEARCH_ONLY", "phase": "dispatch",
        "intent_seq": seq, "elapsed_ns": 20_000_000,
    })
    audit.submit({
        "kind": "worker_latency", "authority": "RESEARCH_ONLY",
        "phase": "offer_to_durable_observed", "intent_seq": seq, "elapsed_ns": 10_000_000,
    })
    audit.submit({
        "kind": "worker_shutdown_final", "authority": "RESEARCH_ONLY", "blockers": [],
        "ledger_head": report.chain_sha256, "ts_utc": end.isoformat(),
    })
    audit.close()
    collected = end + timedelta(minutes=1)
    artifact = tmp_path / "venue-source.json"
    receipt = collect_venue_artifact(
        account_id=ACCOUNT, session_date=DAY, output=artifact,
        transport=_capture_transport(remote, []), clock=lambda: collected,
    )
    assert not receipt.blockers
    collector, reviewer = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({
        "schema": "scorpion.paper-outcome-trust.v1", "authority": "PAPER_ONLY",
        "source_id": "alpaca-equity-paper", "account_ref": ACCOUNT,
        "rules_sha256": RULES.sha256, "registered_ts_utc": (OPEN-timedelta(days=1)).isoformat(),
        "horizon_seconds": 180, "collector_id": "collector", "reviewer_id": "reviewer",
        "collector_public_key": collector.public_key().public_bytes_raw().hex(),
        "reviewer_public_key": reviewer.public_key().public_bytes_raw().hex(),
    }))
    draft = tmp_path / "draft.json"
    trust_pin = hashlib.sha256(trust.read_bytes()).hexdigest()
    kwargs = dict(
        journal=sealed_path, audit_path=audit_path, worker_config=tmp_path / "worker.json",
        worker_pin=auth.digest, risk_path=tmp_path / "risk.json", ledger_path=obj.path,
        ledger_head=report.chain_sha256, trust_path=trust, trust_pin=trust_pin,
        source=artifact, source_pin=receipt.source_artifact_sha256, output=draft,
    )
    from scorpion.paper_campaign_report import build_campaign_report

    ops = build_campaign_report(
        journal=sealed_path, audit_path=audit_path, worker_config=tmp_path / "worker.json",
        worker_pin=auth.digest, risk_path=tmp_path / "risk.json", ledger_path=obj.path,
        ledger_head=report.chain_sha256,
    )
    assert not ops.blockers and ops.dispatch_reservations == 1
    assert not ops.certifies_external_account and not ops.authorizes_orders
    dispatch_timing = next(t for t in ops.timings if t.phase == "dispatch")
    assert dispatch_timing.samples == 1 and dispatch_timing.median_ms == 20
    assert dispatch_timing.p95_ms is None and dispatch_timing.p99_ms is None
    result = prepare_outcome_draft(**kwargs)
    assert result.entry_orders == 1 and result.fills == 0
    assert result.authority == "UNSIGNED_RESEARCH_DRAFT"
    assert obj.status(expected_head=report.chain_sha256).events == report.events
    data = json.loads(draft.read_bytes())
    assert data["reviewed_ts_utc"] is None and data["orders"][0]["state"] == "CANCELLED"
    reviewed = collected + timedelta(minutes=1)
    raw = draft.read_bytes()

    def admission(path, raw):
        return PaperOutcomeAdmission(
            sealed_path, trust, trust_pin, path, artifact,
            collector.sign(b"scorpion.paper-outcome-export.v1:collector\x00"+raw).hex(),
            reviewer.sign(b"scorpion.paper-outcome-export.v1:reviewer\x00"+raw).hex(),
            reviewed,
        )

    with pytest.raises(ValueError):
        admission(draft, raw).verify()
    data["reviewed_ts_utc"] = reviewed.isoformat()
    signed = tmp_path / "reviewed.json"
    signed.write_text(json.dumps(data))
    draft_pin = result.draft_sha256
    result = verify_reviewed_outcomes(
        admission(signed, signed.read_bytes()), draft_path=draft, draft_pin=draft_pin,
    )
    assert result.labels[0].outcome is PaperOutcome.CANCELLED_NO_FILL
    assert result.labels[0].observed_fill_target == 0
    assert not result.authorizes_training

    # Even two valid signatures cannot alter the frozen normalization.
    data["orders"][0]["state"] = "REJECTED"
    changed = tmp_path / "changed-review.json"
    changed.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="modified frozen"):
        verify_reviewed_outcomes(
            admission(changed, changed.read_bytes()), draft_path=draft, draft_pin=draft_pin,
        )


def test_campaign_probe_refuses_even_small_opening_cash_drift(
    tmp_path, ledger, adapter, monkeypatch,
):
    from scorpion.paper_campaign_probe import probe_campaign

    worker, remote, _, _, _ = _worker_fixture(tmp_path, ledger, adapter, monkeypatch)
    worker._audit.close()
    obj, _ = ledger
    _, venue = adapter
    remote["cash"] = "1000.01"
    monkeypatch.setattr("scorpion.paper_campaign_probe.AlpacaEquityPaperVenue", lambda *a, **k: venue)
    monkeypatch.setattr("scorpion.paper_campaign_probe._latch_ok", lambda _: True)
    manifest = tmp_path / "worker.json"
    counter = iter((0, 100_000_000, 100_000_000, 200_000_000))
    result = probe_campaign(
        worker_config=manifest, worker_pin=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        risk_path=tmp_path / "risk.json", ledger_path=obj.path,
        latch_path=tmp_path / "synthetic", samples=1,
        clock=lambda: NOW, monotonic_ns=lambda: next(counter),
    )
    assert result.blockers == ("opening_cash_changed_after_pin",)
    assert remote["posts"] == remote["deletes"] == 0


def test_campaign_timing_percentiles_preserve_sample_resolution():
    from scorpion.paper_campaign_report import timing_distribution

    assert timing_distribution("empty", []).median_ms is None
    short = timing_distribution("short", [i*1_000_000 for i in range(1, 21)])
    assert short.p95_ms == 19 and short.p99_ms is None
    long = timing_distribution("long", [i*1_000_000 for i in range(1, 101)])
    assert long.p99_ms == 99 and long.maximum_ms == 100
    with pytest.raises(ValueError, match="invalid duration"):
        timing_distribution("bad", [-1])
