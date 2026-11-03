"""Working-order lifecycle: time-in-force, an explicit state machine, a durable order
registry, and the stale-limit guard.

`place()` was the whole story until now: one POST, one ack, and the fill log. That
leaves three live-money holes this module closes.

1. **A stale entry limit can fill later.** The engine prices an entry as a
   marketable limit; if the quote moves the order sits *working* and nobody is
   watching it. `StaleLimitGuard` ages working orders against an injected clock and
   asks for the entry to be cancelled (``STALE_ENTRY``) instead of discovering the
   fill after the thesis is gone.
2. **An unfilled exit can never be pulled.** A working sell is the *only* protection
   on a live position, so it is deliberately asymmetric: an exit is never
   auto-cancelled into nothing. It is paged as ``EXIT_UNFILLED_PAGE`` and a human (or
   the operator flatten) decides. Cancelling an exit automatically would turn "the
   venue is slow" into "the position is naked".
3. **No order had durable state.** The fill log records decisions, the confirmed
   execution book records fills; neither answers "what is this order doing right
   now, and what did we already ask the venue?". `OrderRegistry` is an append-only
   ``live/orders.jsonl`` replayed on start: every transition is one complete JSON
   line with a contiguous sequence number, a torn **final** line (a crash
   mid-append) is tolerated and counted, and corruption earlier in the file refuses
   to load rather than silently dropping the middle of an order's life.

The state machine
-----------------

`OrderState` is a small, explicit set: ``PENDING`` (sent, no venue answer),
``WORKING`` (acked, live), ``PARTIAL`` (some contracts filled), ``FILLED`` /
``CANCELLED`` / ``REJECTED`` / ``EXPIRED`` (terminal), and ``UNKNOWN`` (a send that
timed out; the venue may or may not hold it - non-terminal on purpose, so a late
venue answer can still resolve it).

`apply_event` is the one transition function. It is pure: it takes an order, an
event, an injected timestamp and the event's data, and returns a **new**
``WorkingOrder`` or raises ``OrderError`` with a named reason. The invariants are
explicit, never defaults:

* a terminal state is terminal - every event after ``FILLED``/``CANCELLED``/
  ``REJECTED``/``EXPIRED`` is ``terminal_state``;
* a fill can never exceed the quantity (``fill_exceeds_qty``) and a cumulative fill
  never goes backwards (``regressing_fill``); an identical cumulative number is
  ``duplicate_fill``;
* ``fill`` must complete the order (``fill_short_of_qty``: use ``partial``);
  ``partial`` must leave something working (``partial_fills_the_order``);
* ``replace_requested`` is legal only from a venue-working state (``WORKING`` or
  ``PARTIAL``); ``replace_confirmed`` is the **only** event that changes ``limit``
  (and ``qty``), so a replace request that the venue never confirms cannot make the
  local book claim a price the venue does not have;
* a late ``reject`` is terminal but never un-does a fill; a ``timeout`` on a
  ``PENDING`` send becomes ``UNKNOWN``, not a fake reject.

Every method on ``OrderLifecycle`` records the resulting state in the registry (when
one is configured), so the in-memory view and the JSONL cannot diverge through this
class.

Entry/exit asymmetry
--------------------

A ``WorkingOrder.side`` is the **order action**, ``"buy"`` or ``"sell"`` - the
contract itself (and its CALL/PUT side) is in ``contract``. That is deliberate: the
guard's decision depends on whether the order is opening or closing risk, and a
CALL/PUT field would not answer it.

``sweep(now)``
--------------

This is the hook the Listener calls on a timer. ``OrderLifecycle.sweep(now)`` returns
a list of ``SweepAction`` values whose ``kind`` is ``cancel`` or ``page`` (``none``
exists in the vocabulary for ``StaleLimitGuard.evaluate`` so a caller can switch
exhaustively; ``sweep`` omits the quiet ones). For a stale entry with
``cancel_stale=True`` it records ``cancel_requested`` **before** calling the broker's
cancel, and a transport/venue refusal turns the action into a ``cancel_failed`` page
rather than pretending the cancel happened.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("live/orders.jsonl")
SCHEMA_VERSION = 1

# ── sweep reasons: what the caller is being asked to do ──────────────────────
STALE_ENTRY = "STALE_ENTRY"
EXIT_UNFILLED_PAGE = "EXIT_UNFILLED_PAGE"
CANCEL_FAILED = "cancel_failed"
CANCEL_UNAVAILABLE = "cancel_unavailable"

# ── transition reasons recorded on the order ─────────────────────────────────
CANCEL_REQUESTED = "cancel_requested"
CANCEL_CONFIRMED = "cancel_confirmed"
REPLACE_REQUESTED = "replace_requested"
REPLACE_CONFIRMED = "replace_confirmed"
TIMEOUT = "timeout"
REJECTED = "rejected"
EXPIRED = "expired"
FILLED = "filled"

# ── named construction refusals ──────────────────────────────────────────────
BAD_CLIENT_ORDER_ID = "bad_client_order_id"
BAD_CONTRACT = "bad_contract"
BAD_SIDE = "bad_side"
BAD_QUANTITY = "bad_quantity"
BAD_LIMIT = "bad_limit"
BAD_FILLED_QTY = "bad_filled_qty"
BAD_TIF = "bad_tif"
BAD_STATE = "bad_state"
BAD_TIMESTAMP = "bad_timestamp"
BAD_PENDING = "bad_pending"
BAD_ORDER_ROW = "bad_order_row"
BAD_GUARD_WINDOW = "bad_guard_window"
BAD_EVENT = "bad_event"

# ── named registry refusals ──────────────────────────────────────────────────
REGISTRY_CORRUPT = "registry_corrupt"
REGISTRY_SEQUENCE = "registry_sequence"
REGISTRY_SCHEMA = "registry_schema"
REGISTRY_IO = "registry_io"

EVENTS = frozenset({
    "ack", "partial", "fill", "cancel_requested", "cancel_confirmed",
    "replace_requested", "replace_confirmed", "reject", "expire", "timeout",
})


class OrderError(ValueError):
    """A working-order operation that must not proceed. ``reason`` is always named."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


