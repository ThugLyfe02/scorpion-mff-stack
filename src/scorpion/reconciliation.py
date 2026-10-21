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
from typing import Literal

from .domain import BookState, Effect, EffectKind, PositionStatus

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
