from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from .domain import BookState, Effect, PositionState, PositionStatus, SignalEvent
from .invariants import assert_valid_book
from .policy_bundle import RuntimePolicyBundle
from .reducer import reduce_book
from .replay import replay, state_fingerprint
from .store import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_fingerprint TEXT NOT NULL,
    signal_count INTEGER NOT NULL,
    last_event_id TEXT NOT NULL,
    integrity_record_hash TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state_checkpoint_policy
ON state_checkpoints(policy_fingerprint,checkpoint_id DESC);
"""


@dataclass(frozen=True, slots=True)
class StateCheckpoint:
    checkpoint_id: int
    policy_fingerprint: str
    signal_count: int
    last_event_id: str
    integrity_record_hash: str
    state_fingerprint: str
    state: BookState
    created_ts_utc: datetime


@dataclass(frozen=True, slots=True)
class CheckpointRestore:
    used_checkpoint: bool
    checkpoint_id: int | None
    tail_events: int
    state: BookState
    effects_to_rematerialize: tuple[Effect, ...]
    reason: str


def _encode_state(state: BookState) -> str:
    payload = {
        "halted": state.halted,
        "seen_event_ids": sorted(state.seen_event_ids),
        "first_entry_proposed_on": (
            state.first_entry_proposed_on.isoformat() if state.first_entry_proposed_on else None
        ),
        "positions": {
            key: {
                "contract_key": position.contract_key,
                "status": position.status.value,
                "generation": position.generation,
                "source_entry_message_id": position.source_entry_message_id,
                "source_channel_id": position.source_channel_id,
                "source_author_id": position.source_author_id,
                "last_source_ts_utc": (
                    position.last_source_ts_utc.isoformat()
                    if position.last_source_ts_utc is not None
                    else None
                ),
                "quantity": position.quantity,
                "average_price": str(position.average_price),
                "added_once": position.added_once,
                "last_reason": position.last_reason,
            }
            for key, position in sorted(state.positions.items())
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode_state(payload_json: str) -> BookState:
    payload = json.loads(payload_json)
    positions: dict[str, PositionState] = {}
    for key, row in payload.get("positions", {}).items():
        last_source = row.get("last_source_ts_utc")
        positions[str(key)] = PositionState(
            contract_key=str(row["contract_key"]),
            status=PositionStatus(str(row["status"])),
            generation=int(row["generation"]),
            source_entry_message_id=row.get("source_entry_message_id"),
            source_channel_id=row.get("source_channel_id"),
            source_author_id=row.get("source_author_id"),
            last_source_ts_utc=(
                datetime.fromisoformat(str(last_source)).astimezone(UTC)
                if last_source is not None
                else None
            ),
            quantity=int(row["quantity"]),
            average_price=Decimal(str(row["average_price"])),
            added_once=bool(row["added_once"]),
            last_reason=str(row.get("last_reason", "")),
        )
    first_entry = payload.get("first_entry_proposed_on")
    return BookState(
        positions=positions,
        halted=bool(payload.get("halted", False)),
        seen_event_ids=frozenset(str(item) for item in payload.get("seen_event_ids", [])),
        first_entry_proposed_on=(date.fromisoformat(str(first_entry)) if first_entry else None),
    )


def _integrity_hash_for_event(path: str | Path, event_id: str) -> str:
    if not event_id:
        return ""
    with sqlite3.connect(str(path)) as db:
        row = db.execute(
            "SELECT record_hash FROM integrity_ledger WHERE record_id=?",
            (event_id,),
        ).fetchone()
    return str(row[0]) if row is not None else ""


def create_state_checkpoint(
    path: str | Path,
    *,
    runtime_policy: RuntimePolicyBundle | None = None,
) -> StateCheckpoint:
    policy = runtime_policy or RuntimePolicyBundle()
    store = Store(path)
    signals = store.load_signals()
    state, _ = replay(signals, policy.base)
    assert_valid_book(state, max_open_positions=policy.base.max_open_positions)
    last_event_id = signals[-1].event_id if signals else ""
    integrity_hash = _integrity_hash_for_event(path, last_event_id)
    if signals and not integrity_hash:
        raise RuntimeError("cannot checkpoint signals not covered by the integrity ledger")
    encoded = _encode_state(state)
    fingerprint = state_fingerprint(state)
    created = datetime.now(UTC)
    with store.connect() as db:
        db.executescript(_SCHEMA)
        cursor = db.execute(
            """
            INSERT INTO state_checkpoints
            (policy_fingerprint,signal_count,last_event_id,integrity_record_hash,
             state_fingerprint,state_json,created_ts_utc)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                policy.fingerprint,
                len(signals),
                last_event_id,
                integrity_hash,
                fingerprint,
                encoded,
                created.isoformat(),
            ),
        )
        raw_checkpoint_id = cursor.lastrowid
        if raw_checkpoint_id is None:
            raise RuntimeError("SQLite did not return a checkpoint row id")
        checkpoint_id = int(raw_checkpoint_id)
    return StateCheckpoint(
        checkpoint_id,
        policy.fingerprint,
        len(signals),
        last_event_id,
        integrity_hash,
        fingerprint,
        state,
        created,
    )