class OrderRegistryError(OrderError):
    """The order log cannot be trusted, or a row would corrupt the replay."""


class IllegalTransition(OrderError):
    """An event that cannot follow the current state. The reason names both.

    A caller that wants to distinguish "the order is over" from "the state machine
    does not have that edge" branches on this type; everything else about the
    refusal (a named ``reason``) is the same as every other ``OrderError``.
    """


class TimeInForce(StrEnum):
    """Order duration. ``gtc`` was hardcoded in the broker before this enum existed."""

    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


def tif_from_value(value: object) -> TimeInForce:
    """Coerce a declared TIF; refuse anything the venue would not recognise."""
    if isinstance(value, TimeInForce):
        return value
    if isinstance(value, str):
        try:
            return TimeInForce(value.strip().lower())
        except ValueError:
            pass
    raise OrderError(
        BAD_TIF,
        f"{value!r} is not one of {', '.join(item.value for item in TimeInForce)}")


class OrderState(StrEnum):
    """Where an order is in its life. ``UNKNOWN`` is non-terminal by design."""

    PENDING = "pending"       # sent, no venue answer yet
    WORKING = "working"       # venue acknowledged, live
    PARTIAL = "partial"       # some contracts filled, remainder working
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"       # timed out: the venue may or may not have it


TERMINAL_STATES = frozenset({
    OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED,
})
WORKING_STATES = frozenset({OrderState.WORKING, OrderState.PARTIAL})
ACTIVE_STATES = frozenset(state for state in OrderState if state not in TERMINAL_STATES)
ORDER_SIDES = frozenset({"buy", "sell"})

_ORDER_FIELDS = (
    "client_order_id", "contract", "side", "qty", "limit", "venue_order_id",
    "filled_qty", "tif", "state", "created_at", "updated_at", "reason",
    "pending_limit", "pending_qty", "cancel_requested",
)


