"""Replay verification: rebuild the book from its own log and compare, field by field.

An event-sourced system claims a strong property - the state is a pure function of the log -
and almost none of them check it. This does, and it is the drift alarm for the whole design:

    replay every applied decision in sequence order, then diff the result against the live
    book. Any divergence halts the desk and names the field.

The applied log (`applied_events`) is what makes this exact rather than approximate. Signals
alone cannot rebuild a book, because **fills are not signals**: quantity, average cost and
realised P&L come from executions. Before this log existed, a replay would reconstruct
positions with zero quantity and nobody would notice. Fills are therefore recorded as applied
decisions too, in the same sequence.

What is compared, and why each one matters:

    halted              a restart that forgot a stop trades through it
    position set        a missing or extra contract is an unmanaged position
    status/generation   a stale generation is how a client answers the wrong message
    quantity/avg price  the book's idea of size and cost basis
    risk ledger         realised today, equity, peak, streak, stand-down reason

A verifier that cannot fail is decoration, so the divergent-state test constructs a real
divergence and asserts the halt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .domain import BookState, EventKind, SignalEvent
from .reducer import reduce_book
from .settlement import settle_fill
from .store import Store


@dataclass(frozen=True, slots=True)
class Divergence:
    field: str
    live: str
    replayed: str

    def describe(self) -> str:
        return f"{self.field}: live={self.live} replayed={self.replayed}"


@dataclass(frozen=True, slots=True)
class Verification:
    ok: bool
    divergences: tuple[Divergence, ...] = ()
    applied_seq: int = 0
    positions: int = 0
    signals: int = 0
    fills: int = 0

    @property
    def requires_halt(self) -> bool:
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "applied_seq": self.applied_seq,
            "positions": self.positions,
            "signals": self.signals,
            "fills": self.fills,
            "divergences": [
                {"field": d.field, "live": d.live, "replayed": d.replayed}
                for d in self.divergences
            ],
        }


def _signal_from_payload(payload_json: str) -> SignalEvent:
    payload = json.loads(payload_json)
    payload["kind"] = EventKind(payload["kind"])
    payload["source_ts_utc"] = datetime.fromisoformat(payload["source_ts_utc"])
    payload["received_ts_utc"] = datetime.fromisoformat(payload["received_ts_utc"])
    if payload.get("expiry"):
        payload["expiry"] = date.fromisoformat(payload["expiry"])
    for key in ("strike", "referenced_price", "referenced_pct"):
        if payload.get(key) is not None:
            payload[key] = Decimal(payload[key])
    payload.pop("contract_key", None)       # audit field, re-derived by the event itself
    return SignalEvent(**payload)


def apply_applied(state: BookState, row: dict[str, object]) -> BookState:
    """Apply one recorded decision to an existing book.

    Exposed separately from `replay_applied` so a snapshot can be resumed mid-log without
    rebuilding from the beginning - which is what makes compaction possible at all.
    """
    kind = str(row["kind"])
    payload_json = str(row["payload_json"])
    if kind == "signal":
        event = _signal_from_payload(payload_json)
        next_state, _ = reduce_book(state, event)
        return next_state
    if kind == "fill":
        payload = json.loads(payload_json)
        settlement = settle_fill(
            state=state,
            contract_key=str(payload["contract_key"]),
            generation=int(payload["generation"]),
            quantity_delta=int(payload["quantity_delta"]),
            fill_price=Decimal(str(payload["fill_price"])),
            final=bool(payload.get("final", False)),
        )
        from dataclasses import replace

        next_state = settlement.state
        if settlement.realized or settlement.closed:
            next_state = replace(
                next_state,
                risk=next_state.risk.settled(
                    settlement.realized, date.fromisoformat(str(payload["market_day"]))
                ),
            )
        return next_state
    raise ValueError(f"unknown applied kind: {kind}")


def replay_applied(
    rows: list[dict[str, object]], *, collect: list[str] | None = None
) -> BookState:
    """Rebuild the book by applying every recorded decision in sequence order.

    `collect` trades a hard failure for a list of recorded problems. A verifier that raises on
    the first bad row tells you less than one that lists every divergence in the log - and the
    library path (no `collect`) still fails loudly, because applying an impossible fill to a
    real book should never pass silently.
    """
    from .settlement import SettlementError

    state = BookState()
    for row in rows:
        try:
            state = apply_applied(state, row)
        except SettlementError as exc:
            if collect is None:
                raise
            collect.append(f"seq {row.get('seq')}: {exc}")
    return state


def _diff(live: BookState, replayed: BookState) -> list[Divergence]:
    out: list[Divergence] = []
    if live.halted != replayed.halted:
        out.append(Divergence("halted", str(live.halted), str(replayed.halted)))

    live_keys = set(live.positions)
    replayed_keys = set(replayed.positions)
    for key in sorted(live_keys - replayed_keys):
        out.append(Divergence(f"position:{key}", "present", "missing"))
    for key in sorted(replayed_keys - live_keys):
        out.append(Divergence(f"position:{key}", "missing", "present"))

    for key in sorted(live_keys & replayed_keys):
        a, b = live.positions[key], replayed.positions[key]
        for field in ("status", "generation", "quantity", "average_price", "added_once"):
            left, right = getattr(a, field), getattr(b, field)
            if left != right:
                out.append(Divergence(f"{key}.{field}", str(left), str(right)))

    for field in ("market_day", "realized_today", "equity", "peak_equity",
                  "consecutive_losses", "halted_reason"):
        left, right = getattr(live.risk, field), getattr(replayed.risk, field)
        if left != right:
            out.append(Divergence(f"risk.{field}", str(left), str(right)))
    return out


def verify(store: Store, live: BookState) -> Verification:
    """Replay the applied log and compare it with the live book."""
    rows = store.load_applied()
    problems: list[str] = []
    replayed = replay_applied(rows, collect=problems)
    counts = store.applied_counts()
    divergences = tuple(_diff(live, replayed)) + tuple(
        Divergence(f"log:{problem}", "applied", "not applicable") for problem in problems[:10]
    )
    return Verification(
        ok=not divergences,
        divergences=divergences,
        applied_seq=len(rows),
        positions=len(live.positions),
        signals=counts.get("signal", 0),
        fills=counts.get("fill", 0),
    )


def verify_log(store: Store) -> Verification:
    """Log integrity for a process that has no live book (a cold start, or the CLI).

    Comparing a signals-only reconstruction against the applied reconstruction is not
    verification - they are two different rebuilds. What CAN be checked from storage alone:

      * every persisted signal was applied, and exactly once (no silent drops);
      * the applied log has no gaps in its sequence;
      * every applied fill names a contract the log has already seen.

    A cold process then replays `applied_events` for its state; this function is what tells
    it the log it is trusting is complete.
    """
    applied = store.load_applied()
    divergences: list[Divergence] = []

    applied_signal_ids: list[str] = []
    for row in applied:
        if str(row["kind"]) != "signal":
            continue
        payload = json.loads(str(row["payload_json"]))
        applied_signal_ids.append(str(payload["event_id"]))

    stored_signal_ids = [event.event_id for event in store.load_signals()]
    snapshot = store.latest_snapshot()
    covered = int(str(snapshot.get("signals_covered") or 0)) if snapshot else 0

    if covered:
        # After compaction the covered rows are gone by design, so identity cannot be
        # checked - only counts. Pretending otherwise flagged every healthy truncated store
        # as corrupt.
        if len(applied_signal_ids) + covered != len(stored_signal_ids):
            divergences.append(
                Divergence(
                    "log:signal_count",
                    f"{len(applied_signal_ids)} in log + {covered} in snapshot",
                    f"{len(stored_signal_ids)} stored",
                )
            )
    else:
        missing = [event_id for event_id in stored_signal_ids
                   if event_id not in set(applied_signal_ids)]
        for event_id in missing[:5]:
            divergences.append(
                Divergence(f"log:signal_not_applied:{event_id}", "in signals", "not in log")
            )

    # The indexed column and the payload's own derivation must agree: when they do not, a fill
    # can name a position the signal stream never created - which is what a 47-position
    # divergence at corpus scale looked like from the outside.
    for row in applied:
        if str(row["kind"]) != "signal":
            continue
        payload = json.loads(str(row["payload_json"]))
        stated = payload.get("contract_key")
        rebuilt = _signal_from_payload(str(row["payload_json"])).contract_key
        if stated is not None and stated != rebuilt:
            divergences.append(
                Divergence(f"log:contract_key_mismatch:{stated}", str(stated), str(rebuilt))
            )

    sequences = [int(str(row["seq"])) for row in applied]
    if sequences and sequences != list(range(sequences[0], sequences[0] + len(sequences))):
        divergences.append(Divergence("log:sequence_gap", "contiguous", "gapped"))

    seen_contracts: set[str] = set()
    for row in applied:
        key = str(row["contract_key"] or "")
        if str(row["kind"]) == "signal" and key:
            seen_contracts.add(key)
        if str(row["kind"]) == "fill" and key and key not in seen_contracts:
            divergences.append(
                Divergence(f"log:fill_before_signal:{key}", "fill", "no prior signal")
            )

    counts = store.applied_counts()
    return Verification(
        ok=not divergences,
        divergences=tuple(divergences),
        applied_seq=len(applied),
        positions=0,
        signals=counts.get("signal", 0),
        fills=counts.get("fill", 0),
    )


def verify_and_halt(store: Store, live: BookState) -> Verification:
    """Verify, and make a divergence durable: a drifted book must stop trading."""
    result = verify(store, live)
    if not result.ok:
        store.set_halt(
            True,
            "replay_divergence:" + ",".join(d.describe() for d in result.divergences[:3]),
        )
    return result
