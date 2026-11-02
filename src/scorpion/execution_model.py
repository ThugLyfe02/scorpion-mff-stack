"""Single implementation of the frozen forward rules (fwd-v2-FINAL) for the OK stock trade.

The live paper session uses :func:`entry_schedule` to time its limit, chase and skip decisions,
and the replay/backtest uses :func:`evaluate_call` on 1-minute bars. Both read every rule value
from :class:`scorpion.config.ForwardRules` (live/RULES.lock.md); nothing here hard-codes a rule.

Bar arithmetic is float64 on purpose: it reproduces the frozen research logger
(``fwd_logger_v2.py``) bit-for-bit, which is the parity proof against the edge_study results.
Order prices sent to a broker are Decimal and tick-rounded in :mod:`scorpion.pricing`.
"""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum

from .config import MARKET_TZ, ForwardRules


class ChaseModel(StrEnum):
    # oos128/oos130/fwd-v2: an unfilled limit is chased once and assumed filled at the high of
    # minute i0+window (conservative; this is the frozen forward-log rule).
    MINUTE_HIGH = "minute_high"
    # Sensitivity model: the chase is a new limit at the open of minute i0+window that must trade
    # through by trade_through_bps before post+skip_if_seen_after_min, otherwise the call is skipped.
    LIMIT_THROUGH = "limit_through"


class Track(StrEnum):
    MAIN = "main"
    SHADOW_POST_1100 = "shadow_post1100"


@dataclass(frozen=True, slots=True)
class MinuteBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise ValueError("bar timestamp must be timezone-aware")


class DayBars:
    """One symbol's 1-minute bars for one ET calendar day, sorted, first duplicate wins."""

    __slots__ = ("_all", "_all_keys", "_session", "_session_keys")

    def __init__(self, bars: Sequence[MinuteBar], *, session_open: time, session_last: time):
        seen: set[int] = set()
        unique: list[MinuteBar] = []
        for bar in sorted(bars, key=lambda item: item.ts):
            key = int(bar.ts.timestamp())
            if key in seen:
                continue
            seen.add(key)
            unique.append(bar)
        self._all = tuple(unique)
        self._all_keys = tuple(int(bar.ts.timestamp()) for bar in self._all)
        self._session = tuple(
            bar
            for bar in self._all
            if session_open <= bar.ts.astimezone(MARKET_TZ).time() <= session_last
        )
        self._session_keys = tuple(int(bar.ts.timestamp()) for bar in self._session)

    @property
    def session(self) -> tuple[MinuteBar, ...]:
        return self._session

    def open_at(self, ts: datetime) -> float | None:
        key = int(ts.timestamp())
        index = bisect.bisect_left(self._all_keys, key)
        if index < len(self._all_keys) and self._all_keys[index] == key:
            return self._all[index].open
        return None

    def session_index(self, ts: datetime) -> int:
        """Left insertion point of ``ts`` among session bars (pandas searchsorted)."""

        return bisect.bisect_left(self._session_keys, int(ts.timestamp()))


BarStore = Mapping[str, Mapping[str, DayBars]]


@dataclass(frozen=True, slots=True)
class CallSpec:
    date: str
    time_et: str
    ticker: str
    side: str
    strike: str = ""
    expiry: str = ""
    seen_time_et: str = ""
    channel: str = "options-king"
    actual_fill: float | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> CallSpec:
        actual = raw.get("actual_fill")
        return cls(
            date=str(raw["date"]),
            time_et=str(raw["time_et"]),
            ticker=str(raw["ticker"]),
            side=str(raw["side"]),
            strike=str(raw.get("strike", "")),
            expiry=str(raw.get("expiry", "")),
            seen_time_et=str(raw.get("seen_time_et", "") or ""),
            channel=str(raw.get("channel", "options-king")),
            actual_fill=None if actual in (None, "") else float(str(actual)),
        )

    @property
    def call_id(self) -> str:
        # Identical to fwd_logger_v2.py so rows join across loggers on call_id.
        material = "|".join(
            (self.date, self.time_et, self.ticker, self.side, self.strike, self.expiry)
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def post_ts(self) -> datetime:
        return _et(self.date, self.time_et)

    def seen_ts(self, rules: ForwardRules) -> datetime:
        if self.seen_time_et:
            return _et(self.date, self.seen_time_et)
        return self.post_ts() + timedelta(minutes=rules.entry_delay_min)


def _et(day: str, hhmm: str) -> datetime:
    parts = [int(part) for part in hhmm.split(":")]
    year, month, dom = (int(part) for part in day.split("-"))
    return datetime(
        year, month, dom, parts[0], parts[1], parts[2] if len(parts) > 2 else 0, tzinfo=MARKET_TZ
    )


def track_for(post_ts: datetime, rules: ForwardRules) -> Track:
    local = post_ts.astimezone(MARKET_TZ).time()
    return Track.SHADOW_POST_1100 if local >= rules.shadow_track_from_et else Track.MAIN


@dataclass(frozen=True, slots=True)
class EntrySchedule:
    post_ts: datetime
    seen_ts: datetime
    entry_ts: datetime
    chase_ts: datetime
    skip_ts: datetime
    exit_ts: datetime
    late: bool


def entry_schedule(post_ts: datetime, seen_ts: datetime, rules: ForwardRules) -> EntrySchedule:
    """Wall-clock schedule the live session follows (R1-R4)."""

    earliest = post_ts + timedelta(minutes=rules.entry_delay_min)
    entry = max(seen_ts, earliest)
    return EntrySchedule(
        post_ts=post_ts,
        seen_ts=seen_ts,
        entry_ts=entry,
        chase_ts=entry + timedelta(minutes=rules.limit_window_min),
        skip_ts=post_ts + timedelta(minutes=rules.skip_if_seen_after_min),
        exit_ts=entry + timedelta(minutes=rules.hold_min),
        late=(seen_ts - post_ts) > timedelta(minutes=rules.skip_if_seen_after_min),
    )


def tier_for(price: float, rules: ForwardRules) -> str:
    return "sub20" if price < float(rules.main_tier_below_usd) else "20plus"


Outcome = dict[str, object]


def evaluate_call(
    call: CallSpec,
    bars: BarStore,
    rules: ForwardRules,
    *,
    chase_model: ChaseModel = ChaseModel.MINUTE_HIGH,
) -> Outcome:
    """Apply R1-R5/R7 to one call. Returns fwd-v2 rule columns (keys match the frozen log)."""

    row: Outcome = {
        "date": call.date,
        "post_time_et": call.time_et,
        "ticker": call.ticker,
        "side": call.side,
        "strike": call.strike,
        "expiry": call.expiry,
        "seen_time_et": call.seen_time_et,
        "call_id": call.call_id,
    }
    ticker = call.ticker
    if call.side != rules.side:
        return row | {"skip_reason": "not_call"}
    if ticker in rules.excluded_tickers:
        return row | {"skip_reason": "excluded_ticker_" + ticker}
    if ticker not in bars:
        return row | {"skip_reason": "no_cached_bars"}
    day = bars[ticker].get(call.date)
    session = day.session if day is not None else ()
    if day is None or len(session) < rules.min_session_bars:
        return row | {"skip_reason": "thin_day_lt250_bars"}
    post = call.post_ts()
    latest = _et(call.date, rules.latest_post_et.strftime("%H:%M"))
    if post + timedelta(minutes=1) > latest:
        return row | {"skip_reason": "post_after_1459"}
    schedule = entry_schedule(post, call.seen_ts(rules), rules)
    lag = (schedule.seen_ts - post).total_seconds() / 60
    row["lag_min"] = lag
    if schedule.late:
        return row | {"skip_reason": "late_gt15min"}
    cap_index = min(
        day.session_index(_et(call.date, rules.exit_cap_et.strftime("%H:%M"))), len(session) - 1
    )
    i0 = day.session_index(schedule.entry_ts)
    window = rules.limit_window_min
    if i0 + 6 >= cap_index - 1:
        return row | {"skip_reason": "too_close_to_1555"}
    limit = session[i0].open
    lows = [bar.low for bar in session[i0 : i0 + window]]
    through = rules.trade_through_factor
    filled = any(low <= limit * through for low in lows)
    if filled:
        price, entry_bar, fill_type = limit, session[i0], "limit"
    elif chase_model is ChaseModel.MINUTE_HIGH:
        price, entry_bar = session[i0 + window].high, session[i0 + window]
        fill_type = f"chase_at_high_i0+{window}"
    else:
        chase_limit = session[i0 + window].open
        deadline = day.session_index(schedule.skip_ts)
        hit = next(
            (
                k
                for k in range(i0 + window, max(i0 + window + 1, deadline))
                if session[k].low <= chase_limit * through
            ),
            None,
        )
        if hit is None:
            return row | {"skip_reason": "chase_unfilled_by_skip_deadline", "limit_px": limit}
        price, entry_bar, fill_type = chase_limit, session[hit], "chase_limit_through"
    exit_index = min(i0 + rules.hold_min, cap_index)
    exit_bar = session[exit_index]
    exit_price = exit_bar.open
    return row | {
        "skip_reason": "",
        "counted": 1,
        "entry_minute": _pd_str(session[i0].ts),
        "limit_px": limit,
        "window_lows": ";".join(f"{low:.4f}" for low in lows),
        "fill_type": fill_type,
        "entry_ts": _pd_str(entry_bar.ts),
        "entry_px": price,
        "exit_ts": _pd_str(exit_bar.ts),
        "exit_px": exit_price,
        "exit_type": "time_60m" if exit_index == i0 + rules.hold_min else "cap_1555",
        "target": "none",
        "trail": "none",
        "tier": tier_for(price, rules),
        "net_pct": (exit_price / price - 1) * 100 - rules.cost_pct,
        "_close_px": session[cap_index].open,
        "_entry_index": i0,
    }


def _pd_str(ts: datetime) -> str:
    """``str(pd.Timestamp)`` formatting, so CSV cells match the frozen logger."""

    return ts.astimezone(MARKET_TZ).isoformat(sep=" ")
