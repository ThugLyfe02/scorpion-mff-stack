"""Order placement: a Broker protocol, a deterministic paper broker, and the
Robinhood Agentic HTTP adapter.

Hard rules honoured here:
  * no shell, ever - urllib only, argv never carries remote text
  * dry_run defaults to TRUE; live trading is an explicit opt-in
  * the access token is read from the environment and never logged, echoed in
    an error, or written to the fill log
  * every order carries an Idempotency-Key so a retry cannot double-fill
"""

from __future__ import annotations

import json
import math
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Protocol

from .instruments import ContractResolver, InstrumentError, OptionContract
from .orders import (
    TERMINAL_STATES,
    WORKING_STATES,
    OrderError,
    OrderState,
    TimeInForce,
    tif_from_value,
)
from .types import OrderIntent, Quote


class BrokerError(RuntimeError):
    """Placement failed. The message is always token-free."""


class TransportError(BrokerError):
    """The HTTP transport could not reach the broker. Token-free by construction."""


class OrderLookup(StrEnum):
    """Tri-state answer to 'did this idempotency key ever reach the venue?'."""

    SEEN = "seen"              # the venue has the order
    NOT_SEEN = "not_seen"      # the venue never saw the order
    CANNOT_SAY = "cannot_say"  # the lookup could not decide; the row stays open


class OrderLookupProvider(Protocol):
    """Anything that can say whether an idempotency key reached the venue."""

    def lookup(self, idempotency_key: str) -> OrderLookup: ...


def intent_key(intent: OrderIntent) -> str:
    """The effective idempotency key: the explicit one, else contract:action."""
    return intent.idempotency_key or f"{intent.contract_key}:{intent.action}"


def time_in_force(intent: OrderIntent) -> TimeInForce:
    """The intent's declared TIF, or a token-free refusal naming ``bad_tif``.

    Before this function the broker hardcoded ``gtc``; a caller that asks for a
    duration the venue does not have must be refused here, not silently traded GTC.
    """
    try:
        return tif_from_value(intent.time_in_force)
    except OrderError as exc:
        raise BrokerError(f"{intent.contract_key}: {exc.reason}: {exc.detail}") from None


def replace_limit(limit: object) -> float:
    """A replace/target limit: finite and positive, refused by name otherwise."""
    if isinstance(limit, bool) or not isinstance(limit, (int, float)):
        raise BrokerError(
            f"bad_replace_limit: limit must be a number, got {type(limit).__name__}")
    value = float(limit)
    if not math.isfinite(value) or value <= 0:
        raise BrokerError(f"bad_replace_limit: {limit!r} is not a positive finite price")
    return value


def replace_quantity(qty: object, filled_qty: int) -> int:
    """A replacement quantity: a positive integer that cannot shrink below filled."""
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise BrokerError(
            f"bad_replace_quantity: qty must be a positive integer, got {qty!r}")
    if qty < filled_qty:
        raise BrokerError(
            f"replace_below_filled: qty {qty} is below the {filled_qty} already filled")
    return qty


@dataclass(frozen=True)
class VenueOrder:
    """One order as the venue reports it; fields the venue did not send stay empty.

    This is a *view*, not the local lifecycle: ``state`` is whatever the venue's
    answer said (or ``UNKNOWN`` when it said nothing parseable), and the caller
    decides whether that answer is a confirmation. ``dry_run`` marks an answer the
    adapter fabricated locally so it can never be mistaken for a venue confirmation.
    """

    venue_order_id: str
    state: OrderState
    contract_key: str = ""
    side: str = ""
    qty: int = 0
    filled_qty: int = 0
    limit: float = 0.0
    tif: TimeInForce = TimeInForce.GTC
    reason: str = ""
    dry_run: bool = False

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def active(self) -> bool:
        return not self.terminal

    @property
    def working(self) -> bool:
        return self.state in WORKING_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue_order_id": self.venue_order_id,
            "state": self.state.value,
            "contract_key": self.contract_key,
            "side": self.side,
            "qty": self.qty,
            "filled_qty": self.filled_qty,
            "limit": self.limit,
            "tif": self.tif.value,
            "reason": self.reason,
            "dry_run": self.dry_run,
        }


