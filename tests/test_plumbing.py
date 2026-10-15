"""Sizing, fill log, kill switch and health - the plumbing that must not lie."""

from __future__ import annotations

import json

import pytest

from scorpion_mff.fills import FillLog
from scorpion_mff.health import Heartbeat, LatencyBudget
from scorpion_mff.killswitch import KillSwitch
from scorpion_mff.sizing import (
    ADD_CLIP_USD,
    BASE_CLIP_USD,
    HIGH_CONF_ADD_USD,
    HIGH_CONF_BASE_USD,
    MAX_CONCURRENT,
    Tier,
    clip_budget,
    contracts_for,
    pipe_test_size,
    tier_for_channel,
)


# ── sizing ───────────────────────────────────────────────────────────────────
def test_the_clip_table_is_exactly_what_the_lock_file_says():
    assert (BASE_CLIP_USD, ADD_CLIP_USD) == (1500.0, 2250.0)
    assert (HIGH_CONF_BASE_USD, HIGH_CONF_ADD_USD) == (3000.0, 4500.0)
    assert MAX_CONCURRENT == 2
    assert pipe_test_size() == 1


def test_tier_for_channel():
    assert tier_for_channel("etf-options", is_first_of_day=True) is Tier.PIPE
    assert tier_for_channel("options-king", is_first_of_day=False) is Tier.STANDARD
    assert tier_for_channel("platinum-options", is_first_of_day=False) is Tier.STANDARD
    assert tier_for_channel("high-confidence-options", is_first_of_day=False) is Tier.HIGH_CONFIDENCE


def test_clip_budget_add_and_base():
    assert clip_budget(Tier.STANDARD) == 1500.0
    assert clip_budget(Tier.STANDARD, is_add=True) == 2250.0
    assert clip_budget(Tier.HIGH_CONFIDENCE) == 3000.0
    assert clip_budget(Tier.HIGH_CONFIDENCE, is_add=True) == 4500.0
    with pytest.raises(ValueError):
        clip_budget(Tier.PIPE)


def test_contracts_for_floors_and_never_silently_drops_to_zero():
    assert contracts_for(1500, 1.00) == 15
    assert contracts_for(1500, 2.72) == 5          # 1500 / 272 = 5.51
    assert contracts_for(1500, 20.00) == 1         # tiny clip still trades 1
    assert contracts_for(0, 5.0) == 1
    assert contracts_for(0, 5.0, minimum=0) == 0


@pytest.mark.parametrize("price", [0, -1])
def test_contracts_for_rejects_nonsense_prices(price):
    with pytest.raises(ValueError):
        contracts_for(1500, price)


# ── fill log ─────────────────────────────────────────────────────────────────
def test_fill_log_writes_one_json_line_per_decision(tmp_path):
    path = tmp_path / "fills.jsonl"
    log = FillLog(path=path)
    log.record(event="buy", contract_key="QQQ|CALL|719|0DTE", qty=1, price=1.05,
               reason="entry:pipe", ts=1000.0, latency_ms=12)
    log.record(event="sell", contract_key="QQQ|CALL|719|0DTE", qty=1, price=1.11,
               reason="close:pct=10", ts=1360.0, latency_ms=9)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "buy" and first["seq"] == 1 and first["qty"] == 1
    assert [f.seq for f in log.records] == [1, 2]
    assert log.max_latency_ms() == 12

    reread = FillLog().replay_file(path)
    assert [f.event for f in reread] == ["buy", "sell"]


def test_fill_log_reasons_counts_only_refusals():
    log = FillLog()
    log.record(event="buy", contract_key="k", qty=1, price=1, reason="entry")
    log.record(event="skip", contract_key="k", qty=0, price=0, reason="max_concurrent:2", accepted=False)
    log.record(event="skip", contract_key="k", qty=0, price=0, reason="max_concurrent:2", accepted=False)
    log.record(event="reject", contract_key="k", qty=0, price=0, reason="no_quote", accepted=False)
    assert log.reasons() == {"max_concurrent:2": 2, "no_quote": 1}


def test_fill_log_path_is_optional():
    log = FillLog()
    log.record(event="buy", contract_key="k", qty=1, price=1, reason="entry")
    assert log.records[0].seq == 1


# ── kill switch ──────────────────────────────────────────────────────────────
def test_kill_switch_arms_and_stops(tmp_path):
    path = tmp_path / "NO_TRADE"
    switch = KillSwitch(path=path)
    assert not switch.active()
    assert "ARMED" in switch.status()
    switch.stand_down("test")
    assert switch.active()
    assert "STOPPED" in switch.status()
    switch.clear()
    assert not switch.active()


def test_kill_switch_fails_closed_when_the_path_is_unknowable(tmp_path, monkeypatch):
    switch = KillSwitch(path=tmp_path / "NO_TRADE")

    def boom(self):                      # a permission error is not a green light
        raise OSError("access denied")

    monkeypatch.setattr(type(switch.path), "exists", boom)
    assert switch.active() is True


def test_kill_switch_caches_within_the_window(tmp_path):
    switch = KillSwitch(path=tmp_path / "NO_TRADE", clock=lambda: 1000.0)
    assert switch.active(max_age_s=5) is False
    switch.path.write_text("stop", encoding="utf-8")
    assert switch.active(max_age_s=5) is False      # cached
    assert switch.active() is True                  # uncached re-stat sees it


# ── health ───────────────────────────────────────────────────────────────────
def test_latency_budget_flags_a_breach():
    budget = LatencyBudget(budget_ms=10_000)
    assert budget.record(120) is True
    assert budget.record(9_999) is True
    assert budget.record(10_001) is False
    assert budget.breaches == 1
    assert budget.worst_ms == 10_001
    assert budget.summary()["breaches"] == 1


def test_latency_percentile_is_bounded():
    budget = LatencyBudget()
    for v in (10, 20, 30, 40):
        budget.record(v)
    assert budget.percentile(50) in (20, 30)
    assert budget.percentile(95) == 40
    assert LatencyBudget().percentile(95) == 0


def test_heartbeat_goes_stale_after_the_gap(tmp_path):
    hb = Heartbeat(path=tmp_path / "hb.json", gap_s=30.0, clock=lambda: 1000.0)
    hb.beat()
    assert not hb.stale()
    assert hb.stale(now=1031.0)          # 31s without a beat
    assert hb.staleness_s(now=1010.0) == pytest.approx(10.0)
    assert json.loads((tmp_path / "hb.json").read_text(encoding="utf-8"))["beats"] == 1


def test_heartbeat_write_failure_does_not_raise(tmp_path, monkeypatch):
    hb = Heartbeat(path=tmp_path / "hb.json")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(type(hb.path), "write_text", boom)
    hb.beat()                            # must not raise: a watchdog file is not the trade path