def _number(value: object, reason: str, what: str) -> float:
    """A finite number or a named refusal. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OrderError(reason, f"{what} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise OrderError(reason, f"{what} is not finite: {value!r}")
    return number


def _positive(value: object, reason: str, what: str) -> float:
    number = _number(value, reason, what)
    if number <= 0:
        raise OrderError(reason, f"{what} must be positive, got {value!r}")
    return number


def _count(value: object, reason: str, what: str) -> int:
    """A non-negative integer or a named refusal."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise OrderError(reason, f"{what} must be an integer, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class WorkingOrder:
    """One working order as this process knows it, immutable between transitions.

    ``side`` is the order action (``"buy"``/``"sell"``); ``contract`` is the engine's
    contract key. ``pending_limit``/``pending_qty``/``cancel_requested`` exist so a
    request that the venue has not confirmed is durable - a restart must know that a
    cancel was already asked for, or it will ask twice.
    """

    client_order_id: str
    contract: str
    side: str
    qty: int
    limit: float
    venue_order_id: str = ""
    filled_qty: int = 0
    tif: TimeInForce = TimeInForce.GTC
    state: OrderState = OrderState.PENDING
    created_at: float = 0.0
    updated_at: float = 0.0
    reason: str = ""
    pending_limit: float | None = None
    pending_qty: int | None = None
    cancel_requested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.client_order_id, str) or not self.client_order_id.strip():
            raise OrderError(
                BAD_CLIENT_ORDER_ID, "an order without a client_order_id cannot be tracked")
        object.__setattr__(self, "client_order_id", self.client_order_id.strip())
        if not isinstance(self.contract, str) or not self.contract.strip():
            raise OrderError(BAD_CONTRACT, f"{self.client_order_id}: empty contract key")
        object.__setattr__(self, "contract", self.contract.strip())
        if not isinstance(self.side, str) or self.side.strip().lower() not in ORDER_SIDES:
            raise OrderError(
                BAD_SIDE,
                f"{self.client_order_id}: side must be buy or sell (the order action), "
                f"got {self.side!r}")
        object.__setattr__(self, "side", self.side.strip().lower())
        quantity = _count(self.qty, BAD_QUANTITY, f"{self.client_order_id}: qty")
        if quantity <= 0:
            raise OrderError(BAD_QUANTITY, f"{self.client_order_id}: qty must be positive")
        object.__setattr__(self, "qty", quantity)
        object.__setattr__(self, "limit", _positive(
            self.limit, BAD_LIMIT, f"{self.client_order_id}: limit"))
        if not isinstance(self.venue_order_id, str):
            raise OrderError(
                BAD_ORDER_ROW, f"{self.client_order_id}: venue_order_id must be a string")
        filled = _count(self.filled_qty, BAD_FILLED_QTY,
                        f"{self.client_order_id}: filled_qty")
        if filled < 0 or filled > quantity:
            raise OrderError(
                BAD_FILLED_QTY,
                f"{self.client_order_id}: filled_qty {filled} is outside 0..{quantity}")
        if not isinstance(self.cancel_requested, bool):
            raise OrderError(
                BAD_ORDER_ROW, f"{self.client_order_id}: cancel_requested must be a bool")
        try:
            object.__setattr__(self, "tif", tif_from_value(self.tif))
        except OrderError as exc:
            raise OrderError(BAD_TIF, f"{self.client_order_id}: {exc.detail}") from None
        if isinstance(self.state, str):
            try:
                state = OrderState(self.state.strip().lower())
            except ValueError:
                raise OrderError(
                    BAD_STATE, f"{self.client_order_id}: {self.state!r} is not a state") from None
        elif isinstance(self.state, OrderState):
            state = self.state
        else:
            raise OrderError(
                BAD_STATE, f"{self.client_order_id}: state must be an OrderState")
        object.__setattr__(self, "state", state)
        created = _number(self.created_at, BAD_TIMESTAMP, f"{self.client_order_id}: created_at")
        if created < 0:
            raise OrderError(BAD_TIMESTAMP, f"{self.client_order_id}: negative created_at")
        updated = _number(self.updated_at, BAD_TIMESTAMP, f"{self.client_order_id}: updated_at")
        if updated < 0:
            raise OrderError(BAD_TIMESTAMP, f"{self.client_order_id}: negative updated_at")
        if updated == 0.0 and created > 0.0:
            # A caller that stamps only created_at means "nothing has happened yet";
            # normalising here keeps the age math and the replay honest.
            updated = created
            object.__setattr__(self, "updated_at", updated)
        if updated < created:
            raise OrderError(
                BAD_TIMESTAMP,
                f"{self.client_order_id}: updated_at {updated} is before created_at {created}")
        if not isinstance(self.reason, str):
            raise OrderError(BAD_ORDER_ROW, f"{self.client_order_id}: reason must be a string")
        if self.pending_limit is not None:
            object.__setattr__(self, "pending_limit", _positive(
                self.pending_limit, BAD_PENDING, f"{self.client_order_id}: pending_limit"))
        if self.pending_qty is not None:
            pending_qty = _count(self.pending_qty, BAD_PENDING,
                                 f"{self.client_order_id}: pending_qty")
            if pending_qty <= 0:
                raise OrderError(
                    BAD_PENDING, f"{self.client_order_id}: pending_qty must be positive")
            if pending_qty < filled:
                raise OrderError(
                    BAD_PENDING,
                    f"{self.client_order_id}: pending_qty {pending_qty} is below "
                    f"filled_qty {filled}")
            object.__setattr__(self, "pending_qty", pending_qty)

    # ── derived answers ──────────────────────────────────────────────────────
    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def active(self) -> bool:
        return not self.terminal

    @property
    def working(self) -> bool:
        """Venue-live: acked and not yet done. ``PENDING`` is *sent*, not working."""
        return self.state in WORKING_STATES

    @property
    def is_entry(self) -> bool:
        return self.side == "buy"

    @property
    def is_exit(self) -> bool:
        return self.side == "sell"

    @property
    def remaining(self) -> int:
        return max(0, self.qty - self.filled_qty)

    def age(self, now: float) -> float:
        """Seconds since the last thing that changed this order. Never negative."""
        moment = _number(now, BAD_TIMESTAMP, "now")
        last = self.updated_at if self.updated_at else self.created_at
        return max(0.0, moment - last)

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "venue_order_id": self.venue_order_id,
            "contract": self.contract,
            "side": self.side,
            "qty": self.qty,
            "filled_qty": self.filled_qty,
            "limit": self.limit,
            "tif": self.tif.value,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reason": self.reason,
            "pending_limit": self.pending_limit,
            "pending_qty": self.pending_qty,
            "cancel_requested": self.cancel_requested,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> WorkingOrder:
        """Rebuild an order from a registry row, ignoring registry metadata."""
        if not isinstance(payload, Mapping):
            raise OrderError(BAD_ORDER_ROW, f"order row is {type(payload).__name__}, not an object")
        kwargs: dict[str, Any] = {}
        for name in _ORDER_FIELDS:
            if name in payload:
                kwargs[name] = payload[name]
        try:
            return cls(**kwargs)
        except TypeError as exc:
            raise OrderError(BAD_ORDER_ROW, f"order row cannot be rebuilt: {exc}") from None