# The venue's own state spellings, drawn from the same vocabulary `orders.OrderState`
# names. Anything outside this table is refused (`unknown_venue_state`): guessing that
# an unfamiliar string means "working" is how a cancelled order keeps its size.
_STATE_ALIASES: dict[str, OrderState] = {
    "pending": OrderState.PENDING,
    "queued": OrderState.PENDING,
    "submitted": OrderState.PENDING,
    "working": OrderState.WORKING,
    "open": OrderState.WORKING,
    "confirmed": OrderState.WORKING,
    "acknowledged": OrderState.WORKING,
    "new": OrderState.WORKING,
    "partial": OrderState.PARTIAL,
    "partial_fill": OrderState.PARTIAL,
    "partially_filled": OrderState.PARTIAL,
    "filled": OrderState.FILLED,
    "executed": OrderState.FILLED,
    "complete": OrderState.FILLED,
    "completed": OrderState.FILLED,
    "cancelled": OrderState.CANCELLED,
    "canceled": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
    "expired": OrderState.EXPIRED,
    "unknown": OrderState.UNKNOWN,
}


def venue_state(value: object) -> OrderState:
    """Map the venue's state string onto ``OrderState``; refuse the unfamiliar."""
    if isinstance(value, OrderState):
        return value
    if isinstance(value, str):
        state = _STATE_ALIASES.get(value.strip().lower())
        if state is not None:
            return state
    raise BrokerError(
        f"unknown_venue_state: {value!r} is not an order state this adapter understands")


def venue_tif(value: object) -> TimeInForce:
    """Map the venue's TIF string onto ``TimeInForce``; refuse the unfamiliar."""
    try:
        return tif_from_value(value)
    except OrderError as exc:
        raise BrokerError(f"unknown_time_in_force: {exc.detail}") from None


