"""Execution cost at scale: signed basis points, withheld groups, and honest latency."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scorpion.tca_scale import (
    Cost,
    ScaleError,
    attach_channels,
    from_store,
    group_costs,
    headline,
    render,
    report,
    summarise_costs,
)

D = Decimal
T0 = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)


def cost(limit="1.00", fill="1.02", qty=1, channel="etf-options", at=T0, latency_ms=0):
    return Cost(
        contract_key="QQQ|CALL|719|2026-09-01", limit_price=D(limit), fill_price=D(fill),
        fill_qty=qty, intent_qty=qty, intent_ts=at,
        fill_ts=at + timedelta(milliseconds=latency_ms), channel=channel,
    )


def test_cost_is_signed_and_positive_means_money_lost():
    assert cost(fill="1.02").cost_bps == pytest.approx(200.0)
    assert cost(fill="0.98").cost_bps == pytest.approx(-200.0)


def test_a_zero_limit_is_refused():
    with pytest.raises(ScaleError):
        _ = cost(limit="0").cost_bps


def test_fill_ratio_needs_an_intent():
    partial = Cost("X", D("1"), D("1"), 2, 5, T0, T0)
    assert partial.fill_ratio == 0.4
    unknown = Cost("X", D("1"), D("1"), 2, 0, T0, T0)
    assert unknown.fill_ratio is None


def test_latency_comes_from_the_executor_clock():
    assert cost(latency_ms=250).latency_ms == pytest.approx(250.0)
    assert Cost("X", D("1"), D("1"), 1, 1, None, None).latency_ms is None


def test_a_thin_group_is_withheld():
    assert "withheld" in summarise_costs([cost()])


def test_a_populated_group_reports_cost_and_tail_latency():
    costs = [cost(fill="1.02", latency_ms=ms) for ms in (10, 20, 30, 40, 500)]
    payload = summarise_costs(costs)
    assert payload["n"] == 5
    assert payload["mean_cost_bps"] == pytest.approx(200.0)
    assert payload["worse_than_limit_pct"] == 100.0
    assert payload["latency_p90_ms"] > payload["latency_p50_ms"]


def test_price_improvement_is_kept_not_clipped():
    costs = [cost(fill="0.98") for _ in range(5)]
    assert summarise_costs(costs)["mean_cost_bps"] == pytest.approx(-200.0)


def test_groups_are_withheld_independently():
    costs = [cost(channel="a") for _ in range(5)] + [cost(channel="b")]
    groups = group_costs(costs, by="channel")
    assert "withheld" not in groups["a"] and "withheld" in groups["b"]


def test_grouping_by_hour_uses_the_intent_clock():
    groups = group_costs([cost(at=T0) for _ in range(5)], by="hour")
    assert list(groups) == ["13:00"]


def test_channels_are_attached_by_contract_key():
    ledger = SimpleNamespace(trades=[SimpleNamespace(
        ticker="QQQ", side="CALL", strike=D("719"), day=datetime(2026, 9, 1).date(),
        channel="etf-options",
    )])
    attached = attach_channels([cost()], ledger)
    assert attached[0].channel == "etf-options"


def test_an_unmatched_execution_is_named_not_dropped():
    ledger = SimpleNamespace(trades=[])
    assert attach_channels([cost()], ledger)[0].channel == "unmatched"


def test_reports_render_and_summarise():
    payload = report(_store_with([cost() for _ in range(5)]))
    assert "executions" in render(payload)
    assert headline(payload)["measured"] is True


def _store_with(rows):
    class FakeStore:
        def load_executions(self):
            return [
                {
                    "contract_key": r.contract_key, "limit_price": str(r.limit_price),
                    "fill_price": str(r.fill_price), "fill_qty": r.fill_qty,
                    "intent_ts_utc": r.intent_ts.isoformat() if r.intent_ts else None,
                    "fill_ts_utc": r.fill_ts.isoformat() if r.fill_ts else None,
                }
                for r in rows
            ]

    return FakeStore()


def test_from_store_skips_malformed_rows():
    class BadStore:
        def load_executions(self):
            return [{"contract_key": "X", "limit_price": "0", "fill_price": "1",
                     "fill_qty": 1, "intent_ts_utc": None, "fill_ts_utc": None}]

    assert from_store(BadStore()) == []
