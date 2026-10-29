"""Coverage: which claims this corpus can support, and exactly which input is missing.

Every analysis in this repository has been answering with a silent asterisk. Mark-out reported 3
measurable trades of 107; TCA reported 2 of 18; attribution ran on 91 of 93. Each refusal was
correct and each was UNDIAGNOSED - the report said "not measurable" and left the reader to guess
whether the data, the clock or the code was at fault.

This module makes the asterisk the product. Every trade gets one of a small closed set of reasons:

    no_bars                 no bar set exists for this contract at all
    multi_session_bars      a set exists but spans more than one session, so its path is two days
                            stapled together and its MFE is meaningless
    entry_before_bars       the entry precedes the first bar; the entry price cannot be marked
    entry_after_bars        the entry follows the last bar (a late alert against a stale file)
    entry_outside_session   the entry is not inside a trading session at all
    no_exit                 neither an exit price nor an exit percentage was recorded
    clock_skew              the bars and the trade are dated differently, which is a clock bug and
                            not a data gap

That last distinction is the point: `no_bars` means fetch data, `clock_skew` means fix a clock, and
conflating them is how a data problem gets misdiagnosed as a strategy problem for a quarter.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .bars import BarSet
from .sessions import CalendarError, et_date, session_for, session_of_safe

SKEW_LIMIT = timedelta(hours=20)


@dataclass(frozen=True, slots=True)
class TradeCoverage:
    identity: str
    channel: str
    contract_key: str
    measurable: bool
    reason: str = ""
    detail: str = ""
    bars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity, "channel": self.channel,
            "contract_key": self.contract_key, "measurable": self.measurable,
            "reason": self.reason, "detail": self.detail, "bars": self.bars,
        }


def contract_key_for(trade: Any) -> str:
    return f"{trade.ticker}|{trade.side}|{trade.strike}|{trade.day.isoformat()}"


def spans_sessions(barset: BarSet) -> str | None:
    """A set is single-session, or this returns why it is not.

    Two shipped sets carry a 35-hour gap: one contract key, two sessions. A mark-out over such a
    set measures an overnight move as if it were intraday, so it is refused rather than smoothed.
    """
    days = {et_date(bar.ts_utc) for bar in barset.bars}
    if len(days) > 1:
        return f"{len(days)} sessions in one set: {', '.join(sorted(d.isoformat() for d in days))}"
    for bar in barset.bars:
        try:
            session = session_of_safe(bar.ts_utc)
        except CalendarError:
            return "bar outside any known calendar year"
        if session is None:
            return f"bar at {bar.ts_utc.isoformat()} falls on a non-trading day"
    return None


def classify(trade: Any, barset: BarSet | None) -> TradeCoverage:
    """One trade against its bars. Pure, total, and never raises."""
    key = contract_key_for(trade)
    base = {"identity": trade.identity, "channel": trade.channel, "contract_key": key}

    if trade.exit_price is None and trade.lifecycle.exit_pct is None:
        return TradeCoverage(**base, measurable=False, reason="no_exit",
                             detail="no exit price or percentage recorded")

    if barset is None:
        return TradeCoverage(**base, measurable=False, reason="no_bars", detail="")

    if len(barset.bars) < 2:
        return TradeCoverage(**base, measurable=False, reason="no_bars",
                             detail=f"{len(barset.bars)} bar(s)")

    if barset.day != trade.day:
        return TradeCoverage(
            **base, measurable=False, reason="clock_skew",
            detail=f"bars dated {barset.day.isoformat()} for a {trade.day.isoformat()} trade",
            bars=len(barset.bars),
        )

    first, last = barset.coverage
    if abs((trade.ts_utc - first).total_seconds()) > SKEW_LIMIT.total_seconds():
        return TradeCoverage(
            **base, measurable=False, reason="clock_skew",
            detail=f"entry {trade.ts_utc:%Y-%m-%dT%H:%M}Z vs bars from {first:%Y-%m-%dT%H:%M}Z",
            bars=len(barset.bars),
        )

    try:
        session = session_for(trade.day)
    except CalendarError:
        session = None
    if session is None:
        return TradeCoverage(**base, measurable=False, reason="entry_outside_session",
                             detail=f"{trade.day.isoformat()} is not a trading day")
    if not session.contains(trade.ts_utc):
        return TradeCoverage(
            **base, measurable=False, reason="entry_outside_session",
            detail=f"entry {trade.ts_utc:%H:%M}Z outside {session.open_utc:%H:%M}-"
                   f"{session.close_utc:%H:%M}Z",
            bars=len(barset.bars),
        )

    if trade.ts_utc < first:
        return TradeCoverage(**base, measurable=False, reason="entry_before_bars",
                             detail=f"entry {trade.ts_utc:%H:%M}Z < first bar {first:%H:%M}Z",
                             bars=len(barset.bars))
    if trade.ts_utc > last:
        return TradeCoverage(**base, measurable=False, reason="entry_after_bars",
                             detail=f"entry {trade.ts_utc:%H:%M}Z > last bar {last:%H:%M}Z",
                             bars=len(barset.bars))

    if (problem := spans_sessions(barset)) is not None:
        return TradeCoverage(**base, measurable=False, reason="multi_session_bars",
                             detail=problem, bars=len(barset.bars))

    return TradeCoverage(**base, measurable=True, bars=len(barset.bars))


def assess(
    ledger: Any, barsets: Mapping[str, BarSet]
) -> tuple[list[TradeCoverage], dict[str, Any]]:
    """Classify every trade and summarise by channel and by reason."""
    rows = [classify(trade, barsets.get(contract_key_for(trade))) for trade in ledger.trades]

    by_channel: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_channel.setdefault(row.channel, {"total": 0, "measurable": 0, "reasons": {}})
        entry["total"] += 1
        if row.measurable:
            entry["measurable"] += 1
        else:
            entry["reasons"][row.reason] = entry["reasons"].get(row.reason, 0) + 1
    for entry in by_channel.values():
        entry["pct"] = (
            round(100.0 * entry["measurable"] / entry["total"], 1) if entry["total"] else 0.0
        )

    reasons: dict[str, int] = {}
    for row in rows:
        if not row.measurable:
            reasons[row.reason] = reasons.get(row.reason, 0) + 1

    measurable = sum(1 for row in rows if row.measurable)
    summary = {
        "trades": len(rows),
        "measurable": measurable,
        "pct": round(100.0 * measurable / len(rows), 1) if rows else 0.0,
        "by_channel": dict(sorted(by_channel.items())),
        "by_reason": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        "contracts_with_bars": len(barsets),
    }
    return rows, summary


def unmeasurable(rows: Iterable[TradeCoverage], reason: str | None = None) -> list[TradeCoverage]:
    return [r for r in rows if not r.measurable and (reason is None or r.reason == reason)]


def to_dict(rows: Iterable[TradeCoverage], summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": summary,
        "unmeasurable": [r.to_dict() for r in rows if not r.measurable],
    }