def _first_str(body: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = body.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _int_field(body: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        if name not in body:
            continue
        value = body[name]
        if isinstance(value, bool):
            raise BrokerError(f"malformed_order_field: {name}={value!r} is not an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        raise BrokerError(f"malformed_order_field: {name}={value!r} is not an integer")
    return None


def _float_field(body: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        if name not in body:
            continue
        value = body[name]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise BrokerError(f"malformed_order_field: {name}={value!r} is not a price")
        try:
            number = float(value)
        except ValueError:
            raise BrokerError(
                f"malformed_order_field: {name}={value!r} is not a price") from None
        if not math.isfinite(number):
            raise BrokerError(f"malformed_order_field: {name}={value!r} is not finite")
        return number
    return None


@dataclass(frozen=True)
class Ack:
    ok: bool
    contract_key: str
    qty: int
    price: float
    broker_order_id: str = ""
    error: str = ""
    dry_run: bool = False


class QuoteProvider(Protocol):
    def quote(self, contract_key: str) -> Quote | None: ...


@dataclass
class QuoteScript:
    """Deterministic quotes for replay and tests.

    script = {"QQQ|CALL|719|0DTE": [(1.01, 1.05), (1.11, 1.14)]}
    Each call to quote() consumes the next entry, then sticks on the last one -
    a missing quote must never be invented, and must never silently repeat.
    """

    script: dict[str, list[tuple[float, float]]]
    _idx: dict[str, int] = field(default_factory=dict)

    def quote(self, contract_key: str) -> Quote | None:
        series = self.script.get(contract_key)
        if not series:
            return None
        i = min(self._idx.get(contract_key, 0), len(series) - 1)
        self._idx[contract_key] = i + 1
        bid, ask = series[i]
        return Quote(bid=bid, ask=ask)


@dataclass
class StaticQuotes:
    """One fixed quote per contract, for the simple cases."""

    prices: dict[str, tuple[float, float]]

    def quote(self, contract_key: str) -> Quote | None:
        pair = self.prices.get(contract_key)
        return Quote(bid=pair[0], ask=pair[1]) if pair else None


@dataclass
class PaperBroker:
    """Fills at the limit price. Optional slippage for honest replays.

    The lookup is deterministic: SEEN for a key this broker actually placed,
    NOT_SEEN for anything else - there is no transport to be unsure about.

    Working-order methods are the paper half of the M2 lifecycle. By default a paper
    placement fills immediately (the existing behaviour); ``fill_immediately=False``
    leaves the order ``WORKING`` so cancel/replace/status can be exercised offline
    against a venue that does not answer instantly.
    """

    slippage: float = 0.0
    acks: list[Ack] = field(default_factory=list)
    placed_keys: set[str] = field(default_factory=set)
    fill_immediately: bool = True
    orders: dict[str, VenueOrder] = field(default_factory=dict)

    def place(self, intent: OrderIntent) -> Ack:
        if intent.qty <= 0:
            raise BrokerError(f"refusing a {intent.qty}-contract order")
        if intent.action == "buy":
            price = intent.limit * (1 + self.slippage)
        elif intent.action == "sell":
            price = intent.limit * (1 - self.slippage)
        else:
            raise BrokerError(f"unknown action {intent.action!r}")
        price = round(max(0.01, price), 4)
        tif = time_in_force(intent)
        self.placed_keys.add(intent_key(intent))
        venue_order_id = f"paper-{len(self.acks) + 1}"
        ack = Ack(ok=True, contract_key=intent.contract_key, qty=intent.qty,
                  price=price, broker_order_id=venue_order_id)
        self.acks.append(ack)
        self.orders[venue_order_id] = VenueOrder(
            venue_order_id=venue_order_id,
            state=OrderState.FILLED if self.fill_immediately else OrderState.WORKING,
            contract_key=intent.contract_key,
            side=intent.action,
            qty=intent.qty,
            filled_qty=intent.qty if self.fill_immediately else 0,
            limit=intent.limit,
            tif=tif,
            reason="paper_fill" if self.fill_immediately else "paper_working",
        )
        return ack

    def lookup(self, idempotency_key: str) -> OrderLookup:
        return (OrderLookup.SEEN if idempotency_key in self.placed_keys
                else OrderLookup.NOT_SEEN)

    # ── working orders (paper) ───────────────────────────────────────────────
    def cancel_order(self, venue_order_id: str) -> VenueOrder:
        order = self._known(venue_order_id)
        if order.state is OrderState.CANCELLED:
            return order
        if order.terminal:
            raise BrokerError(
                f"order_terminal: {venue_order_id} is {order.state.value}; a terminal "
                f"order cannot be cancelled")
        cancelled = replace(order, state=OrderState.CANCELLED, reason="paper_cancel")
        self.orders[venue_order_id] = cancelled
        return cancelled

    def replace_order(self, venue_order_id: str, limit: float, qty: int) -> VenueOrder:
        order = self._known(venue_order_id)
        if not order.working:
            raise BrokerError(
                f"order_not_working: {venue_order_id} is {order.state.value}; only a "
                f"working order can be replaced")
        new_limit = replace_limit(limit)
        new_qty = replace_quantity(qty, order.filled_qty)
        state = OrderState.PARTIAL if order.filled_qty else OrderState.WORKING
        replaced = replace(order, limit=round(new_limit, 2), qty=new_qty, state=state,
                           reason="paper_replace")
        self.orders[venue_order_id] = replaced
        return replaced

    def order_status(self, venue_order_id: str) -> VenueOrder:
        return self._known(venue_order_id)

    def open_orders(self) -> list[VenueOrder]:
        return [order for order in self.orders.values() if order.active]

    def _known(self, venue_order_id: str) -> VenueOrder:
        order = self.orders.get(venue_order_id)
        if order is None:
            raise BrokerError(
                f"unknown_order: the paper broker never placed {venue_order_id!r}")
        return order


def mask(secret: str) -> str:
    """Never let a token reach a log line."""
    if not secret:
        return "(unset)"
    return f"{secret[:4]}...{secret[-2:]}" if len(secret) > 8 else "***"


@dataclass
class RobinhoodAgenticBroker:
    """POSTs orders to the Robinhood Agentic endpoint.

    `transport` is injectable so the HTTP shape, the idempotency key and the
    retry ladder are tested against a real local server rather than mocked away.

    ``resolver`` is the optional live contract resolver. When it is set,
    ``_build_request`` resolves the intent's contract key and adds ``osi_symbol``,
    ``instrument_id`` and ``multiplier`` to the body; a refusal happens before the
    request is built, so no HTTP send can follow an unresolved contract. When it is
    absent the payload is exactly what it was before M1.
    """

    base_url: str = field(default_factory=lambda: os.environ.get(
        "MFF_RH_BASE", "https://api.robinhood.com"))
    token: str = field(default_factory=lambda: os.environ.get("MFF_RH_TOKEN", ""))
    account: str = field(default_factory=lambda: os.environ.get("MFF_RH_ACCOUNT", "agentic"))
    lookup_template: str = field(default_factory=lambda: os.environ.get(
        "MFF_RH_ORDER_LOOKUP_TEMPLATE", ""))
    dry_run: bool = True
    timeout_s: float = 5.0
    retries: int = 3
    backoff_s: float = 0.5
    transport: Callable[[urllib.request.Request, float], tuple[int, dict[str, Any]]] | None = None
    sleep: Callable[[float], None] = time.sleep
    acks: list[Ack] = field(default_factory=list)
    resolver: ContractResolver | None = None
    session_date: date | None = None
    clock: Callable[[], float] = time.time
    _dry_orders: dict[str, VenueOrder] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.dry_run and not self.token:
            raise BrokerError("live mode needs MFF_RH_TOKEN; refusing to run dry-armed")

    # ── public ───────────────────────────────────────────────────────────────
    def place(self, intent: OrderIntent) -> Ack:
        if intent.qty <= 0:
            raise BrokerError(f"refusing a {intent.qty}-contract order")
        if self.dry_run:
            venue_order_id = f"dry-{len(self.acks) + 1}"
            ack = Ack(ok=True, contract_key=intent.contract_key, qty=intent.qty,
                      price=intent.limit, broker_order_id=venue_order_id, dry_run=True)
            self.acks.append(ack)
            # A dry-run order never reaches a venue, so it is WORKING (not filled) and
            # dry_run=True. That keeps a fabricated ack visibly distinct from a real one.
            self._dry_orders[venue_order_id] = VenueOrder(
                venue_order_id=venue_order_id, state=OrderState.WORKING,
                contract_key=intent.contract_key, side=intent.action, qty=intent.qty,
                limit=intent.limit, tif=time_in_force(intent), reason="dry_run",
                dry_run=True)
            return ack

        request = self._build_request(intent)
        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                status, body = self._send(request)
            except TransportError as exc:
                # Preserve the classification: the listener's circuit breaker
                # must see this as TRANSPORT (urllib/timeout/OSError), not as a
                # business reject.
                raise TransportError(
                    f"{intent.action} {intent.qty} {intent.contract_key}: {exc}"
                ) from exc
            if status == 0:
                # A transport-shaped response (an injected transport or a wrapped
                # connection failure): no HTTP status means the venue was never
                # reached, which is transport, not a business refusal.
                raise TransportError(
                    f"{intent.action} {intent.qty} {intent.contract_key}: "
                    f"transport error: {body.get('error', 'unknown')}"
                )
            if 200 <= status < 300:
                ack = Ack(ok=True, contract_key=intent.contract_key, qty=intent.qty,
                          price=float(body.get("average_price") or intent.limit),
                          broker_order_id=str(body.get("id") or ""))
                self.acks.append(ack)
                return ack
            last_error = self._describe(status, body)
            if (status in (429,) or 500 <= status < 600) and attempt < self.retries:
                self.sleep(self._retry_after(body, attempt))
                continue
            break
        raise BrokerError(f"{intent.action} {intent.qty} {intent.contract_key}: {last_error}")

    def lookup(self, idempotency_key: str) -> OrderLookup:
        """SEEN/NOT_SEEN/CANNOT_SAY from MFF_RH_ORDER_LOOKUP_TEMPLATE only.

        No template -> CANNOT_SAY without a network call. The template's `{key}`
        placeholder is URL-quoted; the response decides the result (see
        `_interpret_lookup`). A transport failure is CANNOT_SAY, never a guess.
        """
        if not self.lookup_template:
            return OrderLookup.CANNOT_SAY
        url = self.lookup_template.replace("{key}", urllib.parse.quote(idempotency_key))
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        try:
            status, body = self._send(req)
        except TransportError:
            return OrderLookup.CANNOT_SAY
        return self._interpret_lookup(status, body)

    @staticmethod
    def _interpret_lookup(status: int, body: dict[str, Any]) -> OrderLookup:
        """A lookup response decides the tri-state; an ambiguous one cannot say."""
        if status == 404:
            return OrderLookup.NOT_SEEN
        if not (200 <= status < 300):
            return OrderLookup.CANNOT_SAY
        found = body.get("found")
        if isinstance(found, bool):
            return OrderLookup.SEEN if found else OrderLookup.NOT_SEEN
        results = body.get("results")
        if isinstance(results, list):
            return OrderLookup.SEEN if results else OrderLookup.NOT_SEEN
        if body.get("id") or body.get("order_id") or body.get("order"):
            return OrderLookup.SEEN
        return OrderLookup.CANNOT_SAY

    # ── working orders: cancel / replace / status / open ─────────────────────
    def cancel_order(self, venue_order_id: str) -> VenueOrder:
        """Ask the venue to cancel one order. Same retry/redaction ladder as place.

        The answer is a ``VenueOrder``: ``cancelled`` only when the venue said so,
        ``unknown`` when it answered 2xx without a state it understands. A 404 is
        ``unknown_order`` - the adapter never invents a cancellation.
        """
        order_id = self._order_id(venue_order_id, "cancel")
        if self.dry_run:
            return self._dry_action(order_id, "cancel")
        request = self._order_request(
            "POST", f"/orders/{urllib.parse.quote(order_id, safe='')}/cancel",
            {"account": self.account}, f"cancel:{order_id}")
        body = self._send_order(request, f"cancel {order_id}")
        return self._venue_order(body, order_id)

    def replace_order(self, venue_order_id: str, limit: float, qty: int) -> VenueOrder:
        """Ask the venue to replace a working order's limit (and quantity).

        The idempotency key covers the order *and* the replacement values, so a
        retry of the same replace cannot apply twice while a genuinely different
        replacement is a new request rather than a deduplicated one.
        """
        order_id = self._order_id(venue_order_id, "replace")
        new_limit = replace_limit(limit)
        new_qty = replace_quantity(qty, 0)
        if self.dry_run:
            return self._dry_action(order_id, "replace", limit=new_limit, qty=new_qty)
        payload = {
            "account": self.account,
            "quantity": new_qty,
            "limit_price": round(new_limit, 2),
        }
        key = f"replace:{order_id}:{new_qty}:{round(new_limit, 2):.2f}"
        request = self._order_request(
            "POST", f"/orders/{urllib.parse.quote(order_id, safe='')}/replace",
            payload, key)
        body = self._send_order(request, f"replace {order_id}")
        return self._venue_order(body, order_id)

    def order_status(self, venue_order_id: str) -> VenueOrder:
        """Read one order's state from the venue. Never guesses a missing state."""
        order_id = self._order_id(venue_order_id, "order status")
        if self.dry_run:
            order = self._dry_orders.get(order_id)
            if order is None:
                raise BrokerError(
                    f"unknown_order: the dry-run broker never placed {order_id!r}")
            return order
        request = self._order_request(
            "GET", f"/orders/{urllib.parse.quote(order_id, safe='')}", None, "")
        body = self._send_order(request, f"order status {order_id}")
        return self._venue_order(body, order_id)

    def open_orders(self) -> list[VenueOrder]:
        """Every order the venue still considers live (not terminal)."""
        if self.dry_run:
            return [order for order in self._dry_orders.values() if order.active]
        request = self._order_request(
            "GET", f"/orders?account={urllib.parse.quote(self.account)}", None, "")
        body = self._send_order(request, "open orders")
        return self._venue_orders(body)

    @staticmethod
    def _order_id(venue_order_id: object, operation: str) -> str:
        if not isinstance(venue_order_id, str) or not venue_order_id.strip():
            raise BrokerError(f"{operation} needs a venue_order_id")
        return venue_order_id.strip()

    def _dry_action(self, venue_order_id: str, operation: str,
                    limit: float | None = None, qty: int | None = None) -> VenueOrder:
        """The dry-run half of cancel/replace: deterministic, and marked dry_run."""
        order = self._dry_orders.get(venue_order_id)
        if order is None:
            raise BrokerError(
                f"unknown_order: the dry-run broker never placed {venue_order_id!r}")
        if operation == "cancel":
            if order.state is OrderState.CANCELLED:
                return order
            if order.terminal:
                raise BrokerError(
                    f"order_terminal: {venue_order_id} is {order.state.value}; a terminal "
                    f"order cannot be cancelled")
            cancelled = replace(order, state=OrderState.CANCELLED,
                                reason="dry_run_cancel", dry_run=True)
            self._dry_orders[venue_order_id] = cancelled
            return cancelled
        if not order.working:
            raise BrokerError(
                f"order_not_working: {venue_order_id} is {order.state.value}; only a "
                f"working order can be replaced")
        if limit is None or qty is None:        # pragma: no cover - guarded by caller
            raise BrokerError("bad_replace_limit: a dry-run replace needs limit and qty")
        state = OrderState.PARTIAL if order.filled_qty else OrderState.WORKING
        replaced = replace(order, limit=round(limit, 2), qty=qty, state=state,
                           reason="dry_run_replace", dry_run=True)
        self._dry_orders[venue_order_id] = replaced
        return replaced

    def _order_request(self, method: str, path: str, payload: dict[str, Any] | None,
                       idempotency_key: str) -> urllib.request.Request:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return urllib.request.Request(
            url=f"{self.base_url.rstrip('/')}{path}", data=data, method=method,
            headers=headers)

    def _send_order(self, request: urllib.request.Request, what: str) -> dict[str, Any]:
        """The place() retry ladder, shared by every order-management call."""
        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                status, body = self._send(request)
            except TransportError as exc:
                # Preserve the classification: a dead socket is TRANSPORT, not a
                # business refusal. The message is token-free by construction.
                raise TransportError(f"{what}: {exc}") from exc
            if status == 0:
                raise TransportError(
                    f"{what}: transport error: {body.get('error', 'unknown')}")
            if 200 <= status < 300:
                return body
            if status == 404:
                raise BrokerError(f"{what}: unknown_order: the venue has no such order")
            last_error = self._describe(status, body)
            if (status in (429,) or 500 <= status < 600) and attempt < self.retries:
                self.sleep(self._retry_after(body, attempt))
                continue
            break
        raise BrokerError(f"{what}: {last_error}")

    def _venue_order(self, body: object, fallback_id: str) -> VenueOrder:
        """Parse one venue order answer. A state it did not report is UNKNOWN."""
        if not isinstance(body, Mapping):
            raise BrokerError(
                f"malformed_order_answer: expected a JSON object, "
                f"got {type(body).__name__}")
        venue_order_id = _first_str(body, "id", "order_id", "venue_order_id") or fallback_id
        if not venue_order_id:
            raise BrokerError("malformed_order_answer: the venue order has no id")
        state_raw = body.get("state", body.get("status"))
        reason = ""
        if state_raw is None:
            state = OrderState.UNKNOWN
            reason = "state_not_reported"
        else:
            state = venue_state(state_raw)
        qty = _int_field(body, "quantity", "qty", "contracts")
        filled = _int_field(body, "cumulative_quantity", "filled_quantity", "filled_qty")
        limit = _float_field(body, "limit_price")
        tif_raw = body.get("time_in_force", body.get("tif"))
        return VenueOrder(
            venue_order_id=venue_order_id,
            state=state,
            contract_key=_first_str(body, "contract_key", "symbol"),
            side=_first_str(body, "side", "action"),
            qty=max(0, qty or 0),
            filled_qty=max(0, filled or 0),
            limit=max(0.0, limit or 0.0),
            tif=TimeInForce.GTC if tif_raw is None else venue_tif(tif_raw),
            reason=reason,
        )

    def _venue_orders(self, body: object) -> list[VenueOrder]:
        rows: object = body
        if isinstance(body, Mapping):
            rows = body.get("orders", body.get("results", body.get("items")))
        if not isinstance(rows, list):
            raise BrokerError(
                "malformed_open_orders: the venue answer has no order list "
                "(looked for orders/results/items)")
        orders: list[VenueOrder] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise BrokerError(
                    f"malformed_open_orders: row {index} is {type(row).__name__}, "
                    f"not an object")
            orders.append(self._venue_order(row, ""))
        return orders

    # ── internals ────────────────────────────────────────────────────────────
    def _build_request(self, intent: OrderIntent) -> urllib.request.Request:
        payload = {
            "account": self.account,
            "contract_key": intent.contract_key,
            "side": intent.action,
            "quantity": intent.qty,
            "limit_price": round(intent.limit, 2),
            "time_in_force": time_in_force(intent).value,
            "reason": intent.reason[:120],
        }
        if self.resolver is not None:
            payload.update(self._instrument_identity(intent))
        idempotency_key = intent_key(intent)
        req = urllib.request.Request(
            url=f"{self.base_url.rstrip('/')}/orders",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Idempotency-Key": idempotency_key,
            },
        )
        return req

    def _instrument_identity(self, intent: OrderIntent) -> dict[str, Any]:
        """Resolve the intent's contract into the venue's identity fields.

        A refusal names the reason and is raised before any request exists, which is
        what guarantees "no resolution, no send". The internal contract key is never
        removed: the venue adapter keeps sending it for compatibility until the
        endpoint's field names are confirmed, but the order can now be *resolved*.
        """
        assert self.resolver is not None              # caller guards
        session = self.session_date
        if session is None:
            session = datetime.fromtimestamp(self.clock(), UTC).date()
        try:
            contract = OptionContract.from_contract_key(intent.contract_key, session)
            ref = self.resolver.resolve(contract)
        except InstrumentError as exc:
            raise BrokerError(
                f"{intent.contract_key}: {exc.reason}: {exc.detail}") from None
        return {
            "osi_symbol": ref.osi_symbol,
            "instrument_id": ref.instrument_id,
            "multiplier": ref.multiplier,
        }

    def _send(self, request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
        if self.transport is not None:
            return self.transport(request, self.timeout_s)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"raw": raw[:200]}
            return exc.code, body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"transport error: {str(exc)[:200]}") from exc

    @staticmethod
    def _retry_after(body: dict[str, Any], attempt: int) -> float:
        try:
            return max(0.0, float(body.get("retry_after") or 0.0)) or attempt * 0.5
        except (TypeError, ValueError):
            return attempt * 0.5

    def _describe(self, status: int, body: dict[str, Any]) -> str:
        if status == 0:
            return f"transport error: {body.get('error', 'unknown')}"
        detail = body.get("detail") or body.get("error") or body.get("raw") or ""
        return f"HTTP {status}" + (f": {str(detail)[:160]}" if detail else "")


def jitter(base: float, spread: float = 0.25) -> float:
    """Backoff helper for the live loop; deterministic when spread=0."""
    return base * (1 + (random.random() * spread if spread else 0))


class QuoteError(RuntimeError):
    """The quote feed could not price a contract. The engine fails closed."""


@dataclass
class HttpQuoteProvider:
    """Bid/ask for a contract key from a URL template YOU supply.

    Deliberately not hard-coded to a vendor schema: the Robinhood options-quote
    response shape is not invented here. You set

        MFF_QUOTE_URL_TEMPLATE=https://.../quotes?symbol={key}
        MFF_QUOTE_BID_PATH=results.0.bid_price
        MFF_QUOTE_ASK_PATH=results.0.ask_price

    and this reads those dotted paths out of the JSON. Unconfigured, `quote()`
    raises QuoteError and the engine rejects the entry with `no_quote` - an
    unpriceable signal must never become a guessed limit price.
    """

    template: str = field(default_factory=lambda: os.environ.get("MFF_QUOTE_URL_TEMPLATE", ""))
    bid_path: str = field(default_factory=lambda: os.environ.get("MFF_QUOTE_BID_PATH", "bid"))
    ask_path: str = field(default_factory=lambda: os.environ.get("MFF_QUOTE_ASK_PATH", "ask"))
    token: str = field(default_factory=lambda: os.environ.get("MFF_RH_TOKEN", ""))
    timeout_s: float = 3.0
    cache_s: float = 5.0
    transport: Callable[[urllib.request.Request, float], tuple[int, dict[str, Any]]] | None = None
    _cache: dict[str, tuple[float, Quote]] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.template)

    def quote(self, contract_key: str) -> Quote | None:
        now = time.time()
        cached = self._cache.get(contract_key)
        if cached and now - cached[0] <= self.cache_s:
            return cached[1]
        if not self.template:
            raise QuoteError("no quote feed configured (set MFF_QUOTE_URL_TEMPLATE)")
        url = self.template.replace("{key}", urllib.parse.quote(contract_key))
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        status, body = self._send(req)
        if not (200 <= status < 300):
            raise QuoteError(f"quote feed HTTP {status}")
        bid = _dig(body, self.bid_path)
        ask = _dig(body, self.ask_path)
        if bid is None or ask is None:
            raise QuoteError(f"quote fields missing ({self.bid_path}/{self.ask_path})")
        quote = Quote(bid=float(bid), ask=float(ask))
        self._cache[contract_key] = (now, quote)
        return quote

    def _send(self, request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
        if self.transport is not None:
            return self.transport(request, self.timeout_s)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            return exc.code, {}
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0, {}


def _dig(payload: Any, dotted: str) -> Any:
    """Read 'results.0.bid_price' out of nested dicts/lists. None when absent."""
    cur = payload
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur
