"""Venue reconciliation: the book in the engine and the book at the venue.

The engine's positions are a decision cache. The venue holds the actual contracts. A
restart, a rejected buy, a partial fill, a manual trade, or a fill the listener never
saw can make the two disagree, and nothing in the hot path compared them. The result
is the quiet failure this package exists to remove: the bot believes it is flat while
the venue still holds risk, or believes it holds a contract it can no longer sell.

This module is the comparator, not the feed. It takes two snapshots and names the
drift in three directions:

* **phantom_local** - the engine holds it, the venue does not. Any future sell is a
  naked short or a rejected order; the local P&L is fiction.
* **missing_local** - the venue holds it, the engine does not. The position is
  invisible to the exit path, the governor and the marks.
* **quantity_mismatch** - both hold it, at different counts. A partial fill recorded as
  a full clip is the common cause.

A snapshot is evidence, so a malformed one is refused rather than repaired: duplicate
contract keys and negative counts raise instead of being summed or clamped. The report
is a pure function of (local book, venue snapshot), so every drift can be replayed.

`Engine.reconcile_positions` consumes the report: clean writes one row, drift halts
**new buys** and pages, and exits stay legal. A desk that cannot reconcile must be able
to leave a position it is in.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import Position


class ReconcileError(ValueError):
    """A snapshot that cannot be used as evidence. Refuse it; do not repair it."""


@dataclass(frozen=True)
class VenuePosition:
    """One line of the venue's book. The contract key is the join, not the ticker."""

    contract_key: str
    contracts: int

    def __post_init__(self) -> None:
        if not self.contract_key:
            raise ReconcileError("venue position without a contract_key")
        if isinstance(self.contracts, bool) or not isinstance(self.contracts, int):
            raise ReconcileError(
                f"{self.contract_key}: contracts must be an integer, "
                f"got {type(self.contracts).__name__}")
        if self.contracts < 0:
            raise ReconcileError(
                f"{self.contract_key}: negative contracts {self.contracts}")

    def to_dict(self) -> dict[str, Any]:
        return {"contract_key": self.contract_key, "contracts": self.contracts}


@dataclass(frozen=True)
class Drift:
    """One named disagreement between the two books."""

    kind: str                      # phantom_local | missing_local | quantity_mismatch
    contract_key: str
    local: int
    venue: int

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "contract_key": self.contract_key,
                "local": self.local, "venue": self.venue}


@dataclass(frozen=True)
class ReconcileReport:
    drifts: tuple[Drift, ...]
    local_positions: int
    venue_positions: int
    local_contracts: int
    venue_contracts: int

    @property
    def clean(self) -> bool:
        return not self.drifts

    @property
    def reason_text(self) -> str:
        return "clean" if self.clean else "+".join(
            f"{drift.kind}:{drift.contract_key}:{drift.local}/{drift.venue}"
            for drift in self.drifts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "drifts": [drift.to_dict() for drift in self.drifts],
            "local_positions": self.local_positions,
            "venue_positions": self.venue_positions,
            "local_contracts": self.local_contracts,
            "venue_contracts": self.venue_contracts,
            "reason": self.reason_text,
        }


def reconcile(local: Iterable[Position], venue: Iterable[VenuePosition]) -> ReconcileReport:
    """Compare the open local book with the venue snapshot, naming every drift.

    Only open local positions participate: a zeroed position is not a holding, and
    requiring the venue to list it would turn a routine close into drift. The venue
    snapshot is validated first; a duplicate key is two answers to one question.
    """
    local_book = {position.contract_key: position.contracts
                  for position in local if position.is_open}
    venue_book: dict[str, int] = {}
    for item in venue:
        if item.contract_key in venue_book:
            raise ReconcileError(
                f"duplicate venue contract {item.contract_key!r}: "
                f"{venue_book[item.contract_key]} and {item.contracts}")
        venue_book[item.contract_key] = item.contracts

    drifts: list[Drift] = []
    for key in sorted(local_book.keys() | venue_book.keys()):
        held = local_book.get(key, 0)
        at_venue = venue_book.get(key, 0)
        if held == at_venue:
            continue
        if at_venue == 0:
            kind = "phantom_local"
        elif held == 0:
            kind = "missing_local"
        else:
            kind = "quantity_mismatch"
        drifts.append(Drift(kind=kind, contract_key=key, local=held, venue=at_venue))

    return ReconcileReport(
        drifts=tuple(drifts),
        local_positions=len(local_book),
        venue_positions=len(venue_book),
        local_contracts=sum(local_book.values()),
        venue_contracts=sum(venue_book.values()),
    )


def snapshot_from_payload(payload: object) -> list[VenuePosition]:
    """Parse a JSON snapshot: an array of objects with contract_key and contracts.

    Names are accepted in the two spellings the operator surfaces already use
    (`contract_key`/`key`, `contracts`/`qty`). Anything else is refused with the
    index that failed, so a typo cannot silently become an empty book.
    """
    if not isinstance(payload, list):
        raise ReconcileError("venue snapshot must be a JSON array")
    positions: list[VenuePosition] = []
    for index, row in enumerate(payload):
        if not isinstance(row, Mapping):
            raise ReconcileError(f"snapshot[{index}]: expected an object")
        key = row.get("contract_key", row.get("key"))
        contracts = row.get("contracts", row.get("qty"))
        if not isinstance(key, str) or not key:
            raise ReconcileError(f"snapshot[{index}]: missing contract_key")
        if isinstance(contracts, bool) or not isinstance(contracts, int):
            raise ReconcileError(f"snapshot[{index}]: contracts must be an integer")
        positions.append(VenuePosition(contract_key=key, contracts=contracts))
    return positions


def load_snapshot(path: str | Path) -> list[VenuePosition]:
    """Read a venue snapshot. Missing/unreadable is an error, never 'flat'."""
    target = Path(path)
    try:
        raw: object = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReconcileError(f"venue snapshot not found: {target}") from None
    except OSError as exc:
        raise ReconcileError(f"cannot read venue snapshot {target}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ReconcileError(f"venue snapshot {target} is not JSON: {exc}") from None
    return snapshot_from_payload(raw)
