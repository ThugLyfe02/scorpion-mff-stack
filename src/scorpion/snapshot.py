"""State snapshots and log compaction.

An append-only applied log is a liability the moment it is longer than the replay budget: a
restart that has to fold months of decisions is a restart that fails at the worst time. The
standard answer is a snapshot plus truncation, and the standard way to get it wrong is to
truncate first and discover the snapshot was lossy later.

So the order here is fixed and enforced:

1. serialize the current book,
2. digest it (sha256 over canonical JSON) and store it AT the sequence it covers,
3. only then delete log rows at or below that sequence,
4. and prove it: `verify_snapshot` replays snapshot+tail and compares it with a full replay.

A snapshot that fails its digest is an error, never a quietly-wrong starting point. A
snapshot whose sequence is ahead of the log is an error too - that means someone truncated a
log this snapshot does not actually cover.

Every value round-trips exactly: Decimal as text (never float), dates as ISO, and the
`seen_event_ids` set as sorted text, because duplicate suppression depends on it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .domain import BookState, PositionState, PositionStatus
from .risk import RiskState
from .store import Store
from .verify import apply_applied

SNAPSHOT_VERSION = 1


class SnapshotError(ValueError):
    """A snapshot that cannot be trusted: wrong digest, wrong version, or out of range."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    seq: int
    payload_json: str
    digest: str
    created_ts_utc: str
    version: int = SNAPSHOT_VERSION
    signals_covered: int = 0
    fills_covered: int = 0

    def to_dict(self) -> dict[str, object]:
        return {"seq": self.seq, "payload_json": self.payload_json,
                "digest": self.digest, "created_ts_utc": self.created_ts_utc,
                "version": self.version}


# ── serialization ────────────────────────────────────────────────────────────
def _position_to_dict(position: PositionState) -> dict[str, Any]:
    return {
        "contract_key": position.contract_key,
        "status": position.status.value,
        "generation": position.generation,
        "source_entry_message_id": position.source_entry_message_id,
        "last_source_ts_utc": (
            position.last_source_ts_utc.isoformat() if position.last_source_ts_utc else None
        ),
        "quantity": position.quantity,
        "average_price": str(position.average_price),
        "added_once": position.added_once,
        "last_reason": position.last_reason,
    }


