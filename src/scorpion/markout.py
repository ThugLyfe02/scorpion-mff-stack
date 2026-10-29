"""Mark-out: separating the entry from the exit.

A realised return is one number produced by two decisions. "He made 75%" cannot tell you
whether he was early into a move or disciplined about leaving one, and those two failures have
opposite fixes: a bad entry is a signal problem, a bad exit is a discipline problem. Mark-out
splits the return into the part the entry earned and the part the exit kept.

The decomposition, per trade, over the bar coverage that actually exists:

    entry_drift     premium at +15m vs entry - did the trade work immediately, or was the
                    entry already behind the move
    mfe             the best premium available after entry - what was on the table
    mae             the worst premium available after entry - what he sat through
    realised        the exit, as claimed
    capture         realised gain / mfe gain - the share of the available move he kept
    left_on_table   mfe - realised - what the exit gave back

Two refusals, both learned from the TCA layer and both load-bearing:

* a trade is MEASURABLE only if its entry falls inside the bars. Aggregating a pre-market
  entry against a session that starts at 09:30 produces a statistic about the calendar.
* aggregates are withheld below a floor of 5 measurable trades. Six bar sets cannot support a
  conclusion about a trader's exit discipline, and printing a mean anyway would manufacture
  one. `coverage` reports what was excluded and why, so the gap is visible rather than silent.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

MIN_MEASURABLE = 5
DEFAULT_HORIZONS = (5, 15, 30, 60)


@dataclass(frozen=True, slots=True)
class Bar:
    ts_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class BarSet:
    contract: str
    ticker: str
    side: str
    strike: Decimal
    day: str
    bars: tuple[Bar, ...]

    @property
    def coverage(self) -> tuple[datetime, datetime]:
        return self.bars[0].ts_utc, self.bars[-1].ts_utc

    @property
    def interval_minutes(self) -> int:
        if len(self.bars) < 2:
            return 5
        return int((self.bars[1].ts_utc - self.bars[0].ts_utc).total_seconds() // 60) or 5

    def at_or_before(self, moment: datetime) -> Bar | None:
        chosen: Bar | None = None
        for bar in self.bars:
            if bar.ts_utc <= moment:
                chosen = bar
            else:
                break
        return chosen

    def window(self, start: datetime, end: datetime) -> tuple[Bar, ...]:
        return tuple(b for b in self.bars if start <= b.ts_utc <= end)


@dataclass(frozen=True, slots=True)
class Markout:
    contract: str
    entry_ts_utc: datetime
    entry_px: Decimal
    hours: dict[int, Decimal]
    mfe: Decimal
    mae: Decimal
    exit_px: Decimal | None
    measurable: bool
    reason: str = ""

    @property
    def entry_drift(self) -> Decimal | None:
        mark = self.hours.get(15)
        if mark is None or self.entry_px <= 0:
            return None
        return (mark / self.entry_px - Decimal(1)).quantize(Decimal("0.0001"))

    @property
    def realised(self) -> Decimal | None:
        if self.exit_px is None or self.entry_px <= 0:
            return None
        return (self.exit_px / self.entry_px - Decimal(1)).quantize(Decimal("0.0001"))

    @property
    def available(self) -> Decimal | None:
        """The gain that was on the table, as a fraction of the entry premium."""
        if self.mfe <= self.entry_px:
            return None
        return (self.mfe / self.entry_px - Decimal(1)).quantize(Decimal("0.0001"))

    @property
    def capture(self) -> Decimal | None:
        """Share of the available move that the exit kept, 0..1+."""
        avail = self.available
        realised = self.realised
        if avail is None or avail <= 0 or realised is None:
            return None
        return (realised / avail).quantize(Decimal("0.0001"))

    @property
    def left_on_table(self) -> Decimal | None:
        if self.exit_px is None:
            return None
        return (self.mfe - self.exit_px).quantize(Decimal("0.01"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "entry_ts_utc": self.entry_ts_utc.isoformat(),
            "entry_px": str(self.entry_px),
            "hours": {str(k): str(v) for k, v in sorted(self.hours.items())},
            "mfe": str(self.mfe),
            "mae": str(self.mae),
            "exit_px": str(self.exit_px) if self.exit_px is not None else None,
            "entry_drift": str(self.entry_drift) if self.entry_drift is not None else None,
            "realised": str(self.realised) if self.realised is not None else None,
            "available": str(self.available) if self.available is not None else None,
            "capture": str(self.capture) if self.capture is not None else None,
            "left_on_table": (
                str(self.left_on_table) if self.left_on_table is not None else None
            ),
            "measurable": self.measurable,
            "reason": self.reason,
        }


# ── loading ─────────────────────────────────────────────────────────────────
def _bar_from_row(row: dict[str, str]) -> Bar:
    ts = row["begins_at"].replace("Z", "+00:00")
    return Bar(
        ts_utc=datetime.fromisoformat(ts),
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
    )


def _parse_name(name: str) -> tuple[str, str, Decimal, str] | None:
    """`QQQ_put_710_2026-09-01` -> (QQQ, PUT, 710, 2026-09-01)."""
    try:
        ticker, side, strike, day = name.split("_")
        return ticker.upper(), side.upper(), Decimal(strike), day
    except (ValueError, ArithmeticError):
        return None


def load_bars(path: str | Path) -> dict[str, BarSet]:
    """Load whatever bar files exist, skipping any set that is not real price history.

    The corpus of bar files is mixed: some carry a full session, some a single synthetic bar.
    Only sets with at least two bars and a strictly increasing clock are kept, because a
    one-bar "session" has no path and therefore no MFE.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, BarSet] = {}
    for name, payload in raw.items():
        parsed = _parse_name(name)
        if parsed is None:
            continue
        ticker, side, strike, day = parsed
        bars = tuple(sorted((_bar_from_row(r) for r in payload.get("bars") or []),
                            key=lambda b: b.ts_utc))
        if len(bars) < 2:
            continue
        if any(b.ts_utc >= n.ts_utc for b, n in zip(bars, bars[1:], strict=False)):
            continue
        key = f"{ticker}|{side}|{strike}|{day}"
        out[key] = BarSet(contract=name, ticker=ticker, side=side, strike=strike, day=day,
                          bars=bars)
    return out


def key_for(trade: Any) -> str:
    return f"{trade.ticker}|{trade.side}|{trade.strike}|{trade.day.isoformat()}"


# ── analysis ────────────────────────────────────────────────────────────────
def analyse(
    trade: Any, barset: BarSet | None, *, horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> Markout:
    contract = key_for(trade)
    if barset is None:
        return Markout(contract, trade.entry_ts_utc, trade.entry_bid, {}, Decimal(0),
                       Decimal(0), trade.exit_price, False, "no_bars")

    start, end = barset.coverage
    if not (start <= trade.entry_ts_utc <= end):
        return Markout(contract, trade.entry_ts_utc, trade.entry_bid, {}, Decimal(0),
                       Decimal(0), trade.exit_price, False, "entry_outside_coverage")

    forward = barset.window(trade.entry_ts_utc, end)
    if not forward:
        return Markout(contract, trade.entry_ts_utc, trade.entry_bid, {}, Decimal(0),
                       Decimal(0), trade.exit_price, False, "no_forward_bars")

    marks: dict[int, Decimal] = {}
    for minutes in horizons:
        bar = barset.at_or_before(trade.entry_ts_utc + timedelta(minutes=minutes))
        if bar is not None and bar.ts_utc >= trade.entry_ts_utc:
            marks[minutes] = bar.close

    mfe = max(b.high for b in forward)
    mae = min(b.low for b in forward)
    return Markout(contract, trade.entry_ts_utc, trade.entry_bid, marks, mfe, mae,
                   trade.exit_price, True, "")


def aggregate(markouts: Iterable[Markout], *, floor: int = MIN_MEASURABLE) -> dict[str, Any]:
    rows = [m for m in markouts if m.measurable]
    reasons: dict[str, int] = {}
    for m in markouts:
        if not m.measurable:
            reasons[m.reason] = reasons.get(m.reason, 0) + 1

    captures = [float(m.capture) for m in rows if m.capture is not None]
    drifts = [float(m.entry_drift) for m in rows if m.entry_drift is not None]
    availables = [float(m.available) for m in rows if m.available is not None]
    left = [float(m.left_on_table) for m in rows if m.left_on_table is not None]

    payload: dict[str, Any] = {
        "coverage": {
            "measurable": len(rows),
            "total": len(rows) + sum(reasons.values()),
            "excluded": reasons,
            "floor": floor,
        },
    }
    if len(rows) < floor:
        payload["withheld"] = (
            f"{len(rows)} measurable trades is below the floor of {floor}: an aggregate here "
            "would be a statement about the bar coverage, not about the trader"
        )
        return payload

    payload.update({
        "median_capture": round(statistics.median(captures), 4) if captures else None,
        "mean_capture": round(statistics.fmean(captures), 4) if captures else None,
        "captured_over_half": (
            round(100.0 * sum(1 for c in captures if c >= 0.5) / len(captures), 1)
            if captures else None
        ),
        "median_entry_drift_15m": round(statistics.median(drifts), 4) if drifts else None,
        "median_available": round(statistics.median(availables), 4) if availables else None,
        "median_left_on_table_usd": round(statistics.median(left), 2) if left else None,
        "verdict": _verdict(captures, drifts),
    })
    return payload


def _verdict(captures: Sequence[float], drifts: Sequence[float]) -> str:
    """Name the failure mode the numbers point at, or say they point at nothing."""
    if len(captures) < MIN_MEASURABLE:
        return "insufficient evidence"
    median_capture = statistics.median(captures)
    median_drift = statistics.median(drifts) if drifts else 0.0
    if median_capture < 0.35:
        return "exit_discipline: the majority of the available move is being given back"
    if median_drift < -0.05:
        return "entry_timing: the trade is behind at 15 minutes more often than not"
    if median_capture > 0.7 and median_drift >= 0:
        return "entry_and_exit_both_working"
    return "mixed: no single failure mode dominates"


def entry_clock_profile(ledger: Any) -> dict[str, Any]:
    """Entry times as a histogram, and the gap to the bar coverage.

    A mark-out study can only exist where the clocks agree. If the corpus enters at 06:30 ET
    and the bars begin at 09:30 ET, one of the two timestamps is wrong and every execution
    metric computed from the pair is a statement about the mistake. This makes that visible
    before anyone reads a capture ratio.
    """
    buckets: dict[str, int] = {}
    for trade in ledger.trades:
        hour = trade.ts_utc.hour
        label = f"{hour:02d}:00-{hour:02d}:59"
        buckets[label] = buckets.get(label, 0) + 1
    ordered = dict(sorted(buckets.items()))
    earliest = min((trade.ts_utc for trade in ledger.trades), default=None)
    latest = max((trade.ts_utc for trade in ledger.trades), default=None)
    return {
        "by_hour_utc": ordered,
        "first_entry_utc": earliest.isoformat() if earliest else None,
        "last_entry_utc": latest.isoformat() if latest else None,
        "note": (
            "entries before 13:30 UTC precede the US cash open, so a session-only bar set "
            "cannot mark them; either the entry clock or the bar clock is misaligned"
        ),
    }


def run(ledger: Any, bars: dict[str, BarSet]) -> dict[str, Any]:
    markouts = [analyse(trade, bars.get(key_for(trade))) for trade in ledger.trades]
    return {
        "aggregate": aggregate(markouts),
        "clock": entry_clock_profile(ledger),
        "trades": [m.to_dict() for m in markouts if m.measurable],
        "contracts_in_bars": len(bars),
    }
