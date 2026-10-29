"""Confirmed execution: the book the venue would recognise.

The engine's positions are a **decision cache**, not a record. It books a buy when it
*decides* to buy (conservative: a failed buy leaves a position that does not exist) and
deletes a position when it *decides* to sell (the opposite: a failed sell leaves the
venue holding risk while the engine believes it is flat). Between those two facts there
was no durable state at all, so four defects were structurally possible:

* a rejected buy stayed in the engine's book forever, holding a concurrency slot;
* a partial fill was recorded as the full clip, overstating realised P&L and the
  governor's equity path;
* a rejected sell had already moved realised P&L and the drawdown;
* a restart forgot every open position, so the bot could re-enter a contract it still
  held, or fail to price an exit for one it did.

Intents, acks and fills are already logged (`fills.py`, `ledger.py`), and the log can
name an order with no terminal. What was missing is the **state**: something that reads
those answers and keeps a book that survives a restart. This module is that state.

Design rules
------------
* **Append-only, replayable.** `live/execution.jsonl` holds one event per line:
  `baseline`, `intent`, `ack`, `fill`, `reject`, `unknown`, `expire`. `load()` replays
  it. A torn **final** line (a crash mid-append) is ignored and counted; corruption
  anywhere earlier is a refusal, never a silent reset, because a reset is the amnesia
  this file exists to prevent.
* **Cumulative acks.** An ack's `qty` is the total filled for that order, not the delta.
  A second ack of 3 after one of 2 applies 1. An identical repeat applies 0 and counts
  as a duplicate, so a redelivered broker response cannot double-fill.
* **The venue is the only source of a fill.** Nothing here predicts, retries or
  reconciles by assumption: a transport failure is `unknown` (non-terminal), and the
  order stays unresolved until a venue lookup settles it through `apply_ack`.
* **Never oversell, never invent.** A sell larger than the lot is clamped to the lot and
  counted as an anomaly; a sell with no lot is an anomaly with no P&L, not a short. A
  fill for an order this book never recorded is refused: adopting an unseen execution is
  an operator act, and inventing one would be a second truth.
* **Baseline, not amnesia.** When the book is introduced mid-history, `ensure_baseline`
  persists the risk state it inherited **once**. `sync_engine` reconstructs the engine
  as `baseline + confirmed events`, so a restart cannot erase a drawdown, and the peak
  equity path keeps the high-water mark the engine actually reached.
* **One day function.** Every day-keyed fact (closed contracts, realised-today, trades)
  is grouped by an injected `day_of(ts)`. Its default is the UTC date, which is honest
  for a book with no calendar; the live path passes the engine's **ET trading day**, so
  a 20:00 ET settle lands on the session it belongs to.

What this module deliberately does not do
-----------------------------------------
It does not place, retry, cancel or reconcile. `reconcile.py` compares books; `recover`
resolves an unresolved intent from a lookup; `stops.py` reads the book for marks. This
module answers one question only: **what does the venue's answer say we hold, and how
much did we realise, given everything it has told us so far?**

The two artifacts it produces are the seams other lanes consume:

| Artifact | Producer | Consumer |
|---|---|---|
| `live/execution.jsonl` (event log) | `_emit` | `load` / `book verify` / `book replay` |
| one `tca.Execution` per filled order | `execution_rows` | `tca fills` - real fills, not models |
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .broker import Ack
from .engine import Engine
from .types import OrderIntent, Position, Side

if TYPE_CHECKING:                       # the export view is not a runtime dependency
    from .tca import Execution

OPTION_MULTIPLIER = 100.0
SCHEMA = "mff.execution-book.v1"
SCHEMA_VERSION = 1

PENDING = "PENDING"
PARTIAL = "PARTIAL"
FILLED = "FILLED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
TERMINAL_STATES = frozenset({FILLED, REJECTED, CANCELLED})

# The entry reason carries the posted bid the price was derived from
# ("entry:FULL:his_bid=1.34:ask=1.41"). Reading it back is not a reconstruction - the
# number was recorded at decision time - and it gives TCA the reference it needs to
# measure how far through his bid we actually paid.
_HIS_BID_RE = re.compile(r"his_bid=([0-9]+(?:\.[0-9]+)?)")


class ExecutionBookError(ValueError):
    """The execution log cannot be trusted, or an event would corrupt the book."""


def utc_day(ts: float) -> str:
    """The default day key: the UTC date. Honest for a book with no calendar."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def contract_parts(contract_key: str) -> tuple[str, Side | None, str]:
    """`TICKER|CALL|720|0DTE` -> (ticker, side, expiry).

    A contract key is this stack's identity for an option, so a key that cannot name a
    contract is refused where it is used (`_fill`) rather than guessed at. The expiry
    may be `0DTE`, an ISO date, a month-day token, or `?`; `expire_carried` only acts on
    the two it can read, because "cannot tell" is not "expired".
    """
    parts = contract_key.split("|")
    ticker = parts[0] if parts else ""
    side: Side | None = None
    if len(parts) > 1:
        token = parts[1].strip().upper()
        if token == Side.CALL.value:
            side = Side.CALL
        elif token == Side.PUT.value:
            side = Side.PUT
    expiry = parts[3] if len(parts) > 3 else ""
    return ticker, side, expiry


def _finite(value: float, what: str) -> float:
    """Refuse a NaN/inf price: a number a human cannot read is not a fill price."""
    if not math.isfinite(value):
        raise ExecutionBookError(f"{what} is not a finite number: {value!r}")
    return value


