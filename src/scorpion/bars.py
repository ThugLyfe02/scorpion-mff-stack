"""Bars: the price history everything else is measured against.

Three analyses in this repository currently withhold their answers - mark-out at 3 of 107
measurable trades, TCA at 2 of 18, attribution on 91 of 93 - and all three withhold for the same
reason: there is no trustworthy bar store to join against. A research desk that cannot say what a
contract did after entry cannot say anything about a trader's timing, so this module is not an
optimisation. It is the wall the rest of the work is against.

What it refuses, and why each refusal is the feature:

* a bar set whose clock does not strictly increase (a duplicated or rewound timestamp makes MFE
  and MAE nonsense, and both are computed from exactly these numbers);
* a bar whose high does not bracket open and close, or whose low exceeds either - vendor data
  arrives corrupted more often than anyone expects, and a bad high silently inflates every
  captured-move statistic downstream;
* a price that is zero or negative, because a zero high looks like a contract that died when the
  file was simply wrong;
* a session shorter than `min_bars`, because one bar has no path and therefore no MFE - the
  loader in `markout.py` already skips these, and doing it at ingestion means the count is
  reported rather than discovered later.

What it does NOT do: assume a timezone. The corpus taught this the hard way - a column labelled
`time_et` was Pacific, and a naive offset shifted every join by three hours. Bar timestamps must
carry an explicit offset or the set is rejected, and the rejection says so.

Storage is the same SQLite file as the applied log, so a mark-out query is one read against one
database rather than a join across a directory of JSON files whose naming schemes disagree.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

D = Decimal
DEFAULT_MIN_BARS = 2
DEFAULT_MAX_GAP_MINUTES = 30


class BarsError(ValueError):
    """A bar set that cannot be trusted, with a stable code for aggregation."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Bar:
    ts_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_utc": self.ts_utc.isoformat(),
            "open": str(self.open), "high": str(self.high),
            "low": str(self.low), "close": str(self.close),
            "volume": self.volume,
        }