def _illegal(order: WorkingOrder, event: str) -> IllegalTransition:
    """The named "no" for an event that cannot follow this state."""
    return IllegalTransition(
        f"illegal_{event}_from_{order.state.value}",
        f"{order.client_order_id} is {order.state.value}; {event} cannot follow")


def apply_event(
    order: WorkingOrder,
    event: str,
    *,
    ts: float,
    venue_order_id: str = "",
    filled_qty: int | None = None,
    limit: float | None = None,
    qty: int | None = None,
    reason: str = "",
) -> WorkingOrder:
    """Apply one lifecycle event to ``order``; return a new order or refuse by name.

    Pure: no I/O, no clock, no mutation. ``ts`` is the injected event instant (the
    caller's clock), and an event stamped before the order existed is
    ``bad_timestamp`` rather than an ordering that the replay would have to guess at.
    """
    moment = _number(ts, BAD_TIMESTAMP, "event ts")
    if moment < order.created_at:
        raise OrderError(
            BAD_TIMESTAMP,
            f"{order.client_order_id}: event at {moment} is before created_at {order.created_at}")
    if not isinstance(event, str) or event not in EVENTS:
        raise OrderError(BAD_EVENT, f"{event!r} is not one of {', '.join(sorted(EVENTS))}")
    if order.terminal:
        raise IllegalTransition(
            "terminal_state",
            f"{order.client_order_id} is {order.state.value}: a terminal state is terminal")
    if not isinstance(reason, str):
        raise OrderError(BAD_ORDER_ROW, "transition reason must be a string")

    if event == "ack":
        if order.state not in (OrderState.PENDING, OrderState.UNKNOWN):
            raise _illegal(order, event)
        if not venue_order_id:
            raise OrderError(
                "missing_venue_order_id",
                f"{order.client_order_id}: an ack must name the venue's order id")
        if order.venue_order_id and order.venue_order_id != venue_order_id:
            raise OrderError(
                "venue_order_id_mismatch",
                f"{order.client_order_id}: ack names {venue_order_id!r} but the order is "
                f"{order.venue_order_id!r}")
        return replace(order, venue_order_id=venue_order_id, state=OrderState.WORKING,
                       updated_at=moment, reason=reason or "acknowledged")

    if event == "partial":
        if filled_qty is None:
            raise OrderError(
                "missing_filled_qty", f"{order.client_order_id}: partial needs a cumulative qty")
        cumulative = _count(filled_qty, BAD_FILLED_QTY,
                            f"{order.client_order_id}: cumulative filled_qty")
        _check_cumulative(order, cumulative)
        if cumulative == order.qty:
            raise OrderError(
                "partial_fills_the_order",
                f"{order.client_order_id}: cumulative {cumulative} fills {order.qty}; use fill")
        return replace(order, filled_qty=cumulative, state=OrderState.PARTIAL,
                       updated_at=moment, reason=reason or "partial_fill")

    if event == "fill":
        cumulative = order.qty if filled_qty is None else _count(
            filled_qty, BAD_FILLED_QTY, f"{order.client_order_id}: cumulative filled_qty")
        _check_cumulative(order, cumulative)
        if cumulative < order.qty:
            raise OrderError(
                "fill_short_of_qty",
                f"{order.client_order_id}: fill of {cumulative} leaves {order.qty} unfilled; "
                f"a partial fill is a partial")
        return replace(order, filled_qty=order.qty, state=OrderState.FILLED,
                       pending_limit=None, pending_qty=None, cancel_requested=False,
                       updated_at=moment, reason=reason or FILLED)

    if event == "cancel_requested":
        if order.cancel_requested:
            raise OrderError(
                "duplicate_cancel_request",
                f"{order.client_order_id}: a cancel is already requested")
        return replace(order, cancel_requested=True, updated_at=moment,
                       reason=reason or CANCEL_REQUESTED)

    if event == "cancel_confirmed":
        return replace(order, state=OrderState.CANCELLED, cancel_requested=False,
                       pending_limit=None, pending_qty=None, updated_at=moment,
                       reason=reason or CANCEL_CONFIRMED)

    if event == "replace_requested":
        if order.state not in WORKING_STATES:
            raise _illegal(order, event)
        if order.cancel_requested:
            raise OrderError(
                "replace_after_cancel",
                f"{order.client_order_id}: a cancel is already requested")
        new_limit = order.limit if limit is None else _positive(
            limit, "bad_replace_limit", f"{order.client_order_id}: new limit")
        new_qty = order.qty if qty is None else _count(
            qty, "bad_replace_qty", f"{order.client_order_id}: new qty")
        if new_qty <= 0:
            raise OrderError(
                "bad_replace_qty", f"{order.client_order_id}: new qty must be positive")
        if new_qty < order.filled_qty:
            raise OrderError(
                "replace_below_filled",
                f"{order.client_order_id}: cannot replace qty to {new_qty} below the "
                f"{order.filled_qty} already filled")
        if new_limit == order.limit and new_qty == order.qty:
            raise OrderError(
                "replace_changes_nothing",
                f"{order.client_order_id}: the replacement is identical to the order")
        return replace(order, pending_limit=new_limit, pending_qty=new_qty,
                       updated_at=moment, reason=reason or REPLACE_REQUESTED)

    if event == "replace_confirmed":
        if order.state not in WORKING_STATES:
            raise _illegal(order, event)
        if order.pending_limit is None and order.pending_qty is None:
            raise OrderError(
                "no_pending_replace",
                f"{order.client_order_id}: the venue confirmed a replace that was never "
                f"requested")
        new_limit = order.pending_limit if order.pending_limit is not None else order.limit
        new_qty = order.pending_qty if order.pending_qty is not None else order.qty
        if new_qty < order.filled_qty:
            raise OrderError(
                "replace_below_filled",
                f"{order.client_order_id}: confirmed qty {new_qty} is below the "
                f"{order.filled_qty} already filled")
        state = (OrderState.PARTIAL
                 if 0 < order.filled_qty < new_qty else OrderState.WORKING)
        return replace(order, limit=new_limit, qty=new_qty, state=state,
                       pending_limit=None, pending_qty=None, cancel_requested=False,
                       updated_at=moment, reason=reason or REPLACE_CONFIRMED)

    if event == "reject":
        return replace(order, state=OrderState.REJECTED, cancel_requested=False,
                       pending_limit=None, pending_qty=None, updated_at=moment,
                       reason=reason or REJECTED)

    if event == "expire":
        if order.state is not OrderState.PENDING and order.state not in WORKING_STATES:
            raise _illegal(order, event)
        return replace(order, state=OrderState.EXPIRED, cancel_requested=False,
                       pending_limit=None, pending_qty=None, updated_at=moment,
                       reason=reason or EXPIRED)

    if event == "timeout":
        if order.state is not OrderState.PENDING:
            raise _illegal(order, event)
        return replace(order, state=OrderState.UNKNOWN, updated_at=moment,
                       reason=reason or TIMEOUT)

    raise OrderError(BAD_EVENT, f"{event!r} has no transition")  # pragma: no cover