def load_latest_verified_checkpoint(
    path: str | Path,
    signals: list[SignalEvent],
    *,
    runtime_policy: RuntimePolicyBundle | None = None,
) -> StateCheckpoint | None:
    policy = runtime_policy or RuntimePolicyBundle()
    with sqlite3.connect(str(path)) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_checkpoints'"
        ).fetchone()
        if exists is None:
            return None
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT * FROM state_checkpoints
            WHERE policy_fingerprint=? ORDER BY checkpoint_id DESC
            """,
            (policy.fingerprint,),
        ).fetchall()
    for row in rows:
        signal_count = int(row["signal_count"])
        if signal_count > len(signals):
            continue
        last_event_id = str(row["last_event_id"])
        expected_last = signals[signal_count - 1].event_id if signal_count else ""
        if last_event_id != expected_last:
            continue
        integrity_hash = str(row["integrity_record_hash"])
        if integrity_hash != _integrity_hash_for_event(path, last_event_id):
            continue
        try:
            state = _decode_state(str(row["state_json"]))
            assert_valid_book(state, max_open_positions=policy.base.max_open_positions)
        except (KeyError, TypeError, ValueError):
            continue
        fingerprint = state_fingerprint(state)
        if fingerprint != str(row["state_fingerprint"]):
            continue
        return StateCheckpoint(
            checkpoint_id=int(row["checkpoint_id"]),
            policy_fingerprint=str(row["policy_fingerprint"]),
            signal_count=signal_count,
            last_event_id=last_event_id,
            integrity_record_hash=integrity_hash,
            state_fingerprint=fingerprint,
            state=state,
            created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC),
        )
    return None


def restore_state(
    path: str | Path,
    signals: list[SignalEvent],
    *,
    runtime_policy: RuntimePolicyBundle | None = None,
) -> CheckpointRestore:
    policy = runtime_policy or RuntimePolicyBundle()
    checkpoint = load_latest_verified_checkpoint(path, signals, runtime_policy=policy)
    if checkpoint is None:
        full_state, replayed_effects = replay(signals, policy.base)
        return CheckpointRestore(
            False,
            None,
            len(signals),
            full_state,
            replayed_effects,
            "full_replay",
        )

    restored_state = checkpoint.state
    tail = signals[checkpoint.signal_count :]
    tail_effects: list[Effect] = []
    for event in tail:
        restored_state, produced = reduce_book(restored_state, event, policy.base)
        tail_effects.extend(produced)
    assert_valid_book(
        restored_state,
        max_open_positions=policy.base.max_open_positions,
    )
    return CheckpointRestore(
        True,
        checkpoint.checkpoint_id,
        len(tail),
        restored_state,
        tuple(tail_effects),
        "verified_policy_bound_checkpoint_plus_tail",
    )