@dataclass
class ExecOrder:
    """One order's life: the intent as sent, and what the venue has confirmed."""

    client_id: str
    intent: OrderIntent
    channel: str = ""
    created_ts: float = 0.0
    state: str = PENDING
    filled_qty: int = 0
    avg_price: float = 0.0
    broker_order_id: str = ""
    error: str = ""
    first_fill_ts: float | None = None
    source_id: str = ""
    last_unknown: str = ""          # the transport failure that left this unresolved

    @property
    def action(self) -> str:
        return self.intent.action

    @property
    def contract_key(self) -> str:
        return self.intent.contract_key

    @property
    def remaining(self) -> int:
        return max(0, self.intent.qty - self.filled_qty)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def partial(self) -> bool:
        return 0 < self.filled_qty < self.intent.qty

    @property
    def unknown(self) -> bool:
        """Sent, no terminal, and the venue may or may not have it."""
        return bool(self.last_unknown) and not self.terminal

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "action": self.action,
            "contract_key": self.contract_key,
            "channel": self.channel,
            "intent_qty": self.intent.qty,
            "limit": self.intent.limit,
            "reason": self.intent.reason,
            "idempotency_key": self.intent.idempotency_key,
            "state": self.state,
            "filled_qty": self.filled_qty,
            "remaining": self.remaining,
            "avg_price": round(self.avg_price, 4),
            "broker_order_id": self.broker_order_id,
            "created_ts": self.created_ts,
            "first_fill_ts": self.first_fill_ts,
            "error": self.error,
            "unknown": self.last_unknown,
            "source_id": self.source_id,
        }


@dataclass
class ExecFill:
    """One confirmed execution, as it was applied to the book."""

    client_id: str
    contract_key: str
    action: str
    qty: int                    # the delta applied by THIS event
    cumulative: int             # filled_qty after this event
    price: float
    ts: float
    broker_order_id: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "contract_key": self.contract_key,
            "action": self.action,
            "qty": self.qty,
            "cumulative": self.cumulative,
            "price": round(self.price, 4),
            "ts": self.ts,
            "broker_order_id": self.broker_order_id,
            "reason": self.reason,
        }


@dataclass
class ExecLot:
    """A holding, built only from confirmed buys and reduced only by confirmed sells."""

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
        return Position(
            contract_key=self.contract_key, ticker=self.ticker, side=self.side,
            contracts=self.qty, avg_price=self.avg_price, opened_ts=self.opened_ts,
            channel=self.channel, adds=self.adds, trimmed=self.trimmed,
            breakeven_stop=self.breakeven_stop,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_key": self.contract_key,
            "ticker": self.ticker,
            "side": self.side.value,
            "qty": self.qty,
            "avg_price": round(self.avg_price, 4),
            "opened_ts": self.opened_ts,
            "channel": self.channel,
            "adds": self.adds,
            "trimmed": self.trimmed,
            "breakeven_stop": self.breakeven_stop,
        }


@dataclass
class RealizedTrade:
    """A closed quantity and the money it made or lost. One per confirmed sell."""

    contract_key: str
    ticker: str
    channel: str
    qty: int
    entry_price: float
    exit_price: float
    pnl_usd: float
    ts: float
    client_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_key": self.contract_key,
            "ticker": self.ticker,
            "channel": self.channel,
            "qty": self.qty,
            "entry_price": round(self.entry_price, 4),
            "exit_price": round(self.exit_price, 4),
            "pnl_usd": self.pnl_usd,
            "ts": self.ts,
            "client_id": self.client_id,
        }


@dataclass
class Baseline:
    """The risk state the book inherited, persisted once, never overwritten.

    Without it the first `sync_engine` would reset a drawdown to zero: the book knows
    only the trades it saw, and the engine's worst day is usually the day before the
    book existed. `peak_equity_usd` is the high-water mark at that moment, so the
    drawdown after a restart is measured against history rather than against the
    book's own short life.
    """

    equity_usd: float = 0.0
    peak_equity_usd: float = 0.0
    day: str = ""
    realized_today_usd: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "equity_usd": round(self.equity_usd, 2),
            "peak_equity_usd": round(self.peak_equity_usd, 2),
            "day": self.day,
            "realized_today_usd": round(self.realized_today_usd, 2),
            "trades_today": self.trades_today,
            "consecutive_losses": self.consecutive_losses,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Baseline:
        return cls(
            equity_usd=float(payload.get("equity_usd") or 0.0),
            peak_equity_usd=float(payload.get("peak_equity_usd") or 0.0),
            day=str(payload.get("day") or ""),
            realized_today_usd=float(payload.get("realized_today_usd") or 0.0),
            trades_today=int(payload.get("trades_today") or 0),
            consecutive_losses=int(payload.get("consecutive_losses") or 0),
        )


@dataclass(frozen=True)
class SyncReport:
    """What `sync_engine` restored, so the caller can print or assert on it."""

    day: str
    expired: int
    positions: int
    contracts: int
    realized_usd: float
    entries_today: int
    trades_today: int
    unresolved: int
    in_flight_sells: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "expired": self.expired,
            "positions": self.positions,
            "contracts": self.contracts,
            "realized_usd": round(self.realized_usd, 2),
            "entries_today": self.entries_today,
            "trades_today": self.trades_today,
            "unresolved": self.unresolved,
            "in_flight_sells": self.in_flight_sells,
        }


