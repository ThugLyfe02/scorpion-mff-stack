"""Run the hot path against the historical alert corpus.

This is NOT a strategy backtest. The strategy is the channel's; this measures the
EXECUTOR: given the 93 alerts the desk has on record (mff-backtest/bt_full/trades.json,
2026-03-10 .. 2026-09-03), what would the locked rules have actually done - how many
contracts, at what limit, taken or skipped and why, and what the rules themselves cost.

Three assumptions are stated rather than buried, because every number below depends on them:

1. ASK MODEL. The corpus records his posted bid (`entry_bid`), never the live ask at
   the moment of the post. `ask_multiple` says how far through his bid we had to pay:
      1.00  fill at his bid (optimistic - the post-mortem's implicit assumption)
      1.05  five percent through (the default here)
      1.30  thirty percent through - past the +25% gate, so the gate fires
2. EXIT LEVEL. `exit_pct` is the level he called out, and the corpus's `outcome` is
   what actually happened. The exit mark is entry_bid * (1 + exit_pct/100), optionally
   haircut by `exit_slippage` (default 1.0 = take the recorded level at face value).
3. EXIT TIME. The notes usually carry the clock time of the close ("200% 12:24"). The
   last HH:MM in the notes that is not before the entry is used; otherwise entry + 20m.

ET is modelled as a fixed UTC-04:00 (EDT) offset rather than a tz database lookup: the
engine only cares about ordering and the day boundary, and no trade in this corpus sits
within four hours of midnight.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from .broker import QuoteScript
from .engine import Engine, EngineConfig
from .fills import FillLog
from .parser import CHANNEL_IDS, parse
from .types import RawMessage

ET = timezone(timedelta(hours=-4))          # EDT; see assumption 3
DEFAULT_ASK_MULTIPLE = 1.05
DEFAULT_HOLD_MINUTES = 20
ETC_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = ETC_DIR / "mff-backtest" / "bt_full" / "trades.json"

_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


@dataclass(frozen=True)
class Trade:
    date: str            # YYYY-MM-DD
    time_et: str         # HH:MM
    channel: str
    ticker: str
    side: str            # "call" | "put"
    strike: str
    expiry: str
    entry_bid: float
    exit_pct: float | None
    outcome: str
    notes: str

    @property
    def entry_ts(self) -> float:
        hh, mm = (int(x) for x in self.time_et.replace(".", ":").split(":")[:2])
        day = datetime.strptime(self.date, "%Y-%m-%d").replace(
            hour=hh, minute=mm, tzinfo=ET)
        return day.timestamp()

    @property
    def exit_ts(self) -> float:
        """Recorded close time from the notes, else entry + the default hold."""
        candidates: list[tuple[int, int]] = [(int(h), int(m)) for h, m in _TIME_RE.findall(self.notes or "")]
        entry = datetime.fromtimestamp(self.entry_ts, ET)
        same_or_later = [
            entry.replace(hour=h, minute=m) for h, m in candidates
            if (h, m) >= (entry.hour, entry.minute)
        ]
        if same_or_later:
            return max(same_or_later).timestamp()
        return (entry + timedelta(minutes=DEFAULT_HOLD_MINUTES)).timestamp()

    @property
    def exit_price(self) -> float | None:
        if self.exit_pct is None:
            return None
        return round(self.entry_bid * (1 + self.exit_pct / 100.0), 4)

    @property
    def contract_key(self) -> str:
        # The parser builds keys with {strike:g}; the corpus writes "227.50". A
        # mismatch is not a market event but it prices as no_quote, so normalise.
        try:
            strike = f"{float(self.strike):g}"
        except (TypeError, ValueError):
            strike = self.strike.strip()
        return f"{self.ticker}|{self.side.upper()}|{strike}|{self.expiry.strip()}"


@dataclass
class TradeResult:
    trade: Trade
    taken: bool
    contracts: int = 0
    entry_limit: float = 0.0
    exit_price: float = 0.0
    pnl_usd: float = 0.0
    reason: str = ""
    decision: str = ""


@dataclass
class BacktestReport:
    trades: int = 0
    days: int = 0
    taken: int = 0
    skipped: int = 0
    rejected: int = 0
    contracts: int = 0
    notional_usd: float = 0.0
    pnl_usd: float = 0.0
    winners: int = 0
    losers: int = 0
    by_channel: dict[str, dict[str, float]] = field(default_factory=dict)
    skip_reasons: dict[str, int] = field(default_factory=dict)
    results: list[TradeResult] = field(default_factory=list)
    ask_multiple: float = DEFAULT_ASK_MULTIPLE
    exit_slippage: float = 1.0
    pipe_test: bool = True
    duplicate_keys: list[str] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        graded = self.winners + self.losers
        return (100.0 * self.winners / graded) if graded else 0.0

    @property
    def avg_pnl_usd(self) -> float:
        return (self.pnl_usd / self.taken) if self.taken else 0.0

    def to_text(self) -> str:
        lines = [
            f"executor backtest | ask x{self.ask_multiple:g} | exit slippage {self.exit_slippage:g}"
            f" | pipe_test={self.pipe_test}",
            f"alerts        {self.trades} over {self.days} days",
            f"taken         {self.taken}   skipped {self.skipped}   rejected {self.rejected}",
            f"contracts     {self.contracts}   notional ${self.notional_usd:,.0f}",
            f"P&L           ${self.pnl_usd:,.2f}   avg/trade ${self.avg_pnl_usd:,.2f}"
            f"   win rate {self.win_rate:.1f}%  ({self.winners}W/{self.losers}L)",
        ]
        if self.duplicate_keys:
            lines.append(f"note          {len(self.duplicate_keys)} contract key(s) repeat: "
                         f"{', '.join(self.duplicate_keys[:3])}")
        if self.by_channel:
            lines.append("by channel:")
            for name, agg in sorted(self.by_channel.items(), key=lambda kv: -kv[1]["pnl"]):
                lines.append(f"  {name:<26} n={int(agg['n']):<3} pnl ${agg['pnl']:>10,.2f}"
                             f"  contracts {int(agg['contracts'])}")
        if self.skip_reasons:
            lines.append("skips / rejects:")
            for reason, count in sorted(self.skip_reasons.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {count:>3}  {reason}")
        return "\n".join(lines)

    def to_markdown(self) -> str:
        """The shareable artefact: tables a human can read in the repo."""
        head = [
            "# Executor backtest — every alert on record",
            "",
            f"Corpus: `mff-backtest/bt_full/trades.json` — **{self.trades} alerts over {self.days} days** "
            f"(2026-03-10 .. 2026-09-03).",
            "",
            "This measures the **executor**, not the strategy: the same parser, state machine, "
            "pricing and sizing that would run live, driven by the historical alerts.",
            "",
            f"Assumptions: ask = his posted bid × **{self.ask_multiple:g}**, exit at the recorded "
            f"level × **{self.exit_slippage:g}**, ET = UTC-04:00, pipe test "
            f"**{'on' if self.pipe_test else 'off'}**. See `scorpion_mff/historic.py`.",
            "",
            "## Headline",
            "",
            "| metric | value |",
            "|---|---|",
            f"| alerts | {self.trades} |",
            f"| taken | {self.taken} |",
            f"| skipped / rejected | {self.skipped} / {self.rejected} |",
            f"| contracts | {self.contracts} |",
            f"| notional | ${self.notional_usd:,.0f} |",
            f"| realised P&L | **${self.pnl_usd:,.2f}** |",
            f"| average per taken trade | ${self.avg_pnl_usd:,.2f} |",
            f"| win rate | {self.win_rate:.1f}% ({self.winners}W / {self.losers}L) |",
            "",
            "## By channel",
            "",
            "| channel | alerts | contracts | P&L |",
            "|---|---|---|---|",
        ]
        for name, agg in sorted(self.by_channel.items(), key=lambda kv: -kv[1]["pnl"]):
            head.append(f"| {name} | {int(agg['n'])} | {int(agg['contracts'])} | ${agg['pnl']:,.2f} |")
        head += ["", "## Why alerts were not taken", "", "| reason | count |", "|---|---|"]
        for reason, count in sorted(self.skip_reasons.items(), key=lambda kv: -kv[1]):
            head.append(f"| `{reason}` | {count} |")
        head += ["", "## The ten largest decisions", "",
                 "| date | et | channel | contract | contracts | limit | exit | P&L | decision |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for r in sorted(self.results, key=lambda r: -abs(r.pnl_usd))[:10]:
            t = r.trade
            head.append(
                f"| {t.date} | {t.time_et} | {t.channel} | {t.ticker} {t.side} {t.strike} {t.expiry} "
                f"| {r.contracts or '—'} | {r.entry_limit or 0:.2f} | {r.exit_price or 0:.2f} "
                f"| {r.pnl_usd:+,.2f} | {r.decision or '—'} |")
        head += ["", f"_generated by `python -m scorpion_mff.cli backtest --md reports/executor-backtest.md`_", ""]
        return "\n".join(head)


def load_trades(path: str | Path = DEFAULT_CORPUS) -> list[Trade]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    trades = [
        Trade(
            date=str(t["date"]), time_et=str(t["time_et"]), channel=str(t["channel"]),
            ticker=str(t["ticker"]).upper(), side=str(t["side"]).lower(),
            strike=str(t["strike"]), expiry=str(t["expiry"]),
            entry_bid=float(t["entry_bid"]),
            exit_pct=(float(t["exit_pct"]) if t.get("exit_pct") is not None else None),
            outcome=str(t.get("outcome") or "unknown"), notes=str(t.get("notes") or ""),
        )
        for t in raw
    ]
    return sorted(trades, key=lambda t: (t.date, t.time_et))


def entry_message(trade: Trade) -> RawMessage:
    text = (f"{trade.side.upper()} TICKER {trade.ticker} STRIKE ${trade.strike} "
            f"Exp {trade.expiry} Current BID Price ${trade.entry_bid:.2f}")
    return RawMessage(channel=trade.channel, channel_id=CHANNEL_IDS.get(trade.channel, ""),
                      text=text, ts=trade.entry_ts)


def exit_message(trade: Trade, *, named: bool = True,
                 slippage: float = 1.0) -> RawMessage | None:
    """An exit post.

    `named` restates the ticker, which is what the channel does ("QQQ puts closed
    at 2.85") and what lets the engine attribute the exit to one position.
    `named=False` emits the bare form ("Close at 2.85") to measure what happens
    when the post names nothing - with two positions open that is correctly an
    alert, not a sale.

    `slippage` haircuts the price the post reports, because the engine executes at
    the price in the message: a 0.90 haircut models getting 90% of his exit level.
    """
    price = trade.exit_price
    if price is None:
        return None
    marked = round(price * slippage, 4)
    text = (f"{trade.ticker} {trade.side}s closed at {marked:.2f}" if named
            else f"Close at {marked:.2f}")
    return RawMessage(channel=trade.channel, channel_id=CHANNEL_IDS.get(trade.channel, ""),
                      text=text, ts=trade.exit_ts)


def build_quotes(trades: Iterable[Trade], ask_multiple: float,
                 exit_slippage: float = 1.0) -> dict[str, list[tuple[float, float]]]:
    """Per-contract quote sequence: (entry bid/ask), then (exit bid/ask).

    The exit *price* comes from the post (the engine executes at the price in the
    message), so the exit quote only needs to be a plausible market for it; the
    slippage that matters is applied to the post itself in `exit_message`.
    """
    script: dict[str, list[tuple[float, float]]] = {}
    for t in trades:
        entry_ask = round(t.entry_bid * ask_multiple, 4)
        script.setdefault(t.contract_key, []).append((t.entry_bid, entry_ask))
        price = t.exit_price
        if price is not None:
            script[t.contract_key].append((price, round(price * 1.02, 4)))
    return script


def run_backtest(trades: list[Trade] | None = None, *,
                 ask_multiple: float = DEFAULT_ASK_MULTIPLE,
                 exit_slippage: float = 1.0,
                 pipe_test: bool = True,
                 named_exits: bool = True,
                 corpus: str | Path = DEFAULT_CORPUS) -> BacktestReport:
    """Drive the real pipeline over the corpus. Deterministic."""
    trades = list(trades if trades is not None else load_trades(corpus))
    engine = Engine(config=EngineConfig(pipe_test=pipe_test), fills=FillLog(),
                    quotes=QuoteScript(build_quotes(trades, ask_multiple, exit_slippage)))

    report = BacktestReport(trades=len(trades), ask_multiple=ask_multiple,
                            exit_slippage=exit_slippage, pipe_test=pipe_test,
                            days=len({t.date for t in trades}))
    keys: dict[str, int] = {}

    for trade in trades:
        keys[trade.contract_key] = keys.get(trade.contract_key, 0) + 1
        entry_signal = parse(entry_message(trade))
        intents = engine.on_signal(entry_signal, now=trade.entry_ts)
        result = TradeResult(trade=trade, taken=bool(intents))

        if intents:
            intent = intents[0]
            result.contracts = intent.qty
            result.entry_limit = intent.limit
            result.decision = intent.reason
        else:
            last = engine.fills.records[-1] if engine.fills.records else None
            result.reason = last.reason if last else "no_signal"
            result.decision = result.reason
            if last is not None and last.event in {"skip", "reject", "alert"}:
                # Group by reason KIND, keeping the qualifier when it is a name:
                # "too_far_off_bid:31.2%" -> too_far_off_bid, but
                # "ignored:ticker_excluded" keeps both halves.
                parts = last.reason.split(":")
                if len(parts) > 1 and ("%" in parts[1] or parts[1].replace(".", "").isdigit()):
                    reason_key = parts[0]
                else:
                    reason_key = last.reason
                report.skip_reasons[reason_key] = report.skip_reasons.get(reason_key, 0) + 1
                if last.event == "reject":
                    report.rejected += 1
                else:
                    report.skipped += 1

        exit_msg = exit_message(trade, named=named_exits, slippage=exit_slippage)
        if exit_msg is not None and result.taken:
            sell_intents = engine.on_signal(parse(exit_msg), now=trade.exit_ts)
            if sell_intents:
                fill = sell_intents[0]
                result.exit_price = fill.limit
                result.pnl_usd = round((fill.limit - result.entry_limit) * fill.qty * 100, 2)

        if result.taken:
            report.taken += 1
            report.contracts += result.contracts
            report.notional_usd += result.entry_limit * result.contracts * 100
            report.pnl_usd += result.pnl_usd
            if result.pnl_usd > 0:
                report.winners += 1
            elif result.pnl_usd < 0:
                report.losers += 1
            agg = report.by_channel.setdefault(trade.channel, {"n": 0, "contracts": 0, "pnl": 0.0})
            agg["n"] += 1
            agg["contracts"] += result.contracts
            agg["pnl"] += result.pnl_usd
        report.results.append(result)

    report.pnl_usd = round(report.pnl_usd, 2)
    report.notional_usd = round(report.notional_usd, 2)
    report.by_channel = {k: {"n": v["n"], "contracts": v["contracts"], "pnl": round(v["pnl"], 2)}
                         for k, v in report.by_channel.items()}
    report.duplicate_keys = sorted(k for k, n in keys.items() if n > 1)
    return report


def attribution(trades: list[Trade] | None = None,
                corpus: str | Path = DEFAULT_CORPUS) -> list[tuple[str, BacktestReport]]:
    """Quantify what each locked rule is worth, one rule at a time."""
    trades = list(trades if trades is not None else load_trades(corpus))
    variants: list[tuple[str, dict]] = [
        ("as locked (ask 1.05, pipe test on)", dict(ask_multiple=1.05, pipe_test=True)),
        ("no pipe test (full clip from trade 1)", dict(ask_multiple=1.05, pipe_test=False)),
        ("fill at his bid (ask 1.00)", dict(ask_multiple=1.00, pipe_test=True)),
        ("30% through his bid -> +25% gate fires", dict(ask_multiple=1.30, pipe_test=True)),
        ("10% exit haircut", dict(ask_multiple=1.05, pipe_test=True, exit_slippage=0.90)),
    ]
    return [(label, run_backtest(trades, **kwargs)) for label, kwargs in variants]
