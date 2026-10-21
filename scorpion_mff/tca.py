"""Execution quality and path analysis, measured on real prices.

Two halves, and they answer different questions.

**Excursion / path analysis** (works today, on the repo's own 1-minute-sourced bars)
takes a trade we would have entered at `his_bid x ask_multiple` and asks what the
contract actually did: how much heat it put us through before working (MAE), how much
favourable excursion it offered (MFE), whether his posted exit level ever printed at
all, and what the premium was worth at the close if an exit were missed. That last
number is the one the backtest cannot see: `BACKTEST.md` admits an unexited position is
marked at entry, which *hides* the real loss. Here it is measured.

**Venue fill analysis** (empty until live fills exist) is the transaction-cost layer:
limit vs realised fill in cents and bps, fill ratio, latency, and the calibration of
`ask_multiple` and the +25% gate from what we actually paid rather than what we assumed.

Nothing here is a forecast. It measures the assumptions the rest of the stack is built
on, and it states how many trades it actually had data for.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_BARS_DIR = Path("mff-backtest/charts_1m/bars")
BARS_NAME_RE = re.compile(
    r"trade_(\d+)_(\d{4}-\d{2}-\d{2})_([A-Z]+)_([a-z]+)_([0-9.]+)\.json$")


# ── bars ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Bar:
    ts: float
    open: float
    high: float
    low: float
    close: float


@dataclass
class BarSet:
    date: str
    ticker: str
    side: str
    strike: float
    bars: tuple[Bar, ...]
    trade_index: int | None = None

    @property
    def key(self) -> tuple[str, str, str, float]:
        return (self.date, self.ticker, self.side, round(self.strike, 2))

    @property
    def span_s(self) -> float:
        return (self.bars[-1].ts - self.bars[0].ts) if len(self.bars) > 1 else 0.0


def _parse_ts(raw: str) -> float:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(
        tzinfo=timezone.utc).timestamp()


def load_barsets(directory: str | Path = DEFAULT_BARS_DIR) -> list[BarSet]:
    """Every `trade_<n>_<date>_<TICKER>_<side>_<strike>.json` series in a directory.

    The `<date>_<instrument_id>` files are a different naming scheme and carry a single
    bar each - they are skipped rather than silently treated as a day of data.
    """
    out: list[BarSet] = []
    for path in sorted(Path(directory).glob("trade_*.json")):
        match = BARS_NAME_RE.search(path.name)
        if not match:
            continue
        trade_index, date, ticker, side, strike = match.groups()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = payload.get("bars") or []
        bars = tuple(
            Bar(ts=_parse_ts(r["begins_at"]), open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]))
            for r in rows if r.get("begins_at")
        )
        if not bars:
            continue
        out.append(BarSet(date=date, ticker=ticker, side=side, strike=float(strike),
                          bars=tuple(sorted(bars, key=lambda b: b.ts)),
                          trade_index=int(trade_index)))
    return out


def join_to_trades(barsets: Sequence[BarSet], trades: Iterable) -> tuple[list[tuple], list[BarSet]]:
    """Match bar series to corpus trades by (date, ticker, side, strike).

    Joining on content rather than the filename's `trade_index`: the index in the file
    names is a numbering from the bar-fetch pass, not a row in `trades.json`.
    """
    index = {(t.date, t.ticker.upper(), t.side.lower(), round(float(t.strike), 2)): t
             for t in trades}
    joined, unmatched = [], []
    for bs in barsets:
        trade = index.get(bs.key)
        if trade is None:
            unmatched.append(bs)
        else:
            joined.append((trade, bs))
    return joined, unmatched


# ── path analysis ──────────────────────────────────────────────────────────
@dataclass
class TradePath:
    """What the contract did between our (modelled) entry and his exit.

    `measurable` is the honest gate: true only when entry AND exit both fall inside the
    bar coverage. Sixteen of the eighteen joins enter pre-market while the series start
    at 09:30, so for those the path *cannot* be measured - only the carry cost can. The
    aggregate metrics ignore unmeasurable rows instead of averaging them in.
    """

    date: str
    time_et: str
    channel: str
    ticker: str
    side: str
    strike: float
    entry_price: float
    exit_price: float | None
    entry_in_coverage: bool
    measurable: bool = False
    mfe_pct: float | None = None          # best high vs entry (measurable rows only)
    mae_pct: float | None = None          # worst low vs entry (negative)
    level_printed: bool | None = None       # did his exit level print inside coverage
    level_printed_by_exit: bool | None = None
    first_close: float = 0.0
    carry_pct: float = 0.0        # first covered close vs entry: cost of carrying in
    terminal_close: float = 0.0
    terminal_pct: float = 0.0     # last covered close vs entry
    expired: bool = False         # contract ended at ~zero
    stop_hit_30: bool | None = None
    stop_hit_50: bool | None = None
    bars_used: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {"date": self.date, "time_et": self.time_et, "channel": self.channel,
                "contract": f"{self.ticker} {self.side} {self.strike:g}",
                "entry_price": round(self.entry_price, 4),
                "exit_price": None if self.exit_price is None else round(self.exit_price, 4),
                "entry_in_coverage": self.entry_in_coverage, "measurable": self.measurable,
                "mfe_pct": None if self.mfe_pct is None else round(self.mfe_pct, 2),
                "mae_pct": None if self.mae_pct is None else round(self.mae_pct, 2),
                "level_printed": self.level_printed,
                "level_printed_by_exit": self.level_printed_by_exit,
                "carry_pct": round(self.carry_pct, 2),
                "terminal_close": round(self.terminal_close, 4),
                "terminal_pct": round(self.terminal_pct, 2),
                "expired": self.expired,
                "stop_hit_30": self.stop_hit_30, "stop_hit_50": self.stop_hit_50,
                "bars_used": self.bars_used, "note": self.note}


def analyse_trade(trade, barset: BarSet, *, ask_multiple: float = 1.05,
                  stop_levels: Sequence[float] = (0.30, 0.50)) -> TradePath:
    """One trade's realised path, from the bars we actually have.

    Path metrics are computed only when the entry AND the stated exit are inside the
    series. Otherwise the row carries the carry cost and says why it is not measurable.
    """
    entry_price = round(trade.entry_bid * ask_multiple, 4)
    exit_price = trade.exit_price
    entry_ts, exit_ts = trade.entry_ts, trade.exit_ts
    first_bar_ts, last_bar_ts = barset.bars[0].ts, barset.bars[-1].ts

    path = TradePath(date=trade.date, time_et=trade.time_et, channel=trade.channel,
                     ticker=trade.ticker, side=trade.side, strike=float(trade.strike),
                     entry_price=entry_price, exit_price=exit_price,
                     entry_in_coverage=entry_ts >= first_bar_ts)

    covered = [b for b in barset.bars if b.ts >= max(entry_ts, first_bar_ts)]
    if not covered:
        path.note = "no bars at or after entry"
        return path
    path.bars_used = len(covered)
    path.first_close = covered[0].close
    path.carry_pct = (path.first_close / entry_price - 1) * 100
    path.terminal_close = covered[-1].close
    path.terminal_pct = (path.terminal_close / entry_price - 1) * 100
    path.expired = path.terminal_close <= max(0.02, entry_price * 0.02)

    path.measurable = path.entry_in_coverage and exit_ts <= last_bar_ts
    if not path.measurable:
        if not path.entry_in_coverage:
            path.note = ("entry before bar coverage - path not measurable; "
                         "carry cost only")
        else:
            path.note = "exit after bar coverage - path not measurable; carry cost only"
        if exit_price is not None:
            # Not a verdict on his exit: the window simply does not contain it.
            path.level_printed = None
        return path

    window = [b for b in covered if b.ts <= exit_ts]
    path.mfe_pct = (max(b.high for b in window) / entry_price - 1) * 100
    path.mae_pct = (min(b.low for b in window) / entry_price - 1) * 100
    path.stop_hit_30 = 0.30 in stop_levels and path.mae_pct <= -30.0
    path.stop_hit_50 = 0.50 in stop_levels and path.mae_pct <= -50.0
    if exit_price is not None:
        path.level_printed = any(b.high >= exit_price for b in window)
        path.level_printed_by_exit = path.level_printed
    return path


@dataclass
class PathReport:
    rows: list[TradePath] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    barsets: int = 0

    # ── aggregates ──
    @property
    def n(self) -> int:
        return len(self.rows)

    @property
    def with_exit(self) -> list[TradePath]:
        return [r for r in self.rows if r.exit_price is not None]

    @property
    def in_coverage(self) -> list[TradePath]:
        return [r for r in self.rows if r.entry_in_coverage]

    @property
    def measurable(self) -> list[TradePath]:
        """The only rows allowed into path aggregates."""
        return [r for r in self.rows if r.measurable]

    @property
    def measurable_enough(self) -> bool:
        """Five is the floor for quoting a median at all; below it we say so."""
        return len(self.measurable) >= 5

    def _median(self, values: Sequence[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        mid = len(ordered) // 2
        return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2

    @property
    def mae_median(self) -> float:
        return self._median([r.mae_pct for r in self.measurable if r.mae_pct is not None])

    @property
    def mae_worst(self) -> float:
        return min([r.mae_pct for r in self.measurable if r.mae_pct is not None], default=0.0)

    @property
    def mfe_median(self) -> float:
        return self._median([r.mfe_pct for r in self.measurable if r.mfe_pct is not None])

    @property
    def stop30_share(self) -> float:
        rows = [r for r in self.measurable if r.stop_hit_30 is not None]
        return (sum(1 for r in rows if r.stop_hit_30) / len(rows) * 100) if rows else 0.0

    @property
    def stop50_share(self) -> float:
        rows = [r for r in self.measurable if r.stop_hit_50 is not None]
        return (sum(1 for r in rows if r.stop_hit_50) / len(rows) * 100) if rows else 0.0

    @property
    def printed_share(self) -> float:
        rows = [r for r in self.measurable if r.level_printed is not None]
        if not rows:
            return 0.0
        return sum(1 for r in rows if r.level_printed) / len(rows) * 100

    # ── carry cost: measurable for every joined trade, coverage or not ──
    @property
    def carry_median(self) -> float:
        """First covered close vs entry — what the position was worth when RTH opened."""
        return self._median([r.carry_pct for r in self.rows])

    @property
    def expired_share(self) -> float:
        return (sum(1 for r in self.rows if r.expired) / self.n * 100) if self.n else 0.0

    def to_text(self) -> str:
        lines = [
            f"path analysis | {self.n} trades joined to bars | {self.barsets} bar series",
            f"  measurable (entry AND exit inside coverage)  {len(self.measurable)}/{self.n}"
            f"   <- aggregates use only these",
            f"  entry before bar coverage                   {self.n - len(self.in_coverage)}/{self.n}"
            f"  (pre-market entries; series start 09:30 ET)",
        ]
        if self.measurable_enough:
            lines += [
                f"  MAE  median {self.mae_median:+.1f}%   worst {self.mae_worst:+.1f}%",
                f"  MFE  median {self.mfe_median:+.1f}%",
                f"  a -30% stop would have been hit in {self.stop30_share:.0f}%,"
                f" -50% in {self.stop50_share:.0f}%",
                f"  his exit level printed inside the window   {self.printed_share:.0f}%",
            ]
        else:
            lines.append(f"  path aggregates withheld: only {len(self.measurable)} measurable "
                         f"trade(s), below the floor of 5")
        lines += [
            f"  carry: value at the first covered bar vs entry  {self.carry_median:+.1f}% median",
            f"  contracts that expired at ~zero               {self.expired_share:.0f}%",
        ]
        if self.unmatched:
            lines.append(f"  unmatched bar series: {len(self.unmatched)} "
                         f"({'; '.join(self.unmatched[:3])})")
        return "\n".join(lines)

    def to_markdown(self) -> str:
        lines = [
            "# Execution path analysis — real bars, and what they can actually answer",
            "",
            f"Contract bars from `mff-backtest/charts_1m/bars`, joined to the corpus by "
            f"(date, ticker, side, strike): **{self.n} trades** across {self.barsets} series.",
            "",
            f"**Coverage is the headline finding.** The series start at 09:30 ET, and "
            f"**{self.n - len(self.in_coverage)} of {self.n}** entries are pre-market. Only "
            f"**{len(self.measurable)}** trades have both their entry and their stated exit "
            f"inside the data, so path statistics are quoted only where they are real:",
            "",
            "| metric | value |",
            "|---|---|",
        ]
        if self.measurable_enough:
            lines += [
                f"| median MAE (heat before working) | {self.mae_median:+.1f}% |",
                f"| worst MAE | {self.mae_worst:+.1f}% |",
                f"| median MFE | {self.mfe_median:+.1f}% |",
                f"| a −30% stop would have been hit | {self.stop30_share:.0f}% |",
                f"| a −50% stop would have been hit | {self.stop50_share:.0f}% |",
                f"| his exit level printed inside the window | {self.printed_share:.0f}% |",
            ]
        else:
            lines.append(
                f"| path statistics | **withheld** — only {len(self.measurable)} measurable "
                f"trade(s), below the floor of 5 |")
        lines += [
            f"| carry: value at the first covered bar vs entry (median) | {self.carry_median:+.1f}% |",
            f"| contracts that expired at ~zero | {self.expired_share:.0f}% |",
            "",
            "## Why this matters more than a table of MFE",
            "",
            "The stack's exit assumption is that his posted level was attainable. This data "
            "**cannot confirm or refute that for most trades**, because the move happened "
            "before the first bar. Saying so is the result: an assumption labelled "
            "*unverified* is safer than one averaged over a window that does not contain it. "
            "What the bars *do* show is the cost of carrying: what the position was worth "
            "once regular hours opened, and how many contracts finished at ~zero.",
            "",
            "## Per trade",
            "",
            "| date | et | contract | entry | exit | measurable | MFE | MAE | carry | close | note |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in sorted(self.rows, key=lambda r: (r.date, r.time_et)):
            mfe = "—" if r.mfe_pct is None else f"{r.mfe_pct:+.0f}%"
            mae = "—" if r.mae_pct is None else f"{r.mae_pct:+.0f}%"
            lines.append(
                f"| {r.date} | {r.time_et} | {r.ticker} {r.side} {r.strike:g} | "
                f"{r.entry_price:.2f} | {'—' if r.exit_price is None else f'{r.exit_price:.2f}'} | "
                f"{'yes' if r.measurable else 'no'} | {mfe} | {mae} | "
                f"{r.carry_pct:+.0f}% | {r.terminal_close:.2f} | {r.note} |")
        lines += ["", "_generated by `python -m scorpion_mff.cli tca path --md reports/path-analysis.md`_", ""]
        return "\n".join(lines)


def analyse_paths(barsets: Sequence[BarSet], trades: Iterable, *,
                  ask_multiple: float = 1.05) -> PathReport:
    joined, unmatched = join_to_trades(barsets, trades)
    report = PathReport(barsets=len(barsets),
                        unmatched=[f"{b.date} {b.ticker} {b.side} {b.strike:g}" for b in unmatched])
    report.rows = [analyse_trade(t, b, ask_multiple=ask_multiple) for t, b in joined]
    return report


# ── venue fills (empty until live) ─────────────────────────────────────────
@dataclass(frozen=True)
class Execution:
    """One realised fill, as the venue reported it.

    `cost_usd` is signed so that positive is always money we lost to execution:
    a buy above its limit and a sell below its limit both cost.
    """

    contract_key: str
    action: str
    qty: int
    limit: float
    fill_price: float
    fill_qty: int
    intent_ts: float
    fill_ts: float
    channel: str = ""
    reference: float | None = None       # his posted bid, when known

    @property
    def fill_ratio(self) -> float:
        return (self.fill_qty / self.qty) if self.qty else 0.0

    @property
    def cost_per_contract(self) -> float:
        return (self.fill_price - self.limit) if self.action == "buy" else (self.limit - self.fill_price)

    @property
    def cost_usd(self) -> float:
        return round(self.cost_per_contract * self.fill_qty * 100, 2)

    @property
    def cost_bps(self) -> float:
        return (self.cost_per_contract / self.limit * 10_000) if self.limit else 0.0

    @property
    def latency_ms(self) -> float:
        return max(0.0, (self.fill_ts - self.intent_ts) * 1000)

    @property
    def through_bid(self) -> float | None:
        """How far past his posted bid we actually paid, as a multiple (1.05 = +5%)."""
        if not self.reference:
            return None
        return self.fill_price / self.reference

    def to_dict(self) -> dict:
        return {"contract_key": self.contract_key, "action": self.action, "qty": self.qty,
                "limit": self.limit, "fill_price": self.fill_price, "fill_qty": self.fill_qty,
                "intent_ts": self.intent_ts, "fill_ts": self.fill_ts,
                "channel": self.channel, "reference": self.reference,
                "fill_ratio": round(self.fill_ratio, 4),
                "cost_per_contract": round(self.cost_per_contract, 4),
                "cost_usd": self.cost_usd, "cost_bps": round(self.cost_bps, 1),
                "latency_ms": round(self.latency_ms, 1),
                "through_bid": None if self.through_bid is None else round(self.through_bid, 4)}


class ExecutionLog:
    """Append-only JSONL of venue fills. One write per record, like the fill log."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records: list[Execution] = []

    def record(self, execution: Execution) -> Execution:
        self.records.append(execution)
        if self.path:
            line = json.dumps(execution.to_dict(), sort_keys=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
        return execution

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "ExecutionLog":
        log = cls(path)
        p = Path(path)
        if not p.exists():
            return log
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue                       # a torn line is skipped, not fatal
            log.records.append(Execution(
                contract_key=str(row.get("contract_key", "")),
                action=str(row.get("action", "")),
                qty=int(row.get("qty") or 0),
                limit=float(row.get("limit") or 0.0),
                fill_price=float(row.get("fill_price") or 0.0),
                fill_qty=int(row.get("fill_qty") or 0),
                intent_ts=float(row.get("intent_ts") or 0.0),
                fill_ts=float(row.get("fill_ts") or 0.0),
                channel=str(row.get("channel", "")),
                reference=(float(row["reference"]) if row.get("reference") is not None else None),
            ))
        return log


@dataclass
class FillReport:
    executions: list[Execution] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.executions)

    @property
    def mean_cost_bps(self) -> float:
        return (sum(e.cost_bps for e in self.executions) / self.n) if self.n else 0.0

    @property
    def total_cost_usd(self) -> float:
        return round(sum(e.cost_usd for e in self.executions), 2)

    @property
    def at_or_better_share(self) -> float:
        if not self.n:
            return 0.0
        return sum(1 for e in self.executions if e.cost_per_contract <= 0) / self.n * 100

    @property
    def partial_share(self) -> float:
        if not self.n:
            return 0.0
        return sum(1 for e in self.executions if e.fill_ratio < 1.0) / self.n * 100

    @property
    def p90_cost_bps(self) -> float:
        values = sorted(e.cost_bps for e in self.executions)
        if not values:
            return 0.0
        return values[min(len(values) - 1, math.ceil(0.9 * len(values)) - 1)]

    @property
    def latency_p90_ms(self) -> float:
        values = sorted(e.latency_ms for e in self.executions)
        if not values:
            return 0.0
        return values[min(len(values) - 1, math.ceil(0.9 * len(values)) - 1)]

    def suggested_ask_multiple(self) -> float | None:
        """The p90 of what we actually paid past his bid, rounded *up* to 0.05.

        Rounding up is deliberate: this number becomes the new assumption in the
        backtest, and an assumption that is optimistic by construction is worse than a
        conservative one.
        """
        ratios = [e.through_bid for e in self.executions if e.through_bid]
        if len(ratios) < 10:
            return None
        ratios.sort()
        p90 = ratios[min(len(ratios) - 1, math.ceil(0.9 * len(ratios)) - 1)]
        return math.ceil(p90 * 20) / 20

    def to_text(self) -> str:
        if not self.n:
            return ("fill analysis | no venue fills recorded yet\n"
                    "  live/fills are intents; executions are written by the broker when a\n"
                    "  venue confirms a fill. Run the hone in dry mode against a venue, or\n"
                    "  import a broker export, then re-run: nothing here is estimated.")
        return "\n".join([
            f"fill analysis | {self.n} fills",
            f"  mean cost {self.mean_cost_bps:+.1f} bps | p90 {self.p90_cost_bps:+.1f} bps"
            f" | total ${self.total_cost_usd:+,.2f}",
            f"  filled at or better than our limit: {self.at_or_better_share:.0f}%",
            f"  partial fills: {self.partial_share:.0f}%",
            f"  latency p90 {self.latency_p90_ms:,.0f} ms (budget 10,000)",
            f"  suggested ask_multiple: "
            f"{self.suggested_ask_multiple() or 'need >= 10 fills with a reference price'}",
        ])
