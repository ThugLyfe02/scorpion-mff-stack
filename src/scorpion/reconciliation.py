"""Reconciliation: does the book believe in positions the venue does not have?

Every order this control plane sends is a proposal on a remote system, and remote systems
disagree. A fill that never came back, a manual order placed in the app, a partial that
arrived twice, an expiry the venue settled but we did not - each of these leaves the local
book holding something that is not there, or believing it is flat while the venue holds
risk. Either direction is a live-money failure:

* **phantom local** - we think we hold it, so we refuse new entries and manage a position
  that does not exist;
* **unhedged remote** - the venue holds risk the book cannot see, so no stop, no exit, no
  risk limit applies to it at all.

So reconciliation is not a report: a drift **halts the desk** and emits a HALT effect with
the specific mismatch. Fail closed, name the contract, let a human resolve it. A tolerance
is supported only for the one legitimate case - a fill in flight whose quantity delta is
already expected - and it must be explicit.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from .domain import BookState, Effect, EffectKind, PositionStatus
from .store import Store

LIVE_STATUSES = frozenset(
    {PositionStatus.PENDING_ENTRY, PositionStatus.OPEN, PositionStatus.CLOSING}
)
DriftKind = Literal["phantom_local", "missing_local", "quantity_mismatch"]


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """A position as the venue reports it, at a stated moment."""

    contract_key: str
    quantity: int


@dataclass(frozen=True, slots=True)
class Drift:
    contract_key: str
    kind: DriftKind
    book_quantity: int
    broker_quantity: int

    def describe(self) -> str:
        return (
            f"{self.kind}:{self.contract_key}:book={self.book_quantity},"
            f"broker={self.broker_quantity}"
        )


@dataclass(frozen=True, slots=True)
class Reconciliation:
    drifts: tuple[Drift, ...] = ()
    requires_halt: bool = False

    @property
    def clean(self) -> bool:
        return not self.drifts

    @property
    def reason(self) -> str:
        if self.clean:
            return "reconciled"
        return "reconciliation_drift:" + ",".join(d.describe() for d in self.drifts[:5])

    def to_dict(self) -> dict[str, object]:
        return {
            "clean": self.clean,
            "requires_halt": self.requires_halt,
            "drifts": [
                {"contract_key": d.contract_key, "kind": d.kind,
                 "book_quantity": d.book_quantity, "broker_quantity": d.broker_quantity}
                for d in self.drifts
            ],
        }


def reconcile(
    state: BookState,
    broker_positions: Iterable[BrokerPosition],
    *,
    tolerance: dict[str, int] | None = None,
) -> Reconciliation:
    """Compare the local book with the venue, in both directions.

    `tolerance` maps a contract key to a quantity delta considered explained (an in-flight
    fill). It is deliberately per-contract and explicit: a global tolerance is how a real
    drift hides.
    """
    broker: dict[str, int] = {}
    for broker_position in broker_positions:
        if broker_position.quantity < 0:
            raise ValueError(
                f"broker quantity must not be negative: {broker_position.contract_key}"
            )
        broker[broker_position.contract_key] = (
            broker.get(broker_position.contract_key, 0) + broker_position.quantity
        )

    allowed = tolerance or {}
    drifts: list[Drift] = []

    for key, position in sorted(state.positions.items()):
        if position.status not in LIVE_STATUSES:
            continue
        held = position.quantity
        seen = broker.get(key, 0)
        if held == seen:
            continue
        if abs(held - seen) <= allowed.get(key, 0):
            continue
        kind: DriftKind = (
            "phantom_local" if seen == 0 else "quantity_mismatch"
        )
        drifts.append(Drift(key, kind, held, seen))

    for key, seen in sorted(broker.items()):
        if seen <= 0:
            continue
        local = state.positions.get(key)
        if local is not None and local.status in LIVE_STATUSES:
            continue
        drifts.append(Drift(key, "missing_local", 0, seen))

    return Reconciliation(drifts=tuple(drifts), requires_halt=bool(drifts))


def reconcile_and_halt(
    state: BookState, broker_positions: Iterable[BrokerPosition]
) -> tuple[BookState, tuple[Effect, ...], Reconciliation]:
    """Reconcile, and halt the book if it drifted.

    The halt goes through the same `Effect` path as an operator stop, so it is durable,
    replayable, and visible in the outbox rather than being a log line nobody reads.
    """
    result = reconcile(state, broker_positions)
    if result.clean:
        return state, (), result
    halted = BookState(
        positions=state.positions,
        halted=True,
        seen_event_ids=state.seen_event_ids,
        first_entry_proposed_on=state.first_entry_proposed_on,
        risk=state.risk,
    )
    effect = Effect(
        kind=EffectKind.HALT,
        contract_key=None,
        source_event_id=f"reconcile:{result.drifts[0].describe()}",
        generation=0,
        reason=result.reason,
    )
    return halted, (effect,), result


# ── resolving ambiguous sends ────────────────────────────────────────────────
# An UNKNOWN row is an order we do not know the fate of. Only the venue can say, and it can
# say three things. Collapsing "I cannot tell you" into "not sent" is how one order becomes
# two, so the tri-state is the type system's job, not a caller's good intentions.

LookupStatus = Literal["seen", "not_seen", "unknown"]


@dataclass(frozen=True, slots=True)
class LookupResult:
    status: LookupStatus
    broker_ref: str = ""

    def __post_init__(self) -> None:
        if self.status not in ("seen", "not_seen", "unknown"):
            raise ValueError(f"bad lookup status {self.status!r}")


class OrderLookup(Protocol):
    """Answers whether the venue holds an order for a given idempotency key."""

    def find(self, idempotency_key: str) -> LookupResult: ...


class NoLookup:
    """The honest default when no venue API is wired: every row stays UNKNOWN."""

    def find(self, idempotency_key: str) -> LookupResult:
        return LookupResult("unknown")


@dataclass(frozen=True, slots=True)
class UnknownResolution:
    effect_id: int
    contract_key: str | None
    idempotency_key: str
    status: str          # SENT | RETRY | UNKNOWN
    broker_ref: str = ""


@dataclass(frozen=True, slots=True)
class UnknownReport:
    resolutions: tuple[UnknownResolution, ...] = ()

    @property
    def sent(self) -> int:
        return sum(1 for r in self.resolutions if r.status == "SENT")

    @property
    def retried(self) -> int:
        return sum(1 for r in self.resolutions if r.status == "RETRY")

    @property
    def pending(self) -> int:
        return sum(1 for r in self.resolutions if r.status == "UNKNOWN")

    @property
    def clean(self) -> bool:
        return self.pending == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "sent": self.sent, "retried": self.retried, "pending": self.pending,
            "resolutions": [
                {"effect_id": r.effect_id, "contract_key": r.contract_key,
                 "status": r.status, "broker_ref": r.broker_ref}
                for r in self.resolutions
            ],
        }


def reconcile_unknown(
    *, store: Store, lookup: OrderLookup, now: datetime | None = None
) -> UnknownReport:
    """Resolve every UNKNOWN send against the venue, and leave the unknowable alone."""
    moment = now or datetime.now(UTC)
    resolutions: list[UnknownResolution] = []
    for row in store.load_unknown():
        effect_id = int(str(row["effect_id"]))
        contract_key = str(row["contract_key"]) if row["contract_key"] else None
        # Prefer the key recorded at send time: re-deriving it would duplicate the
        # authorization format in a second place, and a format change would silently stop
        # matching orders the venue already holds.
        recorded = str(row.get("idempotency_key") or "")
        key = recorded or (
            f"{row['source_event_id']}:{contract_key}:{row['generation']}:{row['kind']}"
        )
        answer = lookup.find(key)
        if answer.status == "seen":
            status = store.resolve_unknown(
                effect_id=effect_id, sent=True, broker_ref=answer.broker_ref, now=moment
            )
        elif answer.status == "not_seen":
            status = store.resolve_unknown(
                effect_id=effect_id, sent=False, broker_ref="", now=moment
            )
        else:
            status = "UNKNOWN"          # the venue cannot say: the row keeps waiting
        resolutions.append(
            UnknownResolution(effect_id, contract_key, key, status, answer.broker_ref)
        )
    return UnknownReport(resolutions=tuple(resolutions))
