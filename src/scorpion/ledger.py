"""The trade ledger: the corpus, typed, validated and auditable.

Every quantitative claim this system makes about itself - what the edge is, how much to size,
what a rule is worth - has to rest on a corpus that can be re-read and re-checked. A CSV read
inline in a notebook is not that. This is.

What it does, and why each part earns its place:

* PARSES the trader's own notes into structure. "Added at .57/.65", "sold remaining calls at
  25%", "updates 150%, 300%, 400%" are the actual execution history, and they were previously
  prose that a human re-read by eye. Here they become adds, scale-outs and targets.
* REFUSES to invent. A row that cannot be identified or whose numbers are impossible becomes a
  named rejection with a reason, and rejections are REPORTED rather than dropped. Silent data
  loss is how a backtest flatters itself.
* EXPRESSES everything in R, not dollars. A $1,500 clip in a $1.30 contract and a $1,500 clip
  in a $0.40 contract are not the same risk, so comparing them in dollars compares position
  sizes rather than decisions. R makes the decision the unit.
* DIGESTS the corpus. `manifest()` gives a sha256 over canonical rows plus counts, coverage and
  parse attribution, so a research claim can name exactly the data it was computed from.

Deliberately absent: any network access, any broker concept, any assumption about contract
multiplier beyond the 100 that US equity options use.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

MULTIPLIER = Decimal(100)
EASTERN = timedelta(hours=-4)          # EDT for the corpus window; a fixed offset, not a guess
OUTCOMES = ("win", "win_partial", "loss", "scratch", "unknown")

Outcome = Literal["win", "win_partial", "loss", "scratch", "unknown"]
ExitKind = Literal["full", "partial", "stopped", "unknown"]


class LedgerError(ValueError):
    """A corpus row that cannot be trusted, with a stable code for aggregation."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class Rejected:
    line: int
    code: str
    detail: str
    raw: str = ""


@dataclass(frozen=True, slots=True)
class Lifecycle:
    """The execution story behind a trade, extracted from the trader's notes."""

    adds: tuple[Decimal, ...] = ()
    targets: tuple[int, ...] = ()
    exit_kind: ExitKind = "unknown"
    exit_pct: int | None = None
    stopped: bool = False

    @property
    def added(self) -> bool:
        return bool(self.adds)

    @property
    def scaled_out(self) -> bool:
        return self.exit_kind in ("partial", "full")

    def to_dict(self) -> dict[str, Any]:
        return {
            "adds": [str(a) for a in self.adds],
            "targets": list(self.targets),
            "exit_kind": self.exit_kind,
            "exit_pct": self.exit_pct,
            "stopped": self.stopped,
        }


