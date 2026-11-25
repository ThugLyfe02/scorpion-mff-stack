"""Read-only paper exporter; all transport fixtures are synthetic."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from scorpion_mff.reconcile import ReconcileError, collect_alpaca_paper_export

DAY = date(2026, 11, 24)
NOW = datetime(2026, 11, 24, 20, tzinfo=UTC)


def order() -> dict:
    return {
        "id": "fixture-order", "client_order_id": "intent-id", "asset_class": "us_option",
        "symbol": "QQQ261124C00719000", "type": "limit", "side": "buy", "qty": "2",
        "limit_price": "1.10", "filled_qty": "2", "filled_avg_price": "1.05",
        "status": "filled", "submitted_at": "2026-11-24T15:00:00Z",
        "updated_at": "2026-11-24T15:00:01Z", "order_class": "simple", "legs": None,
    }


def activity() -> dict:
    return {
        "id": "fixture::fill-1", "activity_type": "FILL", "order_id": "fixture-order",
        "symbol": "QQQ261124C00719000", "side": "buy", "qty": "2", "cum_qty": "2",
        "leaves_qty": "0", "price": "1.05", "type": "fill",
        "transaction_time": "2026-11-24T15:00:01Z",
    }


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("SCORPION_ALPACA_PAPER_KEY", "synthetic-paper-key")
    monkeypatch.setenv("SCORPION_ALPACA_PAPER_SECRET", "synthetic-paper-secret")
    state = {
        "orders": [order()], "positions": [], "account": {"id": "paper-id"},
        "fills": [activity()],
    }
    calls = []

    def read(request, timeout):
        calls.append(request.full_url)
        assert request.get_method() == "GET"
        assert request.full_url.startswith("https://paper-api.alpaca.markets/v2/")
        if "/account/activities/FILL?" in request.full_url:
            value = state["fills"]
        elif "/account" in request.full_url:
            value = state["account"]
        elif "/orders?" in request.full_url:
            value = state["orders"]
        else:
            value = state["positions"]
        return json.dumps(value).encode()

    return state, calls, read


def collect(path, read):
    return collect_alpaca_paper_export(
        account_id="paper-id", session_date=DAY, input_sha256="a" * 64,
        capture_end_ts=NOW.timestamp() - 10, destination=path, transport=read, clock=lambda: NOW,
    )


def test_read_only_export_preserves_normalized_order_identity(tmp_path: Path, transport) -> None:
    _, calls, read = transport
    dest = tmp_path / "snapshot"
    result = collect(dest, read)
    assert len(calls) == 8 and result["captured_orders"] == 1
    assert result["captured_fill_activities"] == 1 and result["fill_activity_reconciled"]
    assert not result["certifies_live_fills"] and not result["snapshot_atomic"]
    normalized = json.loads((dest / "normalized.json").read_text())
    assert normalized["orders"][0]["osi"] == "QQQ   261124C00719000"
    assert normalized["orders"][0]["client_id"] == "intent-id"
    assert normalized["orders"][0]["filled_qty"] == 2
    assert normalized["complete_order_export"]
    assert normalized["fill_activities"][0]["fill_id"] == "fixture::fill-1"
    assert (dest / "fill-page-00.json").is_file()


@pytest.mark.parametrize("reason", ["account", "truncated", "unsupported", "changed"])
def test_read_only_export_refuses_unsafe_or_incomplete_scope(tmp_path, transport, reason):
    state, _, read = transport
    if reason == "account":
        state["account"]["id"] = "wrong"
    elif reason == "truncated":
        state["orders"] = [order()] * 500
    elif reason == "unsupported":
        state["orders"][0]["asset_class"] = "us_equity"
    else:
        old = read
        count = 0

        def read(request, timeout):
            nonlocal count
            if "/orders?" in request.full_url:
                count += 1
                if count == 2:
                    state["orders"][0]["status"] = "canceled"
            return old(request, timeout)

    dest = tmp_path / "snapshot"
    with pytest.raises(ReconcileError):
        collect(dest, read)
    assert not dest.exists()


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "cum", "price", "future", "foreign"])
def test_detailed_fill_discrepancy_refuses_export(tmp_path, transport, mutation):
    state, _, read = transport
    if mutation == "missing":
        state["fills"] = []
    elif mutation == "duplicate":
        state["fills"].append(activity())
    elif mutation == "cum":
        state["fills"][0]["cum_qty"] = "1"
    elif mutation == "price":
        state["fills"][0]["price"] = "1.04"
    elif mutation == "future":
        state["fills"][0]["transaction_time"] = "2026-11-24T21:00:00Z"
    else:
        state["fills"][0]["order_id"] = "unknown"
    with pytest.raises(ReconcileError):
        collect(tmp_path / "snapshot", read)
    assert not (tmp_path / "snapshot").exists()


def test_fill_activity_partial_progression_reconciles_exact_weighted_price(tmp_path, transport):
    state, _, read = transport
    first = dict(activity(), id="fixture::partial", qty="1", cum_qty="1",
                 leaves_qty="1", price="1.01", type="partial_fill")
    second = dict(activity(), id="fixture::last", qty="1", price="1.09")
    state["fills"] = [first, second]
    result = collect(tmp_path / "snapshot", read)
    assert result["captured_fill_activities"] == 2


def test_activity_pagination_preserves_full_pages_and_rejects_replayed_token(tmp_path, transport):
    from urllib.parse import parse_qs, urlsplit

    state, _, read = transport
    state["orders"] = [dict(order(), id=f"order-{i}", client_order_id=f"client-{i}")
                       for i in range(101)]
    all_fills = [dict(activity(), id=f"fill::{i}", order_id=f"order-{i}") for i in range(101)]
    tokens = []

    def paged(request, timeout):
        if "/activities/FILL?" not in request.full_url:
            return read(request, timeout)
        params = parse_qs(urlsplit(request.full_url).query)
        token = params.get("page_token", [None])[0]
        tokens.append(token)
        return json.dumps(all_fills[:100] if token is None else all_fills[100:]).encode()

    result = collect(tmp_path / "snapshot", paged)
    assert result["captured_fill_activities"] == 101
    assert tokens == [None, "fill::99", None, "fill::99"]
    assert (tmp_path / "snapshot" / "fill-page-01.json").is_file()

    def replayed(request, timeout):
        if "/activities/FILL?" not in request.full_url:
            return read(request, timeout)
        return json.dumps(all_fills[:100]).encode()

    with pytest.raises(ReconcileError, match="pagination replay"):
        collect(tmp_path / "refused", replayed)
