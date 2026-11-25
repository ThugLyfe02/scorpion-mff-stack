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


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("SCORPION_ALPACA_PAPER_KEY", "synthetic-paper-key")
    monkeypatch.setenv("SCORPION_ALPACA_PAPER_SECRET", "synthetic-paper-secret")
    state = {"orders": [order()], "positions": [], "account": {"id": "paper-id"}}
    calls = []

    def read(request, timeout):
        calls.append(request.full_url)
        assert request.get_method() == "GET"
        assert request.full_url.startswith("https://paper-api.alpaca.markets/v2/")
        if "/account" in request.full_url:
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
    assert len(calls) == 6 and result["captured_orders"] == 1
    assert not result["certifies_live_fills"] and not result["snapshot_atomic"]
    normalized = json.loads((dest / "normalized.json").read_text())
    assert normalized["orders"][0]["osi"] == "QQQ   261124C00719000"
    assert normalized["orders"][0]["client_id"] == "intent-id"
    assert normalized["orders"][0]["filled_qty"] == 2
    assert normalized["complete_order_export"]


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