def serialize_state(state: BookState) -> str:
    """Canonical JSON: sorted keys, no whitespace, Decimals and dates as text."""
    risk = state.risk
    payload = {
        "version": SNAPSHOT_VERSION,
        "halted": state.halted,
        "seen_event_ids": sorted(state.seen_event_ids),
        "first_entry_proposed_on": (
            state.first_entry_proposed_on.isoformat()
            if state.first_entry_proposed_on
            else None
        ),
        "positions": {
            key: _position_to_dict(position)
            for key, position in sorted(state.positions.items())
        },
        "risk": {
            "market_day": risk.market_day.isoformat() if risk.market_day else None,
            "realized_today": str(risk.realized_today),
            "equity": str(risk.equity),
            "peak_equity": str(risk.peak_equity),
            "consecutive_losses": risk.consecutive_losses,
            "halted_reason": risk.halted_reason,
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def deserialize_state(payload_json: str) -> BookState:
    payload = json.loads(payload_json)
    if payload.get("version") != SNAPSHOT_VERSION:
        raise SnapshotError(
            f"unsupported snapshot version {payload.get('version')!r}"
        )
    positions: dict[str, PositionState] = {}
    for key, row in (payload.get("positions") or {}).items():
        last_seen = row.get("last_source_ts_utc")
        positions[key] = PositionState(
            contract_key=str(row["contract_key"]),
            status=PositionStatus(str(row["status"])),
            generation=int(row["generation"]),
            source_entry_message_id=row.get("source_entry_message_id"),
            last_source_ts_utc=(
                datetime.fromisoformat(str(last_seen)) if last_seen else None
            ),
            quantity=int(row["quantity"]),
            average_price=Decimal(str(row["average_price"])),
            added_once=bool(row.get("added_once", False)),
            last_reason=str(row.get("last_reason", "")),
        )
    risk_row = payload.get("risk") or {}
    market_day = risk_row.get("market_day")
    risk = RiskState(
        market_day=date.fromisoformat(str(market_day)) if market_day else None,
        realized_today=Decimal(str(risk_row.get("realized_today", "0"))),
        equity=Decimal(str(risk_row.get("equity", "0"))),
        peak_equity=Decimal(str(risk_row.get("peak_equity", "0"))),
        consecutive_losses=int(risk_row.get("consecutive_losses", 0)),
        halted_reason=str(risk_row.get("halted_reason", "")),
    )
    first_entry = payload.get("first_entry_proposed_on")
    return BookState(
        positions=positions,
        halted=bool(payload.get("halted", False)),
        seen_event_ids=frozenset(str(x) for x in payload.get("seen_event_ids") or ()),
        first_entry_proposed_on=(
            date.fromisoformat(str(first_entry)) if first_entry else None
        ),
        risk=risk,
    )


def digest_of(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


# ── snapshots ────────────────────────────────────────────────────────────────
def take_snapshot(
    state: BookState, *, seq: int, now: datetime | None = None,
    signals_covered: int = 0, fills_covered: int = 0,
) -> Snapshot:
    if seq < 1:
        raise SnapshotError("a snapshot must cover at least one applied event")
    payload_json = serialize_state(state)
    return Snapshot(
        seq=seq,
        payload_json=payload_json,
        digest=digest_of(payload_json),
        created_ts_utc=(now or datetime.now()).isoformat(),
        signals_covered=signals_covered,
        fills_covered=fills_covered,
    )


def save_snapshot(store: Store, snapshot: Snapshot) -> None:
    store.append_snapshot(
        seq=snapshot.seq, payload_json=snapshot.payload_json,
        digest=snapshot.digest, created_ts_utc=snapshot.created_ts_utc,
        signals_covered=snapshot.signals_covered, fills_covered=snapshot.fills_covered,
    )


def load_snapshot(store: Store) -> Snapshot | None:
    row = store.latest_snapshot()
    if row is None:
        return None
    snapshot = Snapshot(
        seq=int(str(row["seq"])),
        payload_json=str(row["payload_json"]),
        digest=str(row["digest"]),
        created_ts_utc=str(row["created_ts_utc"]),
        signals_covered=int(str(row.get("signals_covered") or 0)),
        fills_covered=int(str(row.get("fills_covered") or 0)),
    )
    if digest_of(snapshot.payload_json) != snapshot.digest:
        raise SnapshotError(
            f"snapshot at seq {snapshot.seq} fails its digest - refusing to resume from it"
        )
    return snapshot


def state_from_snapshot(store: Store) -> tuple[BookState, int]:
    """Rebuild the book from the newest snapshot plus the log tail after it."""
    snapshot = load_snapshot(store)
    if snapshot is None:
        from .verify import replay_applied

        return replay_applied(store.load_applied()), 0
    state = deserialize_state(snapshot.payload_json)
    for row in store.load_applied(after_seq=snapshot.seq):
        state = apply_applied(state, row)
    return state, snapshot.seq


def compact(store: Store, *, keep_tail: int = 0, now: datetime | None = None) -> Snapshot:
    """Snapshot the current book, then delete the log rows it covers.

    Truncation happens only after the snapshot is durably written and read back with a
    matching digest, and `keep_tail` rows are retained so a small forensic window survives
    compaction.
    """
    rows = store.load_applied()
    if not rows:
        raise SnapshotError("nothing to compact: the applied log is empty")
    seq = int(str(rows[-1]["seq"]))
    state, _ = state_from_snapshot(store)
    counts = store.applied_counts()
    snapshot = take_snapshot(
        state, seq=seq, now=now,
        signals_covered=counts.get("signal", 0), fills_covered=counts.get("fill", 0),
    )
    save_snapshot(store, snapshot)

    verified = load_snapshot(store)          # re-read: a snapshot that cannot be read back
    if verified is None or verified.digest != snapshot.digest:
        raise SnapshotError("snapshot did not survive its own write; refusing to truncate")

    cutoff = seq - max(0, keep_tail)
    if cutoff > 0:
        store.truncate_applied(upto_seq=cutoff)
    return snapshot


def verify_snapshot(store: Store) -> tuple[bool, list[str]]:
    """Prove that snapshot+tail equals a full replay of whatever log is still present.

    Run in tests and after compaction. It cannot prove equivalence with rows that were
    already truncated, so `compact` also verifies before truncating - the two together are
    what make compaction safe.
    """
    snapshot = load_snapshot(store)
    if snapshot is None:
        return True, []
    state = deserialize_state(snapshot.payload_json)
    for row in store.load_applied(after_seq=snapshot.seq):
        state = apply_applied(state, row)

    # Only meaningful while the log is still complete: if nothing has been truncated (the
    # first surviving row is seq 1), snapshot+tail must reproduce a full replay exactly.
    remaining = store.load_applied()
    if remaining and int(str(remaining[0]["seq"])) == 1:
        from .verify import replay_applied

        if state != replay_applied(remaining):
            return False, ["snapshot+tail diverged from a full replay (seq 1..n)"]
    return True, []