@dataclass
class ExecutionBook:
    """Append-only confirmed state. Construct with `path` to persist it."""

    path: Path | None = None
    fsync: bool = False
    day_of: Callable[[float], str] = utc_day
    orders: dict[str, ExecOrder] = field(default_factory=dict)
    lots: dict[str, ExecLot] = field(default_factory=dict)
    fills: list[ExecFill] = field(default_factory=list)
    realized: list[RealizedTrade] = field(default_factory=list)
    realized_total: float = 0.0
    peak_realized: float = 0.0
    closed_by_day: dict[str, set[str]] = field(default_factory=dict)
    baseline: Baseline | None = None
    anomalies: dict[str, int] = field(default_factory=dict)
    duplicates: int = 0
    torn_lines: int = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, path: str | Path, *, day_of: Callable[[float], str] = utc_day,
             fsync: bool = False) -> ExecutionBook:
        """Replay the log into a book. A missing file is an empty book, not an error.

        A crash can tear the *last* append, so a final unreadable line is counted and
        dropped. Corruption earlier than that is refused: accepting it would silently
        discard confirmed fills in the middle of the history, which is a worse lie than
        refusing to start.
        """
        target = Path(path)
        book = cls(path=target, fsync=fsync, day_of=day_of)
        if not target.exists():
            return book
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ExecutionBookError(f"cannot read execution log {target}: {exc}") from None
        rows = [(number, line.strip())
                for number, line in enumerate(text.splitlines(), start=1) if line.strip()]
        for index, (number, line) in enumerate(rows):
            try:
                event: object = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(rows) - 1:
                    book.torn_lines += 1        # a crash mid-append; named, not hidden
                    break
                raise ExecutionBookError(
                    f"execution log {target}:{number} is corrupt: {exc}") from None
            if not isinstance(event, Mapping):
                raise ExecutionBookError(f"execution log {target}:{number} is not an object")
            version = event.get("v")
            if version != SCHEMA_VERSION:
                raise ExecutionBookError(
                    f"execution log {target}:{number} has schema version {version!r}; "
                    f"this build reads v{SCHEMA_VERSION}")
            book._apply(event)
        return book

    def ensure_baseline(self, *, equity_usd: float, peak_equity_usd: float, day: str,
                        realized_today_usd: float, trades_today: int,
                        consecutive_losses: int, ts: float | None = None) -> Baseline:
        """Persist the inherited risk state once, then never touch it again.

        Called by `arm` with whatever `live/governor.json` held at the moment the book
        was introduced. Re-running it is a no-op, so a crash loop cannot keep resetting
        the baseline to "everything is fine".
        """
        if self.baseline is not None:
            return self.baseline
        event: dict[str, Any] = {
            "v": SCHEMA_VERSION, "t": "baseline",
            "equity": round(float(equity_usd), 2),
            "peak": round(float(peak_equity_usd), 2),
            "day": day,
            "realized_today": round(float(realized_today_usd), 2),
            "trades_today": int(trades_today),
            "losses": int(consecutive_losses),
            "ts": time.time() if ts is None else ts,
        }
        self._apply(event)
        self._emit(event)
        assert self.baseline is not None           # _apply sets it, or raises
        return self.baseline

    # ── recording (each of these appends one event) ────────────────────────────
    @staticmethod
    def client_id(intent: OrderIntent, ts: float, day_of: Callable[[float], str]) -> str:
        """`<idempotency key>@<day>`: the broker's key plus the session it belonged to.

        A 0DTE contract key recurs every day it trades and the engine's own key counter
        resets at the roll, so the day is part of the identity. Two sends of the same
        key in different sessions are two orders, not a duplicate.

        The key itself is required. Without one, two genuine orders of the same shape
        (buy 2 QQQ at 1.05, twice) would collapse into a single client id and the book
        would silently merge them - the exact class of mistake a durable record exists
        to prevent. The engine always sets one; refusing here means the caller finds out
        at intent time, not at exit time.
        """
        if not intent.idempotency_key:
            raise ExecutionBookError(
                f"{intent.contract_key}: an intent without an idempotency key cannot be "
                f"addressed (two identical orders would share one client id)")
        return f"{intent.idempotency_key}@{day_of(ts)}"

    def record_intent(self, intent: OrderIntent, *, ts: float, channel: str = "",
                      source_id: str = "") -> ExecOrder:
        """Write the intent before the send. Idempotent on the client id.

        This is the row that makes a crash mid-send survivable: the order exists, has no
        terminal, and `unresolved_orders()` names it until the venue answers. A second
        call with the same id and the same shape returns the existing order (nothing is
        emitted); the same id with a *different* order is a bug and is refused rather
        than silently re-used - that is how a duplicate client id would become a
        duplicate fill.
        """
        if intent.action not in ("buy", "sell"):
            raise ExecutionBookError(f"{intent.contract_key}: unknown action {intent.action!r}")
        if intent.qty <= 0:
            raise ExecutionBookError(
                f"{intent.contract_key}: an intent must ask for at least one contract")
        _finite(float(intent.limit), f"{intent.contract_key}: limit")
        if intent.limit < 0:
            raise ExecutionBookError(f"{intent.contract_key}: negative limit {intent.limit}")
        _, side, _ = contract_parts(intent.contract_key)
        if not intent.contract_key or side is None:
            # A key that cannot name a side cannot become a lot, and refusing here means
            # the refusal happens before a send rather than after a fill.
            raise ExecutionBookError(
                f"{intent.contract_key!r} is not a contract key (TICKER|SIDE|STRIKE|EXP)")
        cid = self.client_id(intent, ts, self.day_of)
        existing = self.orders.get(cid)
        if existing is not None:
            same = (existing.contract_key == intent.contract_key
                    and existing.action == intent.action
                    and existing.intent.qty == intent.qty
                    and abs(existing.intent.limit - intent.limit) < 1e-9)
            if not same:
                raise ExecutionBookError(
                    f"{cid}: duplicate client id for a different order "
                    f"({existing.action} {existing.intent.qty} "
                    f"{existing.contract_key} @ {existing.intent.limit:g} vs "
                    f"{intent.action} {intent.qty} {intent.contract_key} "
                    f"@ {intent.limit:g})")
            self.duplicates += 1
            return existing
        event = {
            "v": SCHEMA_VERSION, "t": "intent", "cid": cid, "ts": ts,
            "action": intent.action, "contract_key": intent.contract_key,
            "qty": intent.qty, "limit": float(intent.limit), "channel": channel,
            "reason": intent.reason, "source_id": source_id,
            "idem": intent.idempotency_key,
        }
        self._apply(event)
        self._emit(event)
        return self.orders[cid]

    def apply_ack(self, client_id: str, ack: Ack, *, ts: float) -> ExecOrder:
        """Apply a broker answer. `ack.qty` is CUMULATIVE, which is what makes it safe.

        The delta is `ack.qty - filled_qty`, so a redelivered ack of 3 after a fill of 3
        applies nothing. A regression (a smaller cumulative than already applied) is
        refused outright: it can only mean the order id was reused or the log is
        corrupt, and clamping it would hide both. A business reject is a terminal, and
        the venue answering at all is transport working.
        """
        order = self._order(client_id)
        if not ack.ok:
            return self.reject(client_id, reason=ack.error or "broker_rejected", ts=ts)
        if ack.contract_key and ack.contract_key != order.contract_key:
            raise ExecutionBookError(
                f"{client_id}: ack contract {ack.contract_key!r} != intent "
                f"{order.contract_key!r}")
        cumulative = int(ack.qty)
        if cumulative < 0:
            raise ExecutionBookError(f"{client_id}: negative ack qty {cumulative}")
        if cumulative > order.intent.qty:
            self._anomaly("ack_qty_exceeds_intent")
            cumulative = order.intent.qty
        if cumulative < order.filled_qty:
            raise ExecutionBookError(
                f"{client_id}: ack regressed from {order.filled_qty} to {cumulative}")
        if cumulative == order.filled_qty:
            # The same answer twice (a redelivery, a polled duplicate). Not an error,
            # not a second fill - and worth counting, because a venue that keeps
            # repeating itself is a signal the transport is replaying.
            self.duplicates += 1
            if ack.broker_order_id:
                order.broker_order_id = ack.broker_order_id
            return order
        price = float(ack.price)
        if not math.isfinite(price) or price <= 0:
            # A fill with no usable price is a missing field, not a free contract.
            # The limit is the honest substitute and the substitution is counted.
            self._anomaly("ack_price_missing")
            price = float(order.intent.limit)
        event = {
            "v": SCHEMA_VERSION, "t": "ack", "cid": client_id, "ts": ts,
            "qty": cumulative, "price": price, "broker_id": ack.broker_order_id,
        }
        self._apply(event)
        self._emit(event)
        return self.orders[client_id]

    def record_fill(self, client_id: str, *, qty: int, price: float, ts: float,
                    broker_order_id: str = "") -> ExecOrder:
        """Apply an incremental late fill (a polled partial). `qty` is NEW, not cumulative.

        Some venues report executions rather than order state; this is the path for
        those. It is clamped to the order's remaining quantity and the clamp is counted,
        so an execution feed that overshoots cannot manufacture contracts.
        """
        _finite(float(price), f"{client_id}: fill price")
        if qty <= 0:
            raise ExecutionBookError(f"{client_id}: a fill must be at least one contract")
        if price < 0:
            raise ExecutionBookError(f"{client_id}: negative fill price {price}")
        order = self._order(client_id)
        if order.terminal:
            raise ExecutionBookError(
                f"{client_id}: fill after terminal state {order.state}")
        event = {
            "v": SCHEMA_VERSION, "t": "fill", "cid": client_id, "ts": ts,
            "qty": int(qty), "price": float(price), "broker_id": broker_order_id,
        }
        self._apply(event)
        self._emit(event)
        return self.orders[client_id]

    def mark_unknown(self, client_id: str, *, reason: str, ts: float) -> ExecOrder:
        """A transport failure: the order stays OPEN and the ambiguity is named.

        This is deliberately not a terminal. The send may have reached the venue after
        the socket died, so writing a reject would be a lie in the one direction that
        costs money - and it would hide the order from `recover`, the only tool that can
        settle it. Nothing is retried here; nothing is sent.
        """
        order = self._order(client_id)
        if order.terminal:
            raise ExecutionBookError(
                f"{client_id}: transport unknown after terminal state {order.state}")
        event = {
            "v": SCHEMA_VERSION, "t": "unknown", "cid": client_id, "ts": ts,
            "reason": str(reason)[:200],
        }
        self._apply(event)
        self._emit(event)
        return self.orders[client_id]

    def reject(self, client_id: str, *, reason: str, ts: float) -> ExecOrder:
        """A terminal 'the venue did not do this' answer.

        A reject that arrives after a fill cannot undo the fill: the order becomes
        CANCELLED (it is done, and it kept what it got) rather than REJECTED, because
        the book's purpose is to state what is held.
        """
        self._order(client_id)
        event = {
            "v": SCHEMA_VERSION, "t": "reject", "cid": client_id, "ts": ts,
            "reason": str(reason)[:200],
        }
        self._apply(event)
        self._emit(event)
        return self.orders[client_id]

    # ── expiry: release a slot the market already took ────────────────────────
    def expire_carried(self, day: str, ts: float, *, assume_zero: bool = True) -> int:
        """Close lots whose contract can no longer be traded. Returns how many.

        A restart can land after expiry, and an open lot on a dead contract holds a
        concurrency slot and makes the pre-trade budget count premium that no longer
        exists. There is no settlement feed in this stack, so:

        * `assume_zero=True` (default) marks a long at zero - the maximum loss - which
          is conservative for risk and visibly flagged (`assumed_zero`), not a guess at
          an in-the-money settlement;
        * `assume_zero=False` releases the slot **without** manufacturing P&L and counts
          `expired_unsettled`, which is the operator's cue to reconcile.

        `0DTE` expires when the lot was opened on an earlier day. An ISO expiry expires
        on/after the day after it. Anything else (`?`, `Dec 5`) is **not** expired: an
        unreadable expiry is not evidence that the contract is dead, and guessing would
        delete real risk from the book.
        """
        expired: list[str] = []
        for key, lot in list(self.lots.items()):
            _, _, expiry = contract_parts(key)
            token = expiry.strip().upper()
            if not token:
                continue
            if token == "0DTE":
                stale = self.day_of(lot.opened_ts) < day
            else:
                try:
                    stale = time.strftime("%Y-%m-%d", time.strptime(token, "%Y-%m-%d")) < day
                except (TypeError, ValueError):
                    self._anomaly("expiry_unreadable")
                    continue
            if stale:
                expired.append(key)
        for key in expired:
            lot = self.lots[key]
            event: dict[str, Any] = {
                "v": SCHEMA_VERSION, "t": "expire", "key": key, "qty": lot.qty, "ts": ts,
                "price": 0.0 if assume_zero else None, "assumed_zero": assume_zero,
            }
            self._apply(event)
            self._emit(event)
        return len(expired)

    # ── derived state ─────────────────────────────────────────────────────────
    def open_positions(self) -> list[Position]:
        """The confirmed book, in a shape the engine and the guards already read."""
        return [lot.to_position() for lot in self.lots.values() if lot.qty > 0]

    def unresolved_orders(self) -> list[ExecOrder]:
        """Sent, no terminal. The orders a restart must settle before it trades again."""
        return [order for order in self.orders.values() if not order.terminal]

    def unknown_orders(self) -> list[ExecOrder]:
        """The subset of unresolved orders the venue may or may not have."""
        return [order for order in self.unresolved_orders() if order.unknown]

    def closed_keys(self, day: str) -> set[str]:
        return set(self.closed_by_day.get(day, set()))

    def realized_on(self, day: str) -> float:
        return round(sum(trade.pnl_usd for trade in self.realized
                         if self.day_of(trade.ts) == day), 2)

    def trades_on(self, day: str) -> int:
        return sum(1 for trade in self.realized if self.day_of(trade.ts) == day)

    def entries_on(self, day: str) -> int:
        """Buys that actually filled today, excluding adds. The pipe-test counter."""
        return sum(
            1 for order in self.orders.values()
            if order.action == "buy" and order.filled_qty > 0
            and order.first_fill_ts is not None
            and self.day_of(order.first_fill_ts) == day
            and not order.intent.reason.startswith("add:")
        )

    def loss_streak(self, day: str) -> int:
        """Consecutive losses, ending with today's last confirmed close."""
        streak = 0
        for trade in self.realized:
            if self.day_of(trade.ts) != day:
                continue
            streak = streak + 1 if trade.pnl_usd < 0 else 0
        return streak

    def snapshot(self) -> dict[str, Any]:
        """Canonical state, order-independent, for equality and for `book show`."""
        return {
            "schema": SCHEMA,
            "orders": len(self.orders),
            "pending": sum(1 for o in self.orders.values() if not o.terminal),
            "positions": {key: lot.qty for key, lot in sorted(self.lots.items())},
            "contracts": sum(lot.qty for lot in self.lots.values()),
            "realized_total_usd": round(self.realized_total, 2),
            "peak_realized_usd": round(self.peak_realized, 2),
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "anomalies": dict(sorted(self.anomalies.items())),
            "duplicates": self.duplicates,
            "torn_lines": self.torn_lines,
        }

    def digest(self) -> str:
        """SHA-256 of the canonical snapshot: the restart-replay fingerprint.

        Two replays of the same log must produce the same digest. Printing it on arm
        and on every `book show` makes "this process rebuilt the book I left" a fact
        instead of an impression.
        """
        payload = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def report_text(self) -> str:
        snap = self.snapshot()
        lines = [
            f"execution book | {snap['orders']} order(s) | {snap['pending']} unresolved "
            f"| {len(snap['positions'])} position(s) / {snap['contracts']} contract(s) "
            f"| realised ${snap['realized_total_usd']:,.2f}",
            f"  digest {self.digest()[:16]}",
        ]
        if self.baseline is not None:
            base = self.baseline.to_dict()
            lines.append(f"  baseline equity ${base['equity_usd']:,.2f} | peak "
                         f"${base['peak_equity_usd']:,.2f} | day {base['day']}")
        if self.torn_lines:
            lines.append(f"  one torn final append was ignored ({self.torn_lines})")
        if self.duplicates:
            lines.append(f"  duplicate answers ignored: {self.duplicates}")
        for order in self.orders.values():
            marker = "!"
            lines.append(f"  {order.state:<9}{marker if order.unknown else ' '}"
                         f"{order.action:<4} {order.contract_key:<28} "
                         f"{order.filled_qty}/{order.intent.qty} @ "
                         f"{order.avg_price or order.intent.limit:g}"
                         f"{'  ' + order.last_unknown if order.unknown else ''}")
        for lot in self.lots.values():
            lines.append(f"  LOT      {lot.contract_key:<28} {lot.qty:>3} @ "
                         f"{lot.avg_price:g}  {lot.channel}")
        if self.anomalies:
            lines.append(f"  anomalies: {dict(sorted(self.anomalies.items()))}")
        return "\n".join(lines)

    # ── the restart: rebuild the engine from confirmed state ───────────────────
    def sync_engine(self, engine: Engine, now: float) -> SyncReport:
        """Replace the engine's optimistic book with confirmed execution state.

        Called once at arm time, before the first message is handled. It restores:

        * positions - the confirmed lots, not the engine's decisions;
        * closed contracts for the trading day - so "never chase a closed contract"
          survives a restart;
        * realised P&L and the governor's equity path - `baseline + confirmed events`,
          with the peak kept at the high-water mark, so a drawdown is not erased;
        * the day - the engine's ET label, so the first roll lands where it should.

        What it deliberately does **not** restore is `engine.halted`. A stand-down is a
        session decision with its own persistence (`GovernorState.halted_reason`, which
        `arm` refuses to start on); inventing a halt from an execution event would make
        a normal rejected order look like a stopped desk. The two authorities stay
        separate: the book owns *what is held*, the policy owns *whether to trade*.

        A lot carried past expiry is closed first (see `expire_carried`), because
        holding a dead contract's slot through a restart is the "carried position holds
        a concurrency slot forever" defect with a restart in front of it.

        The **in-flight sells** are restored too: a restart with an unresolved sell must
        not send a second one for the same lot. The guard belongs to the book because the
        book is the only thing that knows the order outlived the process.
        """
        day = engine.trading_day(now)
        expired = self.expire_carried(day, now)
        base = self.baseline or Baseline()
        same_day = base.day == day

        positions = self.open_positions()
        engine.positions = {position.contract_key: position for position in positions}
        engine.closed = set(self.closed_keys(day))
        # A restart must not forget that an exit is already at the venue. Without this
        # the first close post after arming would send a second sell for a lot whose
        # first sell is still unanswered (or unknown), which is a short.
        engine.exit_in_flight = {
            order.contract_key for order in self.unresolved_orders()
            if order.action == "sell"}
        engine.day = day

        realized_ever = round(base.equity_usd + self.realized_total, 2)
        entries_today = self.entries_on(day)
        trades_today = self.trades_on(day)
        engine.stats.realized_usd = realized_ever
        # Two different counters share the name `trades_today` in this stack: the
        # engine uses it for "entries filled today" (the pipe test and the idempotency
        # key), the governor for "results settled today" (the loss streak). Both are
        # restored from confirmed events, which is the point of the book.
        engine.stats.trades_today = (base.trades_today if same_day else 0) + entries_today

        state = engine.governor_state
        state.day = day
        state.realized_today_usd = round(
            (base.realized_today_usd if same_day else 0.0) + self.realized_on(day), 2)
        state.trades_today = (base.trades_today if same_day else 0) + trades_today
        state.equity_usd = realized_ever
        state.peak_equity_usd = max(base.peak_equity_usd,
                                    round(base.equity_usd + max(0.0, self.peak_realized), 2),
                                    realized_ever)
        streak = self.loss_streak(day)
        if trades_today == 0:
            state.consecutive_losses = base.consecutive_losses if same_day else 0
        elif streak == trades_today and same_day:
            # Every confirmed close today was a loss, so the inherited streak continues.
            state.consecutive_losses = base.consecutive_losses + streak
        else:
            state.consecutive_losses = streak

        report = SyncReport(
            day=day, expired=expired, positions=len(positions),
            contracts=sum(position.contracts for position in positions),
            realized_usd=realized_ever, entries_today=entries_today,
            trades_today=trades_today, unresolved=len(self.unresolved_orders()),
            in_flight_sells=len(engine.exit_in_flight),
        )
        engine.fills.record(
            event="sync", contract_key="", qty=report.contracts, price=0.0,
            reason=f"confirmed:positions={report.positions}:expired={expired}",
            ts=now, note=f"execution-book:{self.digest()[:16]}",
        )
        return report

    # ── verification: the log's own honesty ───────────────────────────────────
    def problems(self) -> list[str]:
        """Structural defects: states that cannot be reached by a correct event stream.

        `unresolved_orders` is **not** listed here - an in-flight order is normal, and
        `book verify --strict` is where an operator asks for that to be a failure. These
        are the defects that mean the log lies: a quantity outside its own intent, a
        terminal that contradicts its fills, a realised total that does not equal its
        trades, a lot that is already closed.
        """
        problems: list[str] = []
        total = round(sum(trade.pnl_usd for trade in self.realized), 2)
        if abs(total - self.realized_total) > 1e-6:
            problems.append(
                f"realised total {self.realized_total:,.2f} != sum of "
                f"{len(self.realized)} trades {total:,.2f}")
        for order in self.orders.values():
            if order.state not in (PENDING, PARTIAL, FILLED, REJECTED, CANCELLED):
                problems.append(f"{order.client_id}: unknown state {order.state!r}")
            if order.filled_qty < 0 or order.filled_qty > order.intent.qty:
                problems.append(
                    f"{order.client_id}: filled {order.filled_qty} of {order.intent.qty}")
            if order.state == FILLED and order.filled_qty != order.intent.qty:
                problems.append(
                    f"{order.client_id}: FILLED with {order.filled_qty}/{order.intent.qty}")
            if order.state == REJECTED and order.filled_qty:
                problems.append(
                    f"{order.client_id}: REJECTED after filling {order.filled_qty}")
            if order.filled_qty and order.avg_price <= 0:
                problems.append(f"{order.client_id}: filled at a non-positive price")
        for key, lot in self.lots.items():
            if not key:
                problems.append("a lot without a contract key")
            if lot.qty <= 0:
                problems.append(f"{key}: closed lot still on the book ({lot.qty})")
            if not math.isfinite(lot.avg_price) or lot.avg_price < 0:
                problems.append(f"{key}: unreadable average price {lot.avg_price!r}")
        return problems

    # ── the TCA seam: one venue fill per terminal order ───────────────────────
    def execution_rows(self) -> list[Execution]:
        """Terminal orders as `tca.Execution` rows: what `tca fills` measures.

        One row per order that resolved with a fill, `fill_qty` cumulative, `qty` the
        intent. That makes `fill_ratio < 1` mean exactly "this order was partially
        filled", which is the question TCA asks. `reference` is his posted bid, read
        back from the recorded entry reason - the number was captured at decision time,
        so `through_bid` measures what we paid against what he said, not a model.
        """
        from .tca import Execution  # the export view, imported on use

        rows: list[Execution] = []
        for order in self.orders.values():
            if order.filled_qty <= 0 or order.first_fill_ts is None:
                continue
            reference: float | None = None
            match = _HIS_BID_RE.search(order.intent.reason)
            if match is not None:
                reference = float(match.group(1))
            rows.append(Execution(
                contract_key=order.contract_key,
                action=order.action,
                qty=order.intent.qty,
                limit=order.intent.limit,
                fill_price=order.avg_price,
                fill_qty=order.filled_qty,
                intent_ts=order.created_ts,
                fill_ts=order.first_fill_ts,
                channel=order.channel,
                reference=reference,
            ))
        return rows

    # ── event application (shared by the live methods and replay) ──────────────
    def _order(self, client_id: str) -> ExecOrder:
        order = self.orders.get(client_id)
        if order is None:
            raise ExecutionBookError(
                f"unknown order {client_id!r}: a fill without an intent cannot be "
                f"applied (adopt it through `reconcile`, never here)")
        return order

    def _apply(self, event: Mapping[str, Any]) -> None:
        """Fold one event into the book. The only place state changes.

        Live methods validate the *input* and then call this; `load()` calls this with
        the parsed row. One implementation means a replay cannot diverge from the live
        path, and every guard below therefore has to hold for both.
        """
        kind = event.get("t")
        if kind == "baseline":
            if self.baseline is None:
                self.baseline = Baseline(
                    equity_usd=float(event.get("equity") or 0.0),
                    peak_equity_usd=float(event.get("peak") or 0.0),
                    day=str(event.get("day") or ""),
                    realized_today_usd=float(event.get("realized_today") or 0.0),
                    trades_today=int(event.get("trades_today") or 0),
                    consecutive_losses=int(event.get("losses") or 0),
                )
            return
        if kind == "intent":
            self._apply_intent(event)
            return
        if kind == "ack":
            self._apply_ack(event)
            return
        if kind == "fill":
            self._apply_fill_event(event)
            return
        if kind == "reject":
            self._apply_reject(event)
            return
        if kind == "unknown":
            self._apply_unknown(event)
            return
        if kind == "expire":
            self._apply_expire(event)
            return
        raise ExecutionBookError(f"unknown execution event {kind!r}")

    def _apply_intent(self, event: Mapping[str, Any]) -> None:
        cid = str(event.get("cid") or "")
        if not cid:
            raise ExecutionBookError("intent without a client id")
        action = str(event.get("action") or "")
        if action not in ("buy", "sell"):
            raise ExecutionBookError(f"{cid}: unknown action {action!r}")
        qty = int(event.get("qty") or 0)
        if qty <= 0:
            raise ExecutionBookError(f"{cid}: intent qty must be positive, got {qty}")
        limit = _finite(float(event.get("limit") or 0.0), f"{cid}: limit")
        if self.orders.get(cid) is not None:
            return                       # replay of a duplicate row: first one wins
        self.orders[cid] = ExecOrder(
            client_id=cid,
            intent=OrderIntent(
                action=action, qty=qty, limit=limit,
                contract_key=str(event.get("contract_key") or ""),
                reason=str(event.get("reason") or ""),
                idempotency_key=str(event.get("idem") or cid),
            ),
            channel=str(event.get("channel") or ""),
            created_ts=float(event.get("ts") or 0.0),
            source_id=str(event.get("source_id") or ""),
        )

    def _apply_ack(self, event: Mapping[str, Any]) -> None:
        order = self._order(str(event.get("cid") or ""))
        if not bool(event.get("ok", True)):
            self._reject(order, str(event.get("reason") or "broker_rejected"))
            return
        cumulative = max(0, int(event.get("qty") or 0))
        if cumulative > order.intent.qty:
            self._anomaly("ack_qty_exceeds_intent")
            cumulative = order.intent.qty
        if cumulative < order.filled_qty:
            raise ExecutionBookError(
                f"{order.client_id}: ack regressed from {order.filled_qty} to {cumulative}")
        price = _finite(float(event.get("price") or 0.0), f"{order.client_id}: ack price")
        if cumulative > order.filled_qty:
            self._fill(order, cumulative - order.filled_qty, price,
                       float(event.get("ts") or 0.0), str(event.get("broker_id") or ""))
            order.filled_qty = cumulative
        elif price > 0:
            # A zero-delta ack still carries the venue's average price; keep the latest
            # readable one so a later partial uses the right basis.
            order.avg_price = price
        order.broker_order_id = str(event.get("broker_id") or order.broker_order_id)
        order.state = (FILLED if order.filled_qty >= order.intent.qty else PARTIAL)
        order.last_unknown = ""            # the venue answered; the ambiguity is gone

    def _apply_fill_event(self, event: Mapping[str, Any]) -> None:
        order = self._order(str(event.get("cid") or ""))
        delta = max(0, int(event.get("qty") or 0))
        room = max(0, order.intent.qty - order.filled_qty)
        if delta > room:
            self._anomaly("fill_exceeds_intent")
            delta = room
        price = _finite(float(event.get("price") or 0.0), f"{order.client_id}: fill price")
        if delta:
            self._fill(order, delta, price, float(event.get("ts") or 0.0),
                       str(event.get("broker_id") or ""))
            order.filled_qty += delta
            order.state = (FILLED if order.filled_qty >= order.intent.qty else PARTIAL)
            order.last_unknown = ""
            if event.get("broker_id"):
                order.broker_order_id = str(event.get("broker_id"))

    def _apply_reject(self, event: Mapping[str, Any]) -> None:
        order = self._order(str(event.get("cid") or ""))
        self._reject(order, str(event.get("reason") or "broker_rejected"))

    def _apply_unknown(self, event: Mapping[str, Any]) -> None:
        order = self._order(str(event.get("cid") or ""))
        if order.terminal:
            raise ExecutionBookError(
                f"{order.client_id}: transport unknown after terminal state {order.state}")
        order.last_unknown = str(event.get("reason") or "transport_unknown")

    def _apply_expire(self, event: Mapping[str, Any]) -> None:
        key = str(event.get("key") or "")
        lot = self.lots.get(key)
        if lot is None:
            self._anomaly("expire_unknown_lot")
            return
        ts = float(event.get("ts") or 0.0)
        qty = min(int(event.get("qty") or lot.qty), lot.qty)
        raw_price = event.get("price")
        if raw_price is None:
            # No settlement feed and no assumption: release the slot, do not
            # manufacture P&L. The anomaly is the operator's signal to reconcile.
            self._settle_lot(lot, qty, None, ts, "expiry")
            self._anomaly("expired_unsettled")
            return
        price = _finite(float(raw_price), f"{key}: expiry price")
        assumed_zero = bool(event.get("assumed_zero"))
        self._settle_lot(lot, qty, price, ts, "expiry")
        self._anomaly("expired_at_zero" if assumed_zero else "expired_settled")

    def _reject(self, order: ExecOrder, reason: str) -> None:
        """Terminal refusal. A fill outranks a late reject: the book states holdings."""
        if order.state == FILLED:
            return
        if order.terminal:
            return                         # one terminal per order; replay keeps the first
        order.error = reason
        order.state = CANCELLED if order.filled_qty > 0 else REJECTED

    def _fill(self, order: ExecOrder, qty: int, price: float, ts: float,
              broker_order_id: str) -> None:
        """Apply a confirmed delta to lots, realised P&L and the fill list.

        Called by the ack path and the fill path, so cumulative acks and incremental
        executions converge on one set of rules. The lot's average price is the
        weighted average of confirmed buys; an add raises it, which is what the marks
        and the breakeven stop must see.
        """
        if qty <= 0:
            return
        price = _finite(price, f"{order.client_id}: fill price")
        if price < 0:
            raise ExecutionBookError(f"{order.client_id}: negative fill price {price}")
        if order.first_fill_ts is None:
            order.first_fill_ts = ts
        key = order.contract_key
        ticker, side, _ = contract_parts(key)
        if not key or side is None:
            raise ExecutionBookError(
                f"{order.client_id}: {key!r} is not a contract key (TICKER|SIDE|STRIKE|EXP)")

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
                if not lot.channel:
                    lot.channel = order.channel
            if order.intent.reason.startswith("add:"):
                lot.adds += 1
        elif order.action == "sell":
            lot = self.lots.get(key)
            if lot is None:
                # A sell with no lot is not a short. The live lane never opens one, and
                # inventing one here would put risk on the book that no intent asked for.
                self._anomaly("sell_without_lot")
                return
            executed = min(qty, lot.qty)
            if executed < qty:
                self._anomaly("oversell_clamped")
            if executed <= 0:
                return
            recorded = executed
            pnl = round((price - lot.avg_price) * executed * OPTION_MULTIPLIER, 2)
            self.realized.append(RealizedTrade(
                contract_key=key, ticker=lot.ticker, channel=lot.channel, qty=executed,
                entry_price=lot.avg_price, exit_price=price, pnl_usd=pnl, ts=ts,
                client_id=order.client_id))
            self.realized_total = round(self.realized_total + pnl, 2)
            self.peak_realized = max(self.peak_realized, self.realized_total)
            lot.qty -= executed
            if order.intent.reason.startswith("trim"):
                # A trim is what arms the breakeven stop: the first partial takes the
                # risk out, and `stops.py` enforces the level this records.
                lot.trimmed += 1
                lot.breakeven_stop = lot.avg_price
            if lot.qty <= 0:
                self.closed_by_day.setdefault(self.day_of(ts), set()).add(key)
                del self.lots[key]
        else:                                   # pragma: no cover - _apply_intent guards
            raise ExecutionBookError(f"{order.client_id}: unknown action {order.action!r}")

        # The order's average price is the weighted average of what it actually got.
        applied = order.filled_qty
        if applied + qty > 0:
            total_qty = applied + qty
            order.avg_price = round(
                (order.avg_price * applied + price * qty) / total_qty, 6)
        self.fills.append(ExecFill(
            client_id=order.client_id, contract_key=key, action=order.action,
            qty=recorded, cumulative=applied + qty, price=price, ts=ts,
            broker_order_id=broker_order_id, reason=order.intent.reason))

    def _settle_lot(self, lot: ExecLot, qty: int, price: float | None, ts: float,
                    client_id: str) -> None:
        """Reduce or retire a lot outside an order (an expiry), realising at `price`."""
        if qty <= 0:
            return
        if price is not None and qty:
            pnl = round((price - lot.avg_price) * qty * OPTION_MULTIPLIER, 2)
            self.realized.append(RealizedTrade(
                contract_key=lot.contract_key, ticker=lot.ticker, channel=lot.channel,
                qty=qty, entry_price=lot.avg_price, exit_price=price, pnl_usd=pnl,
                ts=ts, client_id=client_id))
            self.realized_total = round(self.realized_total + pnl, 2)
            self.peak_realized = max(self.peak_realized, self.realized_total)
            self.fills.append(ExecFill(
                client_id=client_id, contract_key=lot.contract_key, action="expire",
                qty=qty, cumulative=qty, price=price, ts=ts,
                reason="expired_at_zero" if price == 0 else "expired"))
        lot.qty -= qty
        if lot.qty <= 0:
            self.closed_by_day.setdefault(self.day_of(ts), set()).add(lot.contract_key)
            del self.lots[lot.contract_key]

    def _anomaly(self, name: str) -> None:
        """Count a survivable oddity. Never silent, never fatal."""
        self.anomalies[name] = self.anomalies.get(name, 0) + 1

    # ── persistence ───────────────────────────────────────────────────────────
    def _emit(self, event: Mapping[str, Any]) -> None:
        """Append one complete line. A torn line is the worst case, so it is one write."""
        if self.path is None:
            return
        payload = dict(event)
        payload["v"] = SCHEMA_VERSION
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())