def _check_cumulative(order: WorkingOrder, cumulative: int) -> None:
    """Refuse a fill that is impossible or a repeat of a number already applied."""
    if cumulative > order.qty:
        raise OrderError(
            "fill_exceeds_qty",
            f"{order.client_order_id}: cumulative {cumulative} exceeds qty {order.qty}")
    if cumulative == order.filled_qty:
        raise OrderError(
            "duplicate_fill",
            f"{order.client_order_id}: cumulative {cumulative} is already applied "
            f"(a repeat fill would double-fill)")
    if cumulative < order.filled_qty:
        raise OrderError(
            "regressing_fill",
            f"{order.client_order_id}: cumulative {cumulative} goes backwards from "
            f"{order.filled_qty}")


class SweepKind(StrEnum):
    """What a sweep asks the caller to do. ``NONE`` is the explicit no-action answer."""

    CANCEL = "cancel"
    PAGE = "page"
    NONE = "none"


@dataclass(frozen=True)
class SweepAction:
    """One answer from the stale-limit sweep, named so nothing is silently skipped."""

    kind: SweepKind
    client_order_id: str
    reason: str
    venue_order_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "client_order_id": self.client_order_id,
            "reason": self.reason,
            "venue_order_id": self.venue_order_id,
        }


@dataclass(frozen=True)
class StaleLimitGuard:
    """Find working entry limits that have stopped being a plan.

    The asymmetry is the safety property, not an oversight: an entry that is stale is
    a thesis that expired and may be cancelled; an exit that is stale is the only
    thing keeping a live position from being naked, so it is *paged*, never cancelled
    by a timer. ``cancel_stale`` opts into cancelling entries only.
    """

    max_working_seconds: float = 300.0
    cancel_stale: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "max_working_seconds",
            _positive(self.max_working_seconds, BAD_GUARD_WINDOW, "max_working_seconds"))
        if not isinstance(self.cancel_stale, bool):
            raise OrderError(BAD_GUARD_WINDOW, "cancel_stale must be a bool")

    def age_of(self, order: WorkingOrder, now: float) -> float:
        return order.age(now)

    def stale(self, order: WorkingOrder, now: float) -> bool:
        """A *working* order untouched for the configured window."""
        return order.working and order.age(now) >= self.max_working_seconds

    def evaluate(self, order: WorkingOrder, now: float) -> SweepAction:
        """One order, one named action. ``NONE`` means leave it alone."""
        if order.terminal:
            return SweepAction(SweepKind.NONE, order.client_order_id, "terminal",
                               order.venue_order_id)
        if not order.working:
            return SweepAction(SweepKind.NONE, order.client_order_id, "not_working",
                               order.venue_order_id)
        if not self.stale(order, now):
            return SweepAction(SweepKind.NONE, order.client_order_id, "fresh",
                               order.venue_order_id)
        if order.is_exit:
            return SweepAction(SweepKind.PAGE, order.client_order_id, EXIT_UNFILLED_PAGE,
                               order.venue_order_id)
        if self.cancel_stale:
            return SweepAction(SweepKind.CANCEL, order.client_order_id, STALE_ENTRY,
                               order.venue_order_id)
        return SweepAction(SweepKind.PAGE, order.client_order_id, STALE_ENTRY,
                           order.venue_order_id)

    def sweep(self, orders: Iterable[WorkingOrder], now: float) -> list[SweepAction]:
        """The actionable subset, in the order the book supplied them."""
        moment = _number(now, BAD_TIMESTAMP, "now")
        actions: list[SweepAction] = []
        for order in orders:
            action = self.evaluate(order, moment)
            if action.kind is not SweepKind.NONE:
                actions.append(action)
        return actions


