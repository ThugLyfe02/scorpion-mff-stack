from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .accuracy import AssociationEvidence, DecisionEvidence
from .domain import Effect, SignalEvent
from .store import Store


class RuntimeHaltedError(RuntimeError):
    """Normalization was denied by the durable runtime halt."""


class RawReceiptInvariantError(RuntimeError):
    """The normalized transition is not causally anchored to a valid raw receipt."""


@dataclass(frozen=True, slots=True)
class TransitionCommitResult:
    inserted: bool
    effect_count: int
    pipeline_latency_us: int
    transaction_latency_us: int


class TransitionCommitter(Protocol):
    def commit(
        self,
        store: Store,
        *,
        raw_revision_id: str,
        event: SignalEvent,
        effects: Sequence[Effect],
        parser: DecisionEvidence,
        association: AssociationEvidence,
        started_ns: int,
        heartbeat_metadata: Mapping[str, object],
    ) -> TransitionCommitResult: ...


class SQLiteTransitionCommitter:
    """Atomically commits the normalized half of a Discord transition.

    Raw receipt remains a separate FULL-sync transaction so a crash can never erase
    evidence that Discord delivered the message. Everything after parsing is committed
    together: signal, effects, audit, raw completion marker, and pipeline heartbeat.
    """

    def commit(
        self,
        store: Store,
        *,
        raw_revision_id: str,
        event: SignalEvent,
        effects: Sequence[Effect],
        parser: DecisionEvidence,
        association: AssociationEvidence,
        started_ns: int,
        heartbeat_metadata: Mapping[str, object],
    ) -> TransitionCommitResult:
        transaction_started_ns = time.perf_counter_ns()
        created = datetime.now(UTC).isoformat()
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                # Serialize admission with Store.set_halt(), which uses the same SQLite
                # writer lock. A pre-transaction check alone leaves a halt/commit race.
                halt = db.execute(
                    "SELECT value FROM runtime_flags WHERE key='halt'"
                ).fetchone()
                if halt is not None:
                    payload = json.loads(halt["value"])
                    if payload.get("halted", False):
                        raise RuntimeHaltedError(
                            f"runtime halted: {payload.get('reason', '')}"
                        )
                receipt = db.execute(
                    """
                    SELECT p.status,r.message_id
                    FROM raw_processing p
                    JOIN raw_discord_events r ON r.raw_event_id=p.raw_event_id
                    WHERE p.raw_event_id=?
                    """,
                    (raw_revision_id,),
                ).fetchone()
                if receipt is None:
                    raise RawReceiptInvariantError("missing raw receipt")
                if receipt["message_id"] != event.message_id:
                    raise RawReceiptInvariantError("raw receipt message mismatch")
                if receipt["status"] not in {"PENDING", "DONE"}:
                    raise RawReceiptInvariantError(
                        f"invalid raw processing status: {receipt['status']}"
                    )
                if receipt["status"] == "DONE":
                    existing = db.execute(
                        "SELECT payload_json FROM signal_events WHERE event_id=?",
                        (event.event_id,),
                    ).fetchone()
                    if existing is None or existing["payload_json"] != store._signal_payload(event):
                        raise RawReceiptInvariantError(
                            "completed raw receipt does not match committed signal"
                        )

                cursor = db.execute(
                    """
                    INSERT OR IGNORE INTO signal_events
                    (event_id,message_id,kind,contract_key,source_ts_utc,received_ts_utc,
                     parser_version,payload_json)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        event.event_id,
                        event.message_id,
                        event.kind.value,
                        event.contract_key,
                        event.source_ts_utc.isoformat(),
                        event.received_ts_utc.isoformat(),
                        event.parser_version,
                        store._signal_payload(event),
                    ),
                )
                inserted = cursor.rowcount == 1
                effect_count = 0
                if inserted:
                    for effect in effects:
                        effect_cursor = db.execute(
                            """
                            INSERT OR IGNORE INTO proposed_effects
                            (source_event_id,kind,contract_key,generation,reason,quantity_hint,
                             metadata_json,created_ts_utc)
                            VALUES (?,?,?,?,?,?,?,?)
                            """,
                            (
                                effect.source_event_id,
                                effect.kind.value,
                                effect.contract_key,
                                effect.generation,
                                effect.reason,
                                effect.quantity_hint,
                                json.dumps(effect.metadata, sort_keys=True),
                                created,
                            ),
                        )
                        effect_count += max(effect_cursor.rowcount, 0)

                pipeline_latency_us = max(0, (time.perf_counter_ns() - started_ns) // 1_000)
                db.execute(
                    """
                    INSERT OR REPLACE INTO decision_audit
                    (event_id,kind,reason,parser_rule,parser_confidence,parser_latency_us,
                     pipeline_latency_us,matched_terms_json,conflicts_json,association_method,
                     association_confidence,association_candidate_count,created_ts_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event.event_id,
                        event.kind.value,
                        event.reason,
                        parser.rule_id,
                        parser.confidence,
                        parser.latency_us,
                        pipeline_latency_us,
                        json.dumps(parser.matched_terms),
                        json.dumps(parser.conflicts),
                        association.method,
                        association.confidence,
                        association.candidate_count,
                        created,
                    ),
                )
                db.execute(
                    "UPDATE raw_processing SET status='DONE',updated_ts_utc=?,error='' "
                    "WHERE raw_event_id=?",
                    (created, raw_revision_id),
                )
                metadata = dict(heartbeat_metadata)
                metadata["pipeline_latency_us"] = pipeline_latency_us
                db.execute(
                    """
                    INSERT INTO heartbeats(component,last_seen_ts_utc,metadata_json)
                    VALUES ('pipeline',?,?)
                    ON CONFLICT(component) DO UPDATE SET
                        last_seen_ts_utc=excluded.last_seen_ts_utc,
                        metadata_json=excluded.metadata_json
                    """,
                    (created, json.dumps(metadata, sort_keys=True)),
                )
                db.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.OperationalError):
                    db.execute("ROLLBACK")
                raise
        transaction_latency_us = max(
            0,
            (time.perf_counter_ns() - transaction_started_ns) // 1_000,
        )
        return TransitionCommitResult(
            inserted=inserted,
            effect_count=effect_count,
            pipeline_latency_us=pipeline_latency_us,
            transaction_latency_us=transaction_latency_us,
        )
