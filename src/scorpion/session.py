"""Session structure: the open is not the afternoon.

Every measurement here treats a trading day as one object. It is not. A 0DTE long entered at
09:35 and the same contract entered at 14:30 are different trades: the first has six hours for the
thesis to resolve, the second has ninety minutes and a decay curve that steepens into the close.

Grouping by time-of-day is the cheapest possible way to expose that, and it needs no new data - the
corpus already carries entry timestamps in Eastern time. What it usually shows is that one part of
session carries the whole edge, which changes sizing, changes which alerts are worth acting on, and
changes what an "average trade" means.

Buckets are fixed edges, not quantiles, so the same trade lands in the same bucket forever:

    09:30-10:00  open       the first thirty minutes, widest spreads, largest true range
    10:00-12:00  morning    the trend window
    12:00-14:00  midday     thinnest liquidity, most mean reversion
    14:00-15:30  afternoon  positioning into the close
    15:30-16:00  close      gamma concentration, pinning pressure
    outside      pre/post   recorded and reported separately, never pooled with the open

A bucket below `min_sample` reports its counts and withholds its averages, because a
mean over two trades is a story rather than a measurement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

OPEN = time(9, 30)
MORNING = time(10, 0)
MIDDAY = time(12, 0)
AFTERNOON = time(14, 0)
CLOSE = time(15, 30)
SETTLE = time(16, 0)

BUCKETS: tuple[tuple[str, time, time], ...] = (
    ("open", OPEN, MORNING),
    ("morning", MORNING, MIDDAY),
    ("midday", MIDDAY, AFTERNOON),
    ("afternoon", AFTERNOON, CLOSE),
    ("close", CLOSE, SETTLE),
)

MIN_SAMPLE = 5


class SessionError(ValueError):
    """A session question that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def bucket(moment: datetime) -> str:
    """Which part of the session a timestamp falls in, by fixed edges."""
    clock = moment.time()
    for name, start, end in BUCKETS:
        if start <= clock < end:
            return name
    return "outside"


def minutes_since_open(moment: datetime) -> float:
    """Minutes from the 09:30 open; negative before it, so a pre-market entry is visibly early."""
    anchor = datetime.combine(moment.date(), OPEN)
    if moment.tzinfo is not None and anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=moment.tzinfo)
    return (moment - anchor).total_seconds() / 60.0


@dataclass
class Bucket:
    name: str
    trades: int = 0
    wins: int = 0
    r_total: float = 0.0
    usd_total: float = 0.0

    @property
    def mean_r(self) -> float:
        return self.r_total / self.trades if self.trades else 0.0

    @property
    def win_rate(self) -> float:
        """Share of trades that finished positive - the only definition used here."""
        return self.wins / self.trades if self.trades else 0.0

    def to_dict(self, *, minimum: int = MIN_SAMPLE) -> dict[str, Any]:
        enough = self.trades >= minimum
        return {
            "bucket": self.name,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 4) if enough else None,
            "mean_r": round(self.mean_r, 4) if enough else None,
            "r_total": round(self.r_total, 4),
            "usd_total": round(self.usd_total, 2),
            "decisive": enough,
        }


@dataclass
class Summary:
    buckets: dict[str, Bucket] = field(default_factory=dict)
    trades: int = 0

    def best(self, *, minimum: int = MIN_SAMPLE) -> str | None:
        eligible = [
            b for b in self.buckets.values() if b.trades >= minimum
        ]
        if not eligible:
            return None
        return max(eligible, key=lambda b: b.mean_r).name

    def to_dict(self) -> dict[str, Any]:
        ordered = [self.buckets[name].to_dict() for name, _s, _e in BUCKETS
                   if name in self.buckets]
        outside = self.buckets.get("outside")
        if outside is not None:
            ordered.append(outside.to_dict())
        return {
            "trades": self.trades,
            "best_bucket": self.best(),
            "buckets": ordered,
            "caveats": [
                "fixed edges mean a bucket's result is comparable across months; quantile edges "
                "would move and make every comparison a new question",
                "a bucket below the minimum reports no mean, because two trades is a story",
                "outside-session trades are never pooled with the open",
            ],
        }


def _parse(row: Mapping[str, Any]) -> datetime:
    raw = row.get("entry_ts")
    if raw is None:
        raise SessionError("no_timestamp", "entry_ts missing")
    if isinstance(raw, datetime):
        return raw
    text = str(raw).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError as error:
        raise SessionError("bad_timestamp", f"{raw}: {error}") from error


def summarise(rows: Sequence[Mapping[str, Any]]) -> Summary:
    """Group by session bucket; totals are preserved even where means are withheld."""
    summary = Summary()
    for row in rows:
        moment = _parse(row)
        name = bucket(moment)
        entry = summary.buckets.setdefault(name, Bucket(name))
        entry.trades += 1
        summary.trades += 1
        value = float(row.get("realized_r") or 0.0)
        entry.r_total += value
        if value > 0:
            entry.wins += 1
        entry.usd_total += float(row.get("realized_usd") or 0.0)
    return summary


def session_date(row: Mapping[str, Any]) -> date:
    return _parse(row).date()


def render(summary: Summary) -> str:
    payload = summary.to_dict()
    lines = [f"{payload['trades']} trades; best bucket {payload['best_bucket']}"]
    for row in payload["buckets"]:
        if row["decisive"]:
            lines.append(
                f"  {row['bucket']:10s} n={row['trades']:3d} win {row['win_rate']:.1%} "
                f"mean {row['mean_r']:+.3f}R  {row['usd_total']:+,.0f}"
            )
        else:
            lines.append(f"  {row['bucket']:10s} n={row['trades']:3d} withheld")
    return "\n".join(lines)