@dataclass(frozen=True, slots=True)
class Trade:
    """One alert-to-exit cycle, normalised. R is the unit; dollars are derived."""

    channel: str
    day: date
    ts_utc: datetime
    ticker: str
    side: Literal["CALL", "PUT"]
    strike: Decimal
    expiry_kind: str                 # "today" | ISO date
    entry_bid: Decimal
    exit_price: Decimal | None
    outcome: Outcome
    lifecycle: Lifecycle
    notes: str = ""
    line: int = 0

    @property
    def contract_key(self) -> str:
        return f"{self.channel}|{self.day.isoformat()}|{self.ticker}|{self.side}|{self.strike}"

    @property
    def identity(self) -> str:
        """Unique per (contract, entry minute). A re-alert 17 minutes later is a second row."""
        return f"{self.contract_key}|{self.ts_utc:%H:%M}"

    @property
    def key(self) -> str:
        return self.identity

    @property
    def entry_ts_utc(self) -> datetime:
        return self.ts_utc

    @property
    def multiple(self) -> Decimal | None:
        """Realised multiple on premium: 1.25 means +25% on the contract."""
        if self.exit_price is not None and self.entry_bid > 0:
            return (self.exit_price / self.entry_bid).quantize(Decimal("0.0001"))
        if self.lifecycle.exit_pct is not None:
            return (Decimal(1) + Decimal(self.lifecycle.exit_pct) / Decimal(100)).quantize(
                Decimal("0.0001")
            )
        return None

    @property
    def r_multiple(self) -> Decimal | None:
        """Return in units of premium at risk. -1 is a total loss, which is the floor."""
        m = self.multiple
        return None if m is None else (m - Decimal(1)).quantize(Decimal("0.0001"))

    @property
    def risk_usd(self) -> Decimal:
        return (self.entry_bid * MULTIPLIER).quantize(Decimal("0.01"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "day": self.day.isoformat(),
            "ts_utc": self.ts_utc.isoformat(),
            "ticker": self.ticker,
            "side": self.side,
            "strike": str(self.strike),
            "expiry_kind": self.expiry_kind,
            "entry_bid": str(self.entry_bid),
            "exit_price": str(self.exit_price) if self.exit_price is not None else None,
            "outcome": self.outcome,
            "r_multiple": str(self.r_multiple) if self.r_multiple is not None else None,
            "lifecycle": self.lifecycle.to_dict(),
        }


# ── note parsing ─────────────────────────────────────────────────────────────
_ADD_PRICES = re.compile(r"add(?:ed)?\s+(?:at\s+)?((?:[\d.]+\s*[/,]\s*)*[\d.]+)", re.I)
_ADD_TIME = re.compile(r"add(?:ed)?\s+(?:at\s+)?(\d{1,2}:\d{2})", re.I)
_PCTS = re.compile(r"(\d{1,4})\s*%")
_SOLD_REMAINING = re.compile(r"sold\s+(?:the\s+)?remain(?:ing|der)", re.I)
_SOLD_AT = re.compile(r"sold\s+([\d.]+)", re.I)
_TRIM = re.compile(r"\btrim|\bhalf\b|\bpartial|\bscale(?:d)?\s*out", re.I)
_STOP = re.compile(r"\bstop(?:ped)?(?:\s+out)?|\bcut\b|\bflat\b", re.I)


def parse_lifecycle(notes: str, *, exit_pct: int | None) -> Lifecycle:
    """Extract adds, targets and exit character. Never raises: unknown is a valid answer."""
    adds: list[Decimal] = []
    for match in _ADD_PRICES.finditer(notes or ""):
        for part in re.split(r"[/,\s]+", match.group(1).strip()):
            if not part:
                continue
            try:
                value = Decimal(part)
            except InvalidOperation:
                continue
            if Decimal("0") < value < Decimal("100"):
                adds.append(value)

    targets = tuple(sorted({int(m.group(1)) for m in _PCTS.finditer(notes or "")
                            if 1 <= int(m.group(1)) <= 10_000}))

    stopped = bool(_STOP.search(notes or ""))
    if _SOLD_REMAINING.search(notes or ""):
        kind: ExitKind = "full"
    elif stopped:
        kind = "stopped"
    elif _TRIM.search(notes or ""):
        kind = "partial"
    elif exit_pct is not None:
        kind = "full" if exit_pct >= 100 else "partial"
    else:
        kind = "unknown"

    return Lifecycle(adds=tuple(adds), targets=targets, exit_kind=kind,
                     exit_pct=exit_pct, stopped=stopped)


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise LedgerError("bad_date", text) from exc


def _parse_et_clock(text: str) -> time:
    raw = (text or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        raise LedgerError("bad_time", raw or "<empty>")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise LedgerError("bad_time", raw)
    return time(hour=hour, minute=minute)


def _decimal(text: str | None, *, field_name: str) -> Decimal | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise LedgerError(f"bad_{field_name}", raw) from exc
    return value


def parse_row(row: dict[str, str], *, line: int = 0) -> Trade:
    """One CSV row to a Trade, or a named rejection."""
    channel = (row.get("channel") or "").strip()
    if not channel:
        raise LedgerError("missing_channel", f"line {line}")

    day = _parse_day(row.get("date") or "")
    clock = _parse_et_clock(row.get("time_et") or "")
    ts_utc = datetime.combine(day, clock) - EASTERN

    ticker = (row.get("ticker") or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z.]{0,5}", ticker or ""):
        raise LedgerError("bad_ticker", ticker or "<empty>")

    side_raw = (row.get("side") or "").strip().upper().rstrip("S")
    if side_raw not in ("CALL", "PUT"):
        raise LedgerError("bad_side", side_raw or "<empty>")
    side: Literal["CALL", "PUT"] = "CALL" if side_raw == "CALL" else "PUT"

    strike = _decimal(row.get("strike"), field_name="strike")
    if strike is None or not (Decimal("0") < strike < Decimal("100000")):
        raise LedgerError("bad_strike", str(row.get("strike")))

    entry = _decimal(row.get("entry_bid"), field_name="entry_bid")
    if entry is None or entry <= 0:
        raise LedgerError("bad_entry", str(row.get("entry_bid")))

    exit_price = _decimal(row.get("exit_price"), field_name="exit_price")
    if exit_price is not None and exit_price < 0:
        raise LedgerError("bad_exit", str(exit_price))

    pct_raw = (row.get("exit_pct") or "").strip()
    exit_pct: int | None = None
    if pct_raw:
        pct = _decimal(pct_raw, field_name="exit_pct")
        if pct is None or pct < -100 or pct > 10_000:
            raise LedgerError("exit_out_of_range", pct_raw)
        exit_pct = int(pct)

    outcome_raw = (row.get("outcome") or "unknown").strip().lower()
    if outcome_raw not in OUTCOMES:
        raise LedgerError("bad_outcome", outcome_raw)
    outcome: Outcome = outcome_raw  # type: ignore[assignment]

    expiry_raw = (row.get("expiry") or "").strip().lower()
    expiry_kind = "today" if expiry_raw in ("today", "0dte", "same-day") else expiry_raw
    if expiry_kind != "today":
        try:
            date.fromisoformat(expiry_kind)
        except ValueError as exc:
            raise LedgerError("bad_expiry", expiry_raw or "<empty>") from exc

    notes = (row.get("notes") or "").strip()
    return Trade(
        channel=channel, day=day, ts_utc=ts_utc, ticker=ticker, side=side, strike=strike,
        expiry_kind=expiry_kind, entry_bid=entry, exit_price=exit_price, outcome=outcome,
        lifecycle=parse_lifecycle(notes, exit_pct=exit_pct), notes=notes, line=line,
    )


# ── the ledger ───────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Ledger:
    trades: tuple[Trade, ...]
    rejected: tuple[Rejected, ...] = ()
    source_digest: str = ""

    def __post_init__(self) -> None:
        days = [t.day for t in self.trades]
        if days != sorted(days):
            raise LedgerError("unsorted", "trades must be in non-decreasing day order")
        identities = Counter(t.identity for t in self.trades)
        exact = [key for key, n in identities.items() if n > 1]
        if exact:
            raise LedgerError(
                "duplicate_identity",
                f"{len(exact)} rows repeat a contract at the same minute",
            )

    # ── views ────────────────────────────────────────────────────────────────
    def by_channel(self) -> dict[str, tuple[Trade, ...]]:
        out: dict[str, list[Trade]] = {}
        for trade in self.trades:
            out.setdefault(trade.channel, []).append(trade)
        return {k: tuple(v) for k, v in sorted(out.items())}

    def by_day(self) -> dict[date, tuple[Trade, ...]]:
        out: dict[date, list[Trade]] = {}
        for trade in self.trades:
            out.setdefault(trade.day, []).append(trade)
        return {k: tuple(v) for k, v in sorted(out.items())}

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(sorted({t.channel for t in self.trades}))

    @property
    def window(self) -> tuple[date, date] | None:
        if not self.trades:
            return None
        return self.trades[0].day, max(t.day for t in self.trades)

    @property
    def with_r(self) -> tuple[Trade, ...]:
        return tuple(t for t in self.trades if t.r_multiple is not None)

    def rs(self) -> list[Decimal]:
        return [t.r_multiple for t in self.with_r if t.r_multiple is not None]

    # ── statistics that do not flatter themselves ────────────────────────────
    def outcome_mix(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for trade in self.trades:
            out[trade.outcome] = out.get(trade.outcome, 0) + 1
        return dict(sorted(out.items()))

    def r_summary(self) -> dict[str, Any]:
        rs = self.rs()
        if not rs:
            return {"n": 0, "resolved": 0, "unresolved": len(self.trades)}
        total = sum(rs, Decimal(0))
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r < 0]
        gross_win = sum(wins, Decimal(0))
        gross_loss = -sum(losses, Decimal(0))
        return {
            "n": len(self.trades),
            "resolved": len(rs),
            "unresolved": len(self.trades) - len(rs),
            "total_r": str(total.quantize(Decimal("0.01"))),
            "mean_r": str((total / len(rs)).quantize(Decimal("0.0001"))),
            "median_r": str(sorted(rs)[len(rs) // 2].quantize(Decimal("0.0001"))),
            "win_rate": round(100.0 * len(wins) / len(rs), 2),
            "avg_win_r": str((gross_win / len(wins)).quantize(Decimal("0.0001"))) if wins else "0",
            "avg_loss_r": (
                str((gross_loss / len(losses)).quantize(Decimal("0.0001"))) if losses else "0"
            ),
            "profit_factor": (
                str((gross_win / gross_loss).quantize(Decimal("0.01"))) if gross_loss else None
            ),
            "best_r": str(max(rs)), "worst_r": str(min(rs)),
        }

    def per_channel_r(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for channel, trades in self.by_channel().items():
            rs = [t.r_multiple for t in trades if t.r_multiple is not None]
            rs_clean = [r for r in rs if r is not None]
            if not rs_clean:
                out[channel] = {"n": len(trades), "resolved": 0}
                continue
            total = sum(rs_clean, Decimal(0))
            out[channel] = {
                "n": len(trades),
                "resolved": len(rs_clean),
                "mean_r": str((total / len(rs_clean)).quantize(Decimal("0.0001"))),
                "total_r": str(total.quantize(Decimal("0.01"))),
                "win_rate": round(100.0 * sum(1 for r in rs_clean if r > 0) / len(rs_clean), 2),
            }
        return out

    def near_duplicates(self, window_minutes: int = 30) -> list[dict[str, Any]]:
        """Same contract, same day, entries inside a window: one trade or two?

        The corpus contains a genuine case - NVDA 215C alerted at 06:23 and again at 06:40,
        both recorded as +75%. Silently keeping both double-counts the edge; silently
        dropping one discards the trader's own record. So it is surfaced, and the caller
        decides. Research that ignores this list is measuring the corpus, not the market.
        """
        groups: dict[str, list[Trade]] = {}
        for trade in self.trades:
            groups.setdefault(trade.contract_key, []).append(trade)

        found: list[dict[str, Any]] = []
        for key, group in groups.items():
            if len(group) < 2:
                continue
            ordered = sorted(group, key=lambda t: t.ts_utc)
            for earlier, later in zip(ordered, ordered[1:], strict=False):
                gap = (later.ts_utc - earlier.ts_utc).total_seconds() / 60
                if gap <= window_minutes:
                    found.append({
                        "contract_key": key,
                        "gap_minutes": round(gap, 1),
                        "identities": [earlier.identity, later.identity],
                        "same_outcome": earlier.outcome == later.outcome,
                    })
        return found

    def lifecycle_mix(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for trade in self.trades:
            out[trade.lifecycle.exit_kind] = out.get(trade.lifecycle.exit_kind, 0) + 1
        return dict(sorted(out.items()))

    # ── reproducibility ──────────────────────────────────────────────────────
    def manifest(self) -> dict[str, Any]:
        canonical = json.dumps([t.to_dict() for t in self.trades],
                               sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        window = self.window
        return {
            "digest": digest,
            "source_digest": self.source_digest,
            "trades": len(self.trades),
            "rejected": len(self.rejected),
            "channels": list(self.channels),
            "first_day": window[0].isoformat() if window else None,
            "last_day": window[1].isoformat() if window else None,
            "trading_days": len(self.by_day()),
            "outcomes": self.outcome_mix(),
            "exit_kinds": self.lifecycle_mix(),
            "rejection_codes": _count_codes(self.rejected),
            "near_duplicates": len(self.near_duplicates()),
        }

    def to_alerts(self) -> list[dict[str, str]]:
        """The corpus as alert-shaped records, for the parser/engine to replay.

        The ledger is the researcher's view; this is the executor's. Keeping them as separate
        projections of the same rows is what stops research and execution drifting apart.
        """
        return [
            {
                "channel": t.channel,
                "day": t.day.isoformat(),
                "ts_utc": t.ts_utc.isoformat(),
                "text": (
                    f"{t.ticker} {t.strike}{'C' if t.side == 'CALL' else 'P'} "
                    f"{t.expiry_kind if t.expiry_kind != 'today' else 'TODAY'} @ {t.entry_bid}"
                ),
            }
            for t in self.trades
        ]


def _count_codes(rejected: Iterable[Rejected]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in rejected:
        out[item.code] = out.get(item.code, 0) + 1
    return dict(sorted(out.items()))


def load_csv(path: str | Path) -> Ledger:
    """Read the corpus. Rejections are collected, never silently skipped."""
    source = Path(path)
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    trades: list[Trade] = []
    rejected: list[Rejected] = []
    seen: set[str] = set()

    for index, row in enumerate(reader, start=2):
        try:
            trade = parse_row(row, line=index)
        except LedgerError as exc:
            rejected.append(Rejected(line=index, code=exc.code, detail=exc.detail,
                                     raw=(row.get("ticker") or "")))
            continue
        if trade.identity in seen:
            rejected.append(Rejected(line=index, code="duplicate_identity",
                                     detail=trade.identity, raw=trade.ticker))
            continue
        seen.add(trade.identity)
        trades.append(trade)
    trades.sort(key=lambda t: (t.day, t.ts_utc, t.channel))
    return Ledger(trades=tuple(trades), rejected=tuple(rejected), source_digest=digest)


def build_report(ledger: Ledger) -> dict[str, Any]:
    return {
        "manifest": ledger.manifest(),
        "r": ledger.r_summary(),
        "per_channel": ledger.per_channel_r(),
        "rejections": [
            {"line": r.line, "code": r.code, "detail": r.detail} for r in ledger.rejected
        ],
    }