def verify_orders_file(path: str | os.PathLike[str]) -> list[str]:
    """Check an order log for the defects that would corrupt a replay.

    Returns human-readable problems; empty means the log is trustworthy. This is the
    read-only companion to ``OrderRegistry.load`` (which refuses outright) so a CLI
    can print every defect instead of dying on the first.
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read orders log {target}: {exc}"]
    problems: list[str] = []
    previous_seq = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row: object = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"line {line_no}: unreadable row ({exc})")
            continue
        if not isinstance(row, Mapping):
            problems.append(f"line {line_no}: row is {type(row).__name__}, not an object")
            continue
        version = row.get("v")
        if version != SCHEMA_VERSION:
            problems.append(
                f"line {line_no}: schema version {version!r}; this build reads v{SCHEMA_VERSION}")
        seq = row.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            problems.append(f"line {line_no}: seq {seq!r} is not an integer")
        else:
            if seq != previous_seq + 1:
                problems.append(
                    f"line {line_no}: seq {seq} after {previous_seq} (expected {previous_seq + 1})")
            previous_seq = seq
        try:
            WorkingOrder.from_dict(row)
        except OrderError as exc:
            problems.append(f"line {line_no}: {exc}")
    return problems


@dataclass
class OrderRegistry:
    """Append-only order state, one complete JSON line per transition.

    Constructing with ``path=None`` is an in-memory registry (tests, paper runs).
    ``load`` replays an existing log; a torn final line is counted and ignored
    because a crash can only tear the last append, while corruption anywhere earlier
    raises: accepting it would silently drop the middle of an order's life.
    """

    path: Path | None = None
    fsync: bool = False
    orders: dict[str, WorkingOrder] = field(default_factory=dict)
    seq: int = 0
    torn_lines: int = 0
    rows: int = 0

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None,
             *, fsync: bool = False) -> OrderRegistry:
        """Replay ``path`` (default ``live/orders.jsonl``) into a registry."""
        target = Path(path) if path is not None else DEFAULT_PATH
        registry = cls(path=target, fsync=fsync)
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return registry
        except OSError as exc:
            raise OrderRegistryError(
                REGISTRY_IO, f"cannot read orders log {target}: {exc}") from None
        raw_rows = [(number, line.strip())
                    for number, line in enumerate(text.splitlines(), start=1) if line.strip()]
        for index, (number, line) in enumerate(raw_rows):
            try:
                row: object = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(raw_rows) - 1:
                    registry.torn_lines += 1      # a crash mid-append; named, not hidden
                    break
                raise OrderRegistryError(
                    REGISTRY_CORRUPT, f"orders log {target}:{number} is corrupt: {exc}") from None
            if not isinstance(row, Mapping):
                raise OrderRegistryError(
                    REGISTRY_CORRUPT, f"orders log {target}:{number} is not an object")
            version = row.get("v")
            if version != SCHEMA_VERSION:
                raise OrderRegistryError(
                    REGISTRY_SCHEMA,
                    f"orders log {target}:{number} has schema version {version!r}; "
                    f"this build reads v{SCHEMA_VERSION}")
            seq = row.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int):
                raise OrderRegistryError(
                    REGISTRY_CORRUPT, f"orders log {target}:{number} has no integer seq")
            if seq != registry.seq + 1:
                raise OrderRegistryError(
                    REGISTRY_SEQUENCE,
                    f"orders log {target}:{number}: seq {seq} after {registry.seq} "
                    f"(expected {registry.seq + 1}): a gap is a lost transition")
            try:
                order = WorkingOrder.from_dict(row)
            except OrderError as exc:
                raise OrderRegistryError(
                    REGISTRY_CORRUPT, f"orders log {target}:{number}: {exc}") from None
            registry.orders[order.client_order_id] = order
            registry.seq = seq
            registry.rows += 1
        return registry

    # ── writing ──────────────────────────────────────────────────────────────
    def append(self, order: WorkingOrder, *, event: str, ts: float) -> int:
        """Append one order snapshot under a contiguous sequence number."""
        if not isinstance(order, WorkingOrder):
            raise OrderError(BAD_ORDER_ROW, "append needs a WorkingOrder")
        if not isinstance(event, str) or not event.strip():
            raise OrderError(BAD_EVENT, "an order row needs an event name")
        moment = _number(ts, BAD_TIMESTAMP, "order row ts")
        next_seq = self.seq + 1
        row: dict[str, Any] = {
            **order.to_dict(),
            "v": SCHEMA_VERSION,
            "seq": next_seq,
            "event": event.strip(),
            "recorded_at": moment,
        }
        if self.path is not None:
            path = self.path
            line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    if self.fsync:
                        os.fsync(handle.fileno())
            except OSError as exc:
                raise OrderRegistryError(
                    REGISTRY_IO, f"cannot append to orders log {path}: {exc}") from None
        self.orders[order.client_order_id] = order
        self.seq = next_seq
        self.rows += 1
        return next_seq

    # ── reading ──────────────────────────────────────────────────────────────
    def get(self, client_order_id: str) -> WorkingOrder | None:
        return self.orders.get(client_order_id)

    def open_orders(self) -> tuple[WorkingOrder, ...]:
        """Every order that is not terminal. ``UNKNOWN`` included: it may be live."""
        return tuple(order for order in self.orders.values() if order.active)

    def working_orders(self) -> tuple[WorkingOrder, ...]:
        return tuple(order for order in self.orders.values() if order.working)

    def problems(self) -> list[str]:
        """Structural defects in the on-disk log; empty means replayable."""
        return verify_orders_file(self.path) if self.path is not None else []

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {state.value: 0 for state in OrderState}
        for order in self.orders.values():
            counts[order.state.value] += 1
        return counts


class OrderLifecycle:
    """The session's working orders: pure transitions, recorded, and swept.

    One lifecycle owns a map of ``client_order_id`` -> ``WorkingOrder``. Event
    methods apply ``apply_event`` and append the result to the registry, so the
    in-memory book cannot diverge from the durable one through this class. The
    registry is optional (in-memory tests, paper runs), but when a path is
    configured it is the source of truth after a restart.
    """

    def __init__(self, *, registry: OrderRegistry | None = None,
                 guard: StaleLimitGuard | None = None,
                 cancel_order: Callable[[str], object] | None = None) -> None:
        self.registry = registry
        self.guard = guard
        self.cancel_order = cancel_order
        self._orders: dict[str, WorkingOrder] = (
            dict(registry.orders) if registry is not None else {})

    @property
    def orders(self) -> tuple[WorkingOrder, ...]:
        return tuple(self._orders.values())

    def get(self, client_order_id: str) -> WorkingOrder | None:
        return self._orders.get(client_order_id)

    def open_orders(self) -> tuple[WorkingOrder, ...]:
        return tuple(order for order in self._orders.values() if order.active)

    # ── tracking ─────────────────────────────────────────────────────────────
    def track(self, order: WorkingOrder, *, ts: float) -> WorkingOrder:
        """Register a freshly sent order exactly once."""
        if not isinstance(order, WorkingOrder):
            raise OrderError(BAD_ORDER_ROW, "track needs a WorkingOrder")
        if order.client_order_id in self._orders:
            raise OrderError(
                "duplicate_client_order_id",
                f"{order.client_order_id} is already tracked; an order is not re-sent")
        self._record(order, event="track", ts=ts)
        return order

    def apply(self, client_order_id: str, event: str, *, ts: float,
              venue_order_id: str = "", filled_qty: int | None = None,
              limit: float | None = None, qty: int | None = None,
              reason: str = "") -> WorkingOrder:
        """Apply one event and record the result."""
        current = self._orders.get(client_order_id)
        if current is None:
            raise OrderError("unknown_order", f"{client_order_id} is not tracked")
        updated = apply_event(current, event, ts=ts, venue_order_id=venue_order_id,
                              filled_qty=filled_qty, limit=limit, qty=qty, reason=reason)
        self._record(updated, event=event, ts=ts)
        return updated

    # Convenience wrappers: the names the caller's narrative uses. Each one is the
    # same transition as `apply(order_id, "<event>", ...)` and fails the same way.
    def ack(self, client_order_id: str, venue_order_id: str, *, ts: float,
            reason: str = "") -> WorkingOrder:
        return self.apply(client_order_id, "ack", ts=ts, venue_order_id=venue_order_id,
                          reason=reason)

    def partial(self, client_order_id: str, filled_qty: int, *, ts: float,
                reason: str = "") -> WorkingOrder:
        return self.apply(client_order_id, "partial", ts=ts, filled_qty=filled_qty,
                          reason=reason)

    def fill(self, client_order_id: str, *, ts: float, filled_qty: int | None = None,
             reason: str = "") -> WorkingOrder:
        return self.apply(client_order_id, "fill", ts=ts, filled_qty=filled_qty,
                          reason=reason)

    def cancel_requested(self, client_order_id: str, *, ts: float,
                         reason: str = "") -> WorkingOrder:
        return self.apply(client_order_id, "cancel_requested", ts=ts, reason=reason)

    def cancel_confirmed(self, client_order_id: str, *, ts: float,
                         reason: str = "") -> WorkingOrder:
        return self.apply(client_order_id, "cancel_confirmed", ts=ts, reason=reason)

    def replace_requested(self, client_order_id: str, *, ts: float,
                          limit: float | None = None, qty: int | None = None,
                          reason: str = "") -> WorkingOrder:
        return self.apply(client_order_id, "replace_requested", ts=ts, limit=limit,
                          qty=qty, reason=reason)

    def replace_confirmed(self, client_order_id: str, *, ts: float,
                          reason: str = "") -> WorkingOrder:
        return self.apply(client_order_id, "replace_confirmed", ts=ts, reason=reason)

    def reject(self, client_order_id: str, *, ts: float, reason: str = "") -> WorkingOrder:
        return self.apply(client_order_id, "reject", ts=ts, reason=reason)

    def expire(self, client_order_id: str, *, ts: float, reason: str = "") -> WorkingOrder:
        return self.apply(client_order_id, "expire", ts=ts, reason=reason)

    def timeout(self, client_order_id: str, *, ts: float, reason: str = "") -> WorkingOrder:
        return self.apply(client_order_id, "timeout", ts=ts, reason=reason)

    # ── the Listener hook ────────────────────────────────────────────────────
    def sweep(self, now: float) -> list[SweepAction]:
        """Return the actions a timer must take for this session's orders.

        A stale entry with an auto-cancel policy records ``cancel_requested`` first,
        then asks the broker. If the broker refuses or the transport dies, the action
        becomes a ``cancel_failed`` page: the request is durable, the confirmation is
        not, and a human decides. A stale exit is never cancelled here - it is paged.
        """
        if self.guard is None:
            return []
        moment = _number(now, BAD_TIMESTAMP, "now")
        actions: list[SweepAction] = []
        for action in self.guard.sweep(tuple(self._orders.values()), moment):
            if action.kind is SweepKind.CANCEL:
                action = self._attempt_cancel(action, moment)
            actions.append(action)
        return actions

    def _attempt_cancel(self, action: SweepAction, now: float) -> SweepAction:
        if self.cancel_order is None or not action.venue_order_id:
            return replace(action, kind=SweepKind.PAGE, reason=CANCEL_UNAVAILABLE)
        try:
            self.apply(action.client_order_id, "cancel_requested", ts=now, reason=STALE_ENTRY)
        except OrderError as exc:
            return replace(action, kind=SweepKind.PAGE,
                           reason=f"{CANCEL_FAILED}:{exc.reason}")
        try:
            self.cancel_order(action.venue_order_id)
        except Exception as exc:  # a venue refusal is an action, not a crash
            detail = str(exc).strip()[:160] or type(exc).__name__
            return replace(action, kind=SweepKind.PAGE, reason=f"{CANCEL_FAILED}:{detail}")
        return action

    # ── persistence ──────────────────────────────────────────────────────────
    def _record(self, order: WorkingOrder, *, event: str, ts: float) -> None:
        # Record before the in-memory view moves: a failed append must not leave the
        # process believing an order is in a state the durable log never saw.
        if self.registry is not None:
            self.registry.append(order, event=event, ts=ts)
        self._orders[order.client_order_id] = order


# ── operator rendering (the CLI wires these; no formatting logic in cli.py) ──
def render_order(order: WorkingOrder, *, now: float | None = None) -> str:
    """One line an operator can read without opening the JSONL."""
    venue = f" venue={order.venue_order_id}" if order.venue_order_id else ""
    age = f" age={order.age(now):.0f}s" if now is not None else ""
    pending = ""
    if order.pending_limit is not None or order.pending_qty is not None:
        pending = (f" pending={order.pending_limit if order.pending_limit is not None else '-'}"
                   f"x{order.pending_qty if order.pending_qty is not None else '-'}")
    reason = f" reason={order.reason}" if order.reason else ""
    return (f"{order.client_order_id} {order.contract} {order.side} {order.qty}@"
            f"{order.limit:g} {order.tif.value} state={order.state.value} "
            f"filled={order.filled_qty}/{order.qty}{venue}{age}{pending}{reason}")


def render_registry(registry: OrderRegistry, *, now: float | None = None) -> list[str]:
    """The `orders show` block: counts first, then one line per order."""
    summary = registry.summary()
    interesting = ", ".join(f"{name}={count}" for name, count in summary.items() if count)
    lines = [
        f"orders: {len(registry.orders)} (seq {registry.seq}, rows {registry.rows}"
        f"{', torn ' + str(registry.torn_lines) if registry.torn_lines else ''})",
        f"states: {interesting or 'none'}",
    ]
    for order in registry.orders.values():
        lines.append("  " + render_order(order, now=now))
    return lines


def render_registry_verify(registry: OrderRegistry) -> list[str]:
    """The `orders verify` block: OK, or every named defect in the log."""
    problems = registry.problems()
    if not problems:
        return [f"orders: OK ({len(registry.orders)} orders, seq {registry.seq}"
                f"{', one torn final append ignored' if registry.torn_lines else ''})"]
    return [f"orders: {len(problems)} problem(s)", *(f"  {problem}" for problem in problems)]
