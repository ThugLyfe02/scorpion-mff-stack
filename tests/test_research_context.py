"""The MFF side of the research-context contract: verify, join, never gate."""

from __future__ import annotations

import calendar
import json
import time
from pathlib import Path

import pytest

from scorpion_mff import context as rc
from scorpion_mff.evidence import Trade

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "research-context-2026-09-11.json"


def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def ts(day: str, hour: int = 14) -> float:
    return calendar.timegm(time.strptime(f"{day} {hour:02d}:00", "%Y-%m-%d %H:%M"))


# ── verification ─────────────────────────────────────────────────────────────
def test_the_producer_fixture_verifies_and_carries_the_expected_facts():
    ctx = rc.load(FIXTURE)
    assert ctx.as_of == "2026-09-11"
    assert ctx.count == 10
    acva = ctx.get("acva")
    assert acva is not None
    assert acva.buyout_at_deal is True
    assert acva.disposition == "SKIP"
    assert acva.tape_flag == "FADE"
    aent = ctx.get("AENT")
    assert aent is not None and aent.disposition == "GO"
    assert aent.buyout_at_deal is False


def test_the_checksum_matches_the_producer_canonicalisation():
    raw = payload()
    assert raw["checksum"] == rc.checksum_for(raw)


def test_a_changed_fact_breaks_verification():
    raw = payload()
    raw["symbols"][0]["halt_risk"] = not raw["symbols"][0]["halt_risk"]
    with pytest.raises(rc.ContextError, match="checksum mismatch"):
        rc.verify(raw)


def test_authority_is_enforced_even_after_a_rehash():
    raw = payload()
    raw["authority"] = "LIVE"
    raw["checksum"] = rc.checksum_for(raw)
    with pytest.raises(rc.ContextError, match="authority"):
        rc.verify(raw)


def test_unknown_schema_is_rejected():
    raw = payload()
    raw["schema"] = "scorpion.research-context.v2"
    raw["checksum"] = rc.checksum_for(raw)
    with pytest.raises(rc.ContextError, match="schema"):
        rc.verify(raw)


def test_duplicate_symbols_are_rejected():
    raw = payload()
    raw["symbols"].append(dict(raw["symbols"][0]))
    raw["checksum"] = rc.checksum_for(raw)
    with pytest.raises(rc.ContextError, match="duplicate symbol"):
        rc.verify(raw)


def test_as_of_and_generated_at_are_validated():
    raw = payload()
    raw["as_of"] = "not-a-date"
    raw["checksum"] = rc.checksum_for(raw)
    with pytest.raises(rc.ContextError, match="as_of"):
        rc.verify(raw)

    raw = payload()
    raw["generated_at_utc"] = "2026-09-12T00:00:00Z"
    raw["checksum"] = rc.checksum_for(raw)
    with pytest.raises(rc.ContextError, match="fall on as_of"):
        rc.verify(raw)


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(rc.ContextError, match="not found"):
        rc.load(tmp_path / "missing.json")


# ── attribution ──────────────────────────────────────────────────────────────
def test_et_session_date_is_not_the_utc_date():
    # 2026-09-12 01:00 UTC is still the 2026-09-11 ET session.
    utc = calendar.timegm(time.strptime("2026-09-12 01:00", "%Y-%m-%d %H:%M"))
    assert rc.et_date(utc) == "2026-09-11"


def test_join_groups_flagged_symbols_and_ignores_unrelated_ones():
    ctx = rc.load(FIXTURE)
    trades = [
        Trade(pnl_usd=100.0, ts=ts("2026-09-11"), ticker="ACVA", channel="x"),
        Trade(pnl_usd=-50.0, ts=ts("2026-09-11"), ticker="AENT", channel="x"),
        Trade(pnl_usd=25.0, ts=ts("2026-09-11"), ticker="QQQ", channel="x"),
        Trade(pnl_usd=999.0, ts=ts("2026-09-12"), ticker="ACVA", channel="x"),
        Trade(pnl_usd=500.0, ts=ts("2026-09-11"), ticker="ORCL", channel="x"),
    ]
    attr = rc.attribute(trades, ctx)
    assert attr.matched == 2
    assert attr.unmatched == 3   # QQQ, next-day ACVA, and a CONTEXT row (ORCL)
    groups = {(g.name, g.value): g for g in attr.groups}
    assert groups[("buyout_at_deal", "true")].n == 1
    assert groups[("buyout_at_deal", "true")].mean_usd == pytest.approx(100.0)
    assert groups[("tape_flag", "FADE")].n == 1
    assert groups[("tape_flag", "GO")].n == 1


def test_a_context_with_no_overlap_says_so():
    ctx = rc.load(FIXTURE)
    attr = rc.attribute([Trade(pnl_usd=10.0, ts=ts("2026-09-11"), ticker="NVDA")], ctx)
    assert attr.matched == 0
    assert attr.groups == []
    assert "no overlapping symbol" in rc.attribution_text(attr)


def test_attribution_text_is_explicitly_research_only():
    ctx = rc.load(FIXTURE)
    attr = rc.attribute([Trade(pnl_usd=100.0, ts=ts("2026-09-11"), ticker="ACVA")], ctx)
    text = rc.attribution_text(attr)
    assert "buyout_at_deal=true" in text
    assert "does not size or gate" in text
