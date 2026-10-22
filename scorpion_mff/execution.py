"""Confirmed execution state: the book the broker would recognise.

The engine decides; the broker fills. Between those two facts there was no durable
state at all:

* a rejected buy stayed in the engine's position book (the listener only logged it);
* a partial fill was recorded as the full clip;
* a rejected sell had already moved realised P&L and the governor;
* a restart forgot every open position, so the bot could re-enter or fail to exit;
* `ack.ok=False` was ignored entirely.

`ExecutionBook` is the missing authority. It records every intent before the broker
is called, applies only **acknowledged** fills, and exposes a `sync_engine()` that
rebuilds the engine's positions/realised state from confirmed events. The engine is a
decision cache; the execution book is the truth.

Design rules
------------
* **Append-only.** `live/execution.jsonl` is a replayable event log: intent, ack,
  fill, reject, baseline. A torn final line is ignored; corruption in the middle is a
  refusal, never a silent reset.
* **Cumulative acks.** `Ack.qty` is the total filled quantity for that order. A second
  ack of 3 after one of 2 applies the delta (1), so duplicate broker responses cannot
  double-fill.
* **Never oversell.** A sell larger than the lot is clamped and counted as an anomaly;
  the book does not invent contracts.
* **Baseline, not amnesia.** When the book is introduced mid-history, it persists the
  governor's prior equity/peak/day counters once, so the first sync cannot erase a
  drawdown. New realised P&L is added on top.
* **Restart is a replay.** `load()` reconstructs orders, lots, realised trades and
  closed keys from the log; `sync_engine()` restores the engine before the first
  message is handled.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .broker import Ack
from .types import OrderIntent, Position, Side

OPTION_MULTIPLIER = 100.0
SCHEMA = "mff.execution.v1"

STATE_PENDING = "PENDING"
STATE_PARTIAL = "PARTIAL"
STATE_FILLED = "FILLED"
STATE_REJECTED = "REJECTED"
STATE_CANCELLED = "CANCELLED"
TERMINAL = {STATE_FILLED, STATE_REJECTED, STATE_CANCELLED}


class ExecutionError(ValueError):
    """The execution log cannot be trusted, or an event would corrupt the book."""


def utc_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


@dataclass
class ExecFill:
    client_id: str
    contract_key: str
    action: str
    qty: int
    price: float
    ts: float
    broker_order_id: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {"client_id": self.client_id, "contract_key": self.contract_key,
                "action": self.action, "qty": self.qty, "price": round(self.price, 4),
                "ts": self.ts, "broker_order_id": self.broker_order_id,
                "reason": self.reason}


@dataclass
class ExecOrder:
    client_id: str
    intent: OrderIntent
    channel: str
    created_ts: float
    state: str = STATE_PENDING
    filled_qty: int = 0
    avg_price: float = 0.0
    broker_order_id: str = ""
    reason: str = ""
    first_fill_ts: float | None = None
    source_id: str = ""

    @property
    def action(self) -> str:
        return self.intent.action

    @property
    def contract_key(self) -> str:
        return self.intent.contract_key

    @property
    def remaining(self) -> int:
        return max(0, self.intent.qty - self.filled_qty)

    def to_dict(self) -> dict:
        return {"client_id": self.client_id, "action": self.action,
                "contract_key": self.contract_key, "channel": self.channel,
                "intent_qty": self.intent.qty, "limit": self.intent.limit,
                "filled_qty": self.filled_qty, "avg_price": round(self.avg_price, 4),
                "state": self.state, "broker_order_id": self.broker_order_id,
                "created_ts": self.created_ts, "first_fill_ts": self.first_fill_ts,
                "reason": self.intent.reason, "error": self.reason,
                "source_id": self.source_id,
                "idempotency_key": self.intent.idempotency_key}


@dataclass
class ExecLot:
    contract_key: str
    ticker: str
    side: Side
    qty: int
    avg_price: float
    opened_ts: float
    channel: str = ""
    adds: int = 0
    trimmed: int = 0
    breakeven_stop: float | None = None

    def to_position(self) -> Position:
        return Position(contract_key=self.contract_key, ticker=self.ticker,
                        side=self.side, contracts=self.qty,
                        avg_price=self.avg_price, opened_ts=self.opened_ts,
                        channel=self.channel, adds=self.adds, trimmed=self.trimmed,
                        breakeven_stop=self.breakeven_stop)

    def to_dict(self) -> dict:
        return {"contract_key": self.contract_key, "ticker": self.ticker,
                "side": self.side.value, "qty": self.qty,
                "avg_price": round(self.avg_price, 4), "opened_ts": self.opened_ts,
                "channel": self.channel, "adds": self.adds, "trimmed": self.trimmed,
                "breakeven_stop": self.breakeven_stop}


@dataclass(frozen=True)
class RealizedTrade:
    contract_key: str
    ticker: str
    channel: str
    qty: int
    entry_price: float
    exit_price: float
    pnl_usd: float
    ts: float
    client_id: str = ""

    def to_dict(self) -> dict:
        return {"contract_key": self.contract_key, "ticker": self.ticker,
                "channel": self.channel, "qty": self.qty,
                "entry_price": round(self.entry_price, 4),
                "exit_price": round(self.exit_price, 4),
                "pnl_usd": round(self.pnl_usd, 2), "ts": self.ts,
                "client_id": self.client_id}


@dataclass
class Baseline:
    equity_usd: float = 0.0
    peak_equity_usd: float = 0.0
    day: str = ""
    realized_today_usd: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0

    def to_dict(self) -> dict:
        return {"equity_usd": round(self.equity_usd, 2),
                "peak_equity_usd": round(self.peak_equity_usd, 2),
                "day": self.day, "realized_today_usd": round(self.realized_today_usd, 2),
                "trades_today": self.trades_today,
                "consecutive_losses": self.consecutive_losses}


@dataclass
class ExecutionBook:
    """Append-only confirmed state. Construct with `path` to persist."""

    path: Path | None = None
    fsync: bool = False
    orders: dict[str, ExecOrder] = field(default_factory=dict)
    lots: dict[str, ExecLot] = field(default_factory=dict)
    fills: list[ExecFill] = field(default_factory=list)
    realized: list[RealizedTrade] = field(default_factory=list)
    realized_total: float = 0.0
    peak_realized: float = 0.0
    closed_by_day: dict[str, set[str]] = field(default_factory=dict)
    baseline: Baseline | None = None
    anomalies: dict[str, int] = field(default_factory=dict)
    _seq: int = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, path: str | Path) -> ExecutionBook:
        p = Path(path)
        book = cls(path=p)
        if not p.exists():
            return book
        raw_lines = p.read_text(encoding="utf-8").splitlines()
        meaningful = [(i, line.strip()) for i, line in enumerate(raw_lines) if line.strip()]
        for index, (line_no, line) in enumerate(meaningful):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                # A crash can tear the last append. Anything earlier is corruption.
                if index == len(meaningful) - 1:
                    continue
                raise ExecutionError(
                    f"execution log {p}:{line_no + 1} is corrupt: {exc}") from None
            if not isinstance(event, Mapping):
                raise ExecutionError(f"execution log {p}:{line_no + 1} is not an object")
            book._apply(event)
        return book

    def ensure_baseline(self, *, equity_usd: float, peak_equity_usd: float,
                        day: str, realized_today_usd: float,
                        trades_today: int, consecutive_losses: int) -> None:
        """Persist the pre-book risk state once. Never overwrites an existing one."""
        if self.baseline is not None:
            return
        event = {"v": 1, "t": "baseline", "equity": round(float(equity_usd), 2),
                 "peak": round(float(peak_equity_usd), 2), "day": day,
                 "realized_today": round(float(realized_today_usd), 2),
                 "trades_today": int(trades_today),
                 "losses": int(consecutive_losses)}
        self._apply(event)
        self._emit(event)

    # ── recording ─────────────────────────────────────────────────────────────
    @staticmethod
    def client_id(intent: OrderIntent, ts: float, source_id: str = "") -> str:
        base = intent.idempotency_key or f"{intent.contract_key}:{intent.action}"
        return f"{base}@{utc_day(ts)}" + (f"@{source_id}" if source_id else "")

    def record_intent(self, intent: OrderIntent, *, ts: float,
                      channel: str = "", source_id: str = "") -> ExecOrder:
        cid = self.client_id(intent, ts, source_id)
        existing = self.orders.get(cid)
        if existing is not None:
            if (existing.contract_key != intent.contract_key
                    or existing.action != intent.action
                    or existing.intent.qty != intent.qty):
                raise ExecutionError(f"{cid}: duplicate client id with different order")
            return existing
        event = {"v": 1, "t": "intent", "cid": cid, "ts": ts,
                 "action": intent.action, "contract_key": intent.contract_key,
                 "qty": intent.qty, "limit": intent.limit, "channel": channel,
                 "reason": intent.reason, "source_id": source_id,
                 "idem": intent.idempotency_key}
        self._apply(event)
        self._emit(event)
        return self.orders[cid]

    def apply_ack(self, cid: str, ack: Ack, *, ts: float) -> ExecOrder:
        order = self._order(cid)
        if not ack.ok:
            return self.reject(cid, reason=ack.error or "broker_rejected", ts=ts)
        if ack.contract_key and ack.contract_key != order.contract_key:
            raise ExecutionError(
                f"{cid}: ack contract {ack.contract_key} != intent {order.contract_key}")
        cumulative = int(ack.qty)
        if cumulative < 0:
            raise ExecutionError(f"{cid}: negative ack qty")
        if cumulative > order.intent.qty:
            self._anomaly("ack_qty_exceeds_intent")
            cumulative = order.intent.qty
        if cumulative < order.filled_qty:
            raise ExecutionError(
                f"{cid}: ack regressed from {order.filled_qty} to {cumulative}")
        event = {"v": 1, "t": "ack", "cid": cid, "ts": ts, "qty": cumulative,
                 "price": float(ack.price or order.intent.limit),
                 "broker_id": ack.broker_order_id, "ok": True}
        self._apply(event)
        self._emit(event)
        return self.orders[cid]

    def record_fill(self, cid: str, *, qty: int, price: float, ts: float,
                    broker_order_id: str = "") -> ExecOrder:
        """Incremental late fill (e.g. a polled partial). `qty` is NEW, not cumulative."""
        if qty <= 0 or price < 0:
            raise ExecutionError(f"{cid}: invalid fill qty/price")
        event = {"v": 1, "t": "fill", "cid": cid, "ts": ts, "qty": qty,
                 "price": price, "broker_id": broker_order_id}
        self._apply(event)
        self._emit(event)
        return self.orders[cid]

    def reject(self, cid: str, *, reason: str, ts: float) -> ExecOrder:
        event = {"v": 1, "t": "reject", "cid": cid, "ts": ts, "reason": reason[:200]}
        self._apply(event)
        self._emit(event)
        return self.orders[cid]

    def expire_carried(self, day: str, ts: float, *,
                       assume_zero: bool = True) -> int:
        """Close lots whose contract can no longer be traded.

        A restart can land after expiry. Leaving the lot open would hold a slot and
        make the risk budget count premium that no longer exists. With no settlement
        feed, a long option is marked at zero (the maximum loss) and the event is
        flagged `assumed_zero`; this is conservative and visible, not a guess at an
        ITM settlement.
        """
        expired: list[ExecLot] = []
        for lot in list(self.lots.values()):
            expiry = lot.contract_key.rsplit("|", 1)[-1]
            if expiry.upper() == "0DTE":
                if utc_day(lot.opened_ts) < day:
                    expired.append(lot)
            else:
                try:
                    iso = time.strftime("%Y-%m-%d", time.strptime(expiry, "%Y-%m-%d"))
                except (ValueError, TypeError):
                    continue
                if iso < day:
                    expired.append(lot)
        for lot in expired:
            event = {"v": 1, "t": "expire", "key": lot.contract_key,
                     "qty": lot.qty, "ts": ts, "price": 0.0 if assume_zero else None,
                     "assumed_zero": assume_zero}
            self._apply(event)
            self._emit(event)
        return len(expired)

    # ── derived state ─────────────────────────────────────────────────────────
    def open_positions(self) -> list[Position]:
        return [lot.to_position() for lot in self.lots.values() if lot.qty > 0]

    def closed_keys(self, day: str) -> set[str]:
        return set(self.closed_by_day.get(day, set()))

    def realized_on(self, day: str) -> float:
        return sum(t.pnl_usd for t in self.realized if utc_day(t.ts) == day)

    def trades_on(self, day: str) -> int:
        return sum(1 for t in self.realized if utc_day(t.ts) == day)

    def entries_today(self, day: str) -> int:
        return sum(1 for o in self.orders.values()
                   if o.action == "buy" and o.filled_qty > 0
                   and o.first_fill_ts is not None
                   and utc_day(o.first_fill_ts) == day
                   and not o.intent.reason.startswith("add:"))

    def loss_streak(self, day: str) -> int:
        streak = 0
        for trade in self.realized:
            if utc_day(trade.ts) != day:
                continue
            streak = streak + 1 if trade.pnl_usd < 0 else 0
        return streak

    def snapshot(self) -> dict:
        return {
            "schema": SCHEMA,
            "orders": len(self.orders),
            "pending": sum(1 for o in self.orders.values()
                           if o.state in (STATE_PENDING, STATE_PARTIAL)),
            "positions": [p.contract_key for p in self.open_positions()],
            "realized_total_usd": round(self.realized_total, 2),
            "peak_realized_usd": round(self.peak_realized, 2),
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "anomalies": dict(self.anomalies),
        }

    def report_text(self) -> str:
        snap = self.snapshot()
        lines = [
            (f"execution book | {snap['orders']} orders | "
             f"{snap['pending']} pending | {len(snap['positions'])} position(s) | "
             f"realised ${snap['realized_total_usd']:,.2f}"),
        ]
        if snap["baseline"]:
            base = snap["baseline"]
            lines.append(f"  baseline equity ${base['equity_usd']:,.2f} | "
                         f"peak ${base['peak_equity_usd']:,.2f} | day {base['day']}")
        for order in self.orders.values():
            lines.append(f"  {order.state:<9} {order.action:<4} {order.contract_key:<28} "
                         f"{order.filled_qty}/{order.intent.qty} @ "
                         f"{order.avg_price or order.intent.limit:g}  {order.reason}")
        for lot in self.open_positions():
            lines.append(f"  LOT       {lot.contract_key:<28} {lot.contracts} @ "
                         f"{lot.avg_price:g}  {lot.channel}")
        if self.anomalies:
            lines.append(f"  anomalies: {self.anomalies}")
        return "\n".join(lines)

    def sync_engine(self, engine: Any, now: float) -> None:
        """Replace the engine's optimistic book with confirmed execution state."""
        day = utc_day(now)
        self.expire_carried(day, now)
        base = self.baseline or Baseline()
        base_today = base.day == day

        engine.positions = {p.contract_key: p for p in self.open_positions()}
        engine.closed = set(self.closed_by_day.get(day, set()))
        engine.stats.realized_usd = base.equity_usd + self.realized_total
        engine.stats.trades_today = (base.trades_today if base_today else 0) \
            + self.entries_today(day)

        risk = engine.governor_state
        risk.day = day
        risk.realized_today_usd = ((base.realized_today_usd if base_today else 0.0)
                                   + self.realized_on(day))
        risk.trades_today = engine.stats.trades_today
        risk.equity_usd = base.equity_usd + self.realized_total
        path_peak = max(0.0, self.peak_realized)
        risk.peak_equity_usd = max(base.peak_equity_usd,
                                   base.equity_usd + path_peak,
                                   risk.equity_usd)

        book_streak = self.loss_streak(day)
        book_trades = self.trades_on(day)
        if book_trades == 0:
            risk.consecutive_losses = base.consecutive_losses if base_today else 0
        elif book_streak == book_trades and base_today:
            # Every book close today was a loss, so the pre-book streak continues.
            risk.consecutive_losses = base.consecutive_losses + book_streak
        else:
            risk.consecutive_losses = book_streak

    # ── event application (shared by live methods and replay) ─────────────────
    def _order(self, cid: str) -> ExecOrder:
        order = self.orders.get(cid)
        if order is None:
            raise ExecutionError(f"unknown client order id {cid}")
        return order

    def _apply(self, event: Mapping[str, Any]) -> None:
        kind = event.get("t")
        if kind == "baseline":
            if self.baseline is None:
                self.baseline = Baseline(
                    equity_usd=float(event.get("equity") or 0.0),
                    peak_equity_usd=float(event.get("peak") or 0.0),
                    day=str(event.get("day") or ""),
                    realized_today_usd=float(event.get("realized_today") or 0.0),
                    trades_today=int(event.get("trades_today") or 0),
                    consecutive_losses=int(event.get("losses") or 0))
            return
        if kind == "intent":
            cid = str(event.get("cid") or "")
            if not cid:
                raise ExecutionError("intent without client id")
            intent = OrderIntent(
                action=str(event.get("action") or ""),
                qty=int(event.get("qty") or 0),
                limit=float(event.get("limit") or 0.0),
                contract_key=str(event.get("contract_key") or ""),
                reason=str(event.get("reason") or ""),
                idempotency_key=str(event.get("idem") or cid))
            if self.orders.get(cid) is None:
                self.orders[cid] = ExecOrder(
                    client_id=cid, intent=intent,
                    channel=str(event.get("channel") or ""),
                    created_ts=float(event.get("ts") or 0.0),
                    source_id=str(event.get("source_id") or ""))
            return
        if kind == "ack":
            order = self._order(str(event.get("cid") or ""))
            if not bool(event.get("ok", True)):
                self._reject(order, str(event.get("reason") or "broker_rejected"),
                             float(event.get("ts") or 0.0))
                return
            cumulative = max(0, int(event.get("qty") or 0))
            cumulative = min(cumulative, order.intent.qty)
            if cumulative > order.filled_qty:
                delta = cumulative - order.filled_qty
                self._apply_fill(order, delta, float(event.get("price") or 0.0),
                                 float(event.get("ts") or 0.0),
                                 str(event.get("broker_id") or ""))
                order.filled_qty = cumulative
            elif cumulative < order.filled_qty:
                raise ExecutionError(f"{order.client_id}: ack regressed on replay")
            order.broker_order_id = str(event.get("broker_id") or order.broker_order_id)
            order.state = (STATE_FILLED if order.filled_qty >= order.intent.qty
                           else STATE_PARTIAL)
            return
        if kind == "fill":
            order = self._order(str(event.get("cid") or ""))
            delta = max(0, int(event.get("qty") or 0))
            delta = min(delta, max(0, order.intent.qty - order.filled_qty))
            if delta:
                self._apply_fill(order, delta, float(event.get("price") or 0.0),
                                 float(event.get("ts") or 0.0),
                                 str(event.get("broker_id") or ""))
                order.filled_qty += delta
                order.state = (STATE_FILLED if order.filled_qty >= order.intent.qty
                               else STATE_PARTIAL)
            return
        if kind == "reject":
            order = self._order(str(event.get("cid") or ""))
            self._reject(order, str(event.get("reason") or "broker_rejected"),
                         float(event.get("ts") or 0.0))
            return
        if kind == "expire":
            self._apply_expiry(event)
            return
        raise ExecutionError(f"unknown execution event {kind!r}")

    def _reject(self, order: ExecOrder, reason: str, ts: float) -> None:
        if order.state == STATE_FILLED:
            return
        order.reason = reason
        order.state = STATE_CANCELLED if order.filled_qty > 0 else STATE_REJECTED

    def _apply_fill(self, order: ExecOrder, qty: int, price: float, ts: float,
                    broker_id: str) -> None:
        if qty <= 0:
            return
        if not math.isfinite(price) or price < 0:
            raise ExecutionError(f"{order.client_id}: invalid fill price {price!r}")
        if order.first_fill_ts is None:
            order.first_fill_ts = ts
        key = order.contract_key
        ticker = key.split("|")[0] if key else ""
        side = Side.CALL if "|CALL|" in key else Side.PUT
        recorded = qty
        if order.action == "buy":
            lot = self.lots.get(key)
            if lot is None:
                lot = ExecLot(contract_key=key, ticker=ticker, side=side, qty=qty,
                              avg_price=price, opened_ts=ts, channel=order.channel)
                self.lots[key] = lot
            else:
                total = lot.qty + qty
                lot.avg_price = (lot.avg_price * lot.qty + price * qty) / total
                lot.qty = total
                lot.opened_ts = min(lot.opened_ts, ts)
            if order.intent.reason.startswith("add:"):
                lot.adds += 1
        elif order.action == "sell":
            lot = self.lots.get(key)
            if lot is None:
                self._anomaly("sell_without_lot")
                return
            executed = min(qty, lot.qty)
            if executed < qty:
                self._anomaly("oversell_clamped")
            if executed <= 0:
                return
            recorded = executed
            pnl = (price - lot.avg_price) * executed * OPTION_MULTIPLIER
            self.realized.append(RealizedTrade(
                contract_key=key, ticker=lot.ticker, channel=lot.channel,
                qty=executed, entry_price=lot.avg_price, exit_price=price,
                pnl_usd=pnl, ts=ts, client_id=order.client_id))
            self.realized_total += pnl
            self.peak_realized = max(self.peak_realized, self.realized_total)
            lot.qty -= executed
            if order.intent.reason.startswith("trim:"):
                lot.trimmed += 1
                lot.breakeven_stop = lot.avg_price
            if lot.qty <= 0:
                self.closed_by_day.setdefault(utc_day(ts), set()).add(key)
                del self.lots[key]
        else:
            raise ExecutionError(f"{order.client_id}: unknown action {order.action!r}")
        self.fills.append(ExecFill(
            client_id=order.client_id, contract_key=key, action=order.action,
            qty=recorded, price=price, ts=ts, broker_order_id=broker_id,
            reason=order.intent.reason))

    def _apply_expiry(self, event: Mapping[str, Any]) -> None:
        key = str(event.get("key") or "")
        lot = self.lots.get(key)
        if lot is None:
            return
        ts = float(event.get("ts") or 0.0)
        qty = min(int(event.get("qty") or lot.qty), lot.qty)
        price = event.get("price")
        if price is None:
            # No settlement feed and no assumption: release the slot, do not
            # manufacture P&L. The anomaly is the operator's signal to reconcile.
            del self.lots[key]
            self._anomaly("expired_unsettled")
            return
        price = float(price)
        pnl = (price - lot.avg_price) * qty * OPTION_MULTIPLIER
        self.realized.append(RealizedTrade(
            contract_key=key, ticker=lot.ticker, channel=lot.channel, qty=qty,
            entry_price=lot.avg_price, exit_price=price, pnl_usd=pnl, ts=ts,
            client_id="expiry"))
        self.realized_total += pnl
        self.peak_realized = max(self.peak_realized, self.realized_total)
        lot.qty -= qty
        if lot.qty <= 0:
            self.closed_by_day.setdefault(utc_day(ts), set()).add(key)
            del self.lots[key]
        self.fills.append(ExecFill(
            client_id="expiry", contract_key=key, action="expire", qty=qty,
            price=price, ts=ts, reason="expired_at_zero" if bool(
                event.get("assumed_zero")) else "expired"))
        self._anomaly("expired_at_zero" if bool(event.get("assumed_zero"))
                      else "expired_settled")

    def _anomaly(self, name: str) -> None:
        self.anomalies[name] = self.anomalies.get(name, 0) + 1

    # ── persistence ───────────────────────────────────────────────────────────
    def _emit(self, event: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        payload = dict(event)
        payload["v"] = 1
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())


def positions_from_book(book: ExecutionBook) -> list[Position]:
    return book.open_positions()


def reconcile(book: ExecutionBook, positions: Iterable[Position]) -> dict:
    """Compare a book to an external position snapshot, by contract and quantity."""
    expected = {p.contract_key: p.contracts for p in book.open_positions()}
    actual = {p.contract_key: p.contracts for p in positions if p.contracts > 0}
    keys = sorted(set(expected) | set(actual))
    rows = []
    for key in keys:
        if expected.get(key, 0) != actual.get(key, 0):
            rows.append({"contract_key": key, "book": expected.get(key, 0),
                         "broker": actual.get(key, 0),
                         "delta": actual.get(key, 0) - expected.get(key, 0)})
    return {"matched": not rows, "differences": rows, "book_positions": len(expected),
            "broker_positions": len(actual)}