@dataclass(frozen=True, slots=True)
class BarSet:
    """One contract's path, validated and canonical."""

    ticker: str
    side: str
    strike: Decimal
    day: date
    bars: tuple[Bar, ...]
    source: str = ""

    @property
    def contract_key(self) -> str:
        return f"{self.ticker}|{self.side}|{self.strike}|{self.day.isoformat()}"

    @property
    def coverage(self) -> tuple[datetime, datetime]:
        return self.bars[0].ts_utc, self.bars[-1].ts_utc

    @property
    def interval_minutes(self) -> int:
        if len(self.bars) < 2:
            return 0
        return int((self.bars[1].ts_utc - self.bars[0].ts_utc).total_seconds() // 60)

    @property
    def gaps(self) -> tuple[int, ...]:
        """Minutes between consecutive bars that exceed the nominal interval."""
        if len(self.bars) < 2:
            return ()
        nominal = self.interval_minutes
        return tuple(
            int((b.ts_utc - a.ts_utc).total_seconds() // 60) - nominal
            for a, b in zip(self.bars, self.bars[1:], strict=False)
            if int((b.ts_utc - a.ts_utc).total_seconds() // 60) > nominal
        )

    @property
    def session_minutes(self) -> int:
        return int((self.bars[-1].ts_utc - self.bars[0].ts_utc).total_seconds() // 60)


# ── parsing ─────────────────────────────────────────────────────────────────
def _decimal(value: Any, *, field_name: str, ts: str = "") -> Decimal:
    try:
        parsed = D(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise BarsError("bad_price", f"{field_name}={value!r} at {ts}") from exc
    if not parsed.is_finite():
        raise BarsError("bad_price", f"{field_name}={value!r} at {ts}")
    return parsed


def _timestamp(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise BarsError("bad_timestamp", f"{raw!r} is not a string")
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BarsError("bad_timestamp", raw) from exc
    if parsed.tzinfo is None:
        # The corpus taught this: an unlabelled clock is not UTC, it is unknown.
        raise BarsError("naive_timestamp", f"{raw} carries no offset")
    return parsed.astimezone(UTC)


def parse_bar(row: dict[str, Any]) -> Bar:
    ts = _timestamp(row.get("begins_at") or row.get("ts") or row.get("t"))
    open_ = _decimal(row.get("open"), field_name="open", ts=ts.isoformat())
    high = _decimal(row.get("high"), field_name="high", ts=ts.isoformat())
    low = _decimal(row.get("low"), field_name="low", ts=ts.isoformat())
    close = _decimal(row.get("close"), field_name="close", ts=ts.isoformat())

    for name, value in (("open", open_), ("high", high), ("low", low), ("close", close)):
        if value <= 0:
            raise BarsError("non_positive_price", f"{name}={value} at {ts.isoformat()}")
    if high < max(open_, close) or low > min(open_, close):
        raise BarsError(
            "inconsistent_bar",
            f"high={high} low={low} open={open_} close={close} at {ts.isoformat()}",
        )
    if high < low:
        raise BarsError("inconsistent_bar", f"high {high} < low {low} at {ts.isoformat()}")

    volume = row.get("volume")
    return Bar(ts_utc=ts, open=open_, high=high, low=low, close=close,
               volume=int(volume) if volume is not None else None)


def build_barset(
    name: str, rows: Sequence[dict[str, Any]], *,
    min_bars: int = DEFAULT_MIN_BARS,
    max_gap_minutes: int = DEFAULT_MAX_GAP_MINUTES,
    source: str = "",
) -> BarSet:
    """Validate one named set. Raises BarsError rather than returning a partial set."""
    parts = name.split("_")
    if len(parts) == 4:
        ticker, side, strike_raw, day_raw = parts
    elif len(parts) == 3:                      # TICKER_SIDE_STRIKE, day from the first bar
        ticker, side, strike_raw = parts
        day_raw = ""
    else:
        raise BarsError("bad_name", name)

    side = side.upper()
    if side not in ("CALL", "PUT", "C", "P"):
        raise BarsError("bad_name", f"{name}: side {side!r}")
    side = "CALL" if side.startswith("C") else "PUT"
    try:
        strike = D(strike_raw)
    except InvalidOperation as exc:
        raise BarsError("bad_name", f"{name}: strike {strike_raw!r}") from exc

    if not rows:
        raise BarsError("empty_set", name)

    bars = tuple(sorted((parse_bar(row) for row in rows), key=lambda b: b.ts_utc))
    if len(bars) < min_bars:
        raise BarsError("too_few_bars", f"{name}: {len(bars)} < {min_bars}")
    for earlier, later in zip(bars, bars[1:], strict=False):
        if later.ts_utc == earlier.ts_utc:
            raise BarsError("duplicate_timestamp", f"{name} at {earlier.ts_utc.isoformat()}")
        if later.ts_utc < earlier.ts_utc:
            raise BarsError("non_monotonic", f"{name} at {later.ts_utc.isoformat()}")

    day = date.fromisoformat(day_raw) if day_raw else bars[0].ts_utc.date()
    interval = int((bars[1].ts_utc - bars[0].ts_utc).total_seconds() // 60)
    if interval > max_gap_minutes:
        raise BarsError("gap_too_large", f"{name}: {interval}m between the first two bars")

    return BarSet(ticker=ticker.upper(), side=side, strike=strike, day=day, bars=bars,
                  source=source)


def load_document(
    payload: dict[str, Any], *, source: str = "", min_bars: int = DEFAULT_MIN_BARS
) -> tuple[list[BarSet], list[dict[str, str]]]:
    """Load a `{name: {bars: [...]}}` document, collecting rejections by reason."""
    accepted: list[BarSet] = []
    rejected: list[dict[str, str]] = []
    for name, body in payload.items():
        try:
            rows = body.get("bars") if isinstance(body, dict) else body
            accepted.append(build_barset(name, rows or [], min_bars=min_bars, source=source))
        except BarsError as exc:
            rejected.append({"set": name, "code": exc.code, "detail": exc.detail})
        except Exception as exc:                # noqa: BLE001 - a bad file is data, not a crash
            rejected.append({"set": name, "code": type(exc).__name__, "detail": str(exc)[:120]})
    return accepted, rejected


def load_path(path: str | Path) -> tuple[list[BarSet], list[dict[str, str]], str]:
    """Load every bar document under a path (file or directory). Returns the content digest."""
    root = Path(path)
    files = [root] if root.is_file() else sorted(root.rglob("*.json"))
    accepted: list[BarSet] = []
    rejected: list[dict[str, str]] = []
    digest = hashlib.sha256()

    for file in files:
        raw = file.read_bytes()
        digest.update(file.name.encode())
        digest.update(raw)
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except json.JSONDecodeError as exc:
            rejected.append({"set": file.name, "code": "bad_json", "detail": str(exc)[:120]})
            continue
        if not isinstance(payload, dict):
            rejected.append({"set": file.name, "code": "bad_shape", "detail": "not an object"})
            continue
        if "bars" in payload:                   # a single set in its own file
            payload = {file.stem: payload}
        good, bad = load_document(payload, source=file.name)
        accepted.extend(good)
        rejected.extend(bad)

    # A directory of vendor exports contains files that are not contract bar sets. Reporting each
    # one buries the rejections that matter, so unrecognised names are collapsed into one line.
    named = [row for row in rejected if row["code"] != "bad_name"]
    unnamed = len(rejected) - len(named)
    if unnamed:
        named.append({
            "set": f"{unnamed} files",
            "code": "unrecognised_name",
            "detail": "filenames are not TICKER_SIDE_STRIKE[_DAY]",
        })
    return accepted, named, digest.hexdigest()


# ── persistence ─────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    contract_key TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume INTEGER,
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (contract_key, ts_utc)
);
CREATE TABLE IF NOT EXISTS bar_sets (
    contract_key TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    strike TEXT NOT NULL,
    day TEXT NOT NULL,
    bars INTEGER NOT NULL,
    first_ts_utc TEXT NOT NULL,
    last_ts_utc TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    gap_minutes INTEGER NOT NULL DEFAULT 0,
    gap_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bars_ts ON bars(ts_utc);
"""


def install_schema(store: Any) -> None:
    with store.connect() as db:
        db.executescript(SCHEMA)


def persist(store: Any, barsets: Iterable[BarSet]) -> int:
    """Write validated sets. Idempotent: a re-ingest replaces a contract's bars atomically."""
    install_schema(store)
    written = 0
    for barset in barsets:
        with store.connect() as db:
            db.execute("DELETE FROM bars WHERE contract_key=?", (barset.contract_key,))
            db.executemany(
                "INSERT INTO bars(contract_key,ts_utc,open,high,low,close,volume,source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [
                    (barset.contract_key, bar.ts_utc.isoformat(), str(bar.open), str(bar.high),
                     str(bar.low), str(bar.close), bar.volume, barset.source)
                    for bar in barset.bars
                ],
            )
            db.execute(
                "INSERT OR REPLACE INTO bar_sets(contract_key,ticker,side,strike,day,bars,"
                "first_ts_utc,last_ts_utc,interval_minutes,gap_minutes,gap_count,source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    barset.contract_key, barset.ticker, barset.side, str(barset.strike),
                    barset.day.isoformat(), len(barset.bars),
                    barset.coverage[0].isoformat(), barset.coverage[1].isoformat(),
                    barset.interval_minutes, sum(barset.gaps), len(barset.gaps),
                    barset.source,
                ),
            )
        written += 1
    return written


def load(store: Any, contract_key: str) -> BarSet | None:
    with store.connect() as db:
        meta = db.execute("SELECT * FROM bar_sets WHERE contract_key=?", (contract_key,)).fetchone()
        if meta is None:
            return None
        rows = db.execute(
            "SELECT ts_utc,open,high,low,close,volume FROM bars "
            "WHERE contract_key=? ORDER BY ts_utc", (contract_key,)
        ).fetchall()
    if not rows:
        return None
    bars = tuple(
        Bar(ts_utc=datetime.fromisoformat(str(r["ts_utc"])), open=D(str(r["open"])),
            high=D(str(r["high"])), low=D(str(r["low"])), close=D(str(r["close"])),
            volume=int(r["volume"]) if r["volume"] is not None else None)
        for r in rows
    )
    return BarSet(
        ticker=str(meta["ticker"]), side=str(meta["side"]), strike=D(str(meta["strike"])),
        day=date.fromisoformat(str(meta["day"])), bars=bars, source=str(meta["source"]),
    )


def load_all(store: Any) -> dict[str, BarSet]:
    install_schema(store)
    with store.connect() as db:
        keys = [str(r["contract_key"]) for r in db.execute(
            "SELECT contract_key FROM bar_sets ORDER BY contract_key"
        )]
    out: dict[str, BarSet] = {}
    for key in keys:
        barset = load(store, key)
        if barset is not None:
            out[key] = barset
    return out


def inventory(store: Any) -> dict[str, Any]:
    """What the bar store holds, by ticker and by month - the coverage question in one call."""
    install_schema(store)
    with store.connect() as db:
        rows = db.execute(
            "SELECT ticker, side, day, bars, interval_minutes, gap_minutes, gap_count "
            "FROM bar_sets"
        ).fetchall()
    by_ticker: dict[str, int] = {}
    by_month: dict[str, int] = {}
    gap_minutes = 0
    gap_count = 0
    for row in rows:
        by_ticker[str(row["ticker"])] = by_ticker.get(str(row["ticker"]), 0) + 1
        month = str(row["day"])[:7]
        by_month[month] = by_month.get(month, 0) + 1
        gap_minutes += int(row["gap_minutes"])
        gap_count += int(row["gap_count"])
    return {
        "contracts": len(rows),
        "bars": sum(int(row["bars"]) for row in rows),
        "gap_minutes": gap_minutes,
        "gap_count": gap_count,
        "by_ticker": dict(sorted(by_ticker.items())),
        "by_month": dict(sorted(by_month.items())),
    }
