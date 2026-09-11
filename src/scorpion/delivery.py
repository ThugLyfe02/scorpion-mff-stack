from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .fastpath import PreparedExecutionIntent


class DeliveryState(StrEnum):
    PREPARED = "PREPARED"
    LEASED = "LEASED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    delivery_id: str
    event_id: str
    effect_kind: str
    contract_key: str
    generation: int
    quantity: int
    limit_price: str
    state: DeliveryState
    attempt_count: int
    lease_owner: str
    lease_until_utc: datetime | None
    created_ts_utc: datetime
    updated_ts_utc: datetime
    terminal_reason: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_delivery_ledger (
    delivery_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    effect_kind TEXT NOT NULL,
    contract_key TEXT NOT NULL,
    generation INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    limit_price TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_until_utc TEXT,
    created_ts_utc TEXT NOT NULL,
    updated_ts_utc TEXT NOT NULL,
    terminal_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_execution_delivery_state
ON execution_delivery_ledger(state,updated_ts_utc);
"""


def delivery_id(intent: PreparedExecutionIntent, *, policy_fingerprint: str) -> str:
    contract_key = intent.effect.contract_key or ""
    limit = str(intent.limit_price) if intent.limit_price is not None else ""
    material = "|".join(
        (
            "delivery-v1",
            intent.event_id,
            intent.effect.kind.value,
            contract_key,
            str(intent.effect.generation),
            str(intent.quantity),
            limit,
            policy_fingerprint,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class DeliveryLedger:
    """Durable idempotency/lease ledger for prepared execution instructions.

    The ledger never talks to a brokerage. It makes downstream delivery retry-safe by ensuring
    that reconnects and worker crashes can re-acquire the same deterministic instruction rather
    than minting a second logical instruction.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def register(
        self,
        intent: PreparedExecutionIntent,
        *,
        policy_fingerprint: str,
        now: datetime | None = None,
    ) -> DeliveryRecord:
        if intent.quantity <= 0:
            raise ValueError("intent quantity must be positive")
        now = (now or datetime.now(UTC)).astimezone(UTC)
        identifier = delivery_id(intent, policy_fingerprint=policy_fingerprint)
        contract_key = intent.effect.contract_key or ""
        limit = str(intent.limit_price) if intent.limit_price is not None else ""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT OR IGNORE INTO execution_delivery_ledger
                (delivery_id,event_id,effect_kind,contract_key,generation,quantity,limit_price,
                 state,created_ts_utc,updated_ts_utc)
                VALUES (?,?,?,?,?,?,?,'PREPARED',?,?)
                """,
                (
                    identifier,
                    intent.event_id,
                    intent.effect.kind.value,
                    contract_key,
                    intent.effect.generation,
                    intent.quantity,
                    limit,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            db.execute("COMMIT")
        return self.get(identifier)

    def acquire(
        self,
        identifier: str,
        *,
        owner: str,
        lease: timedelta = timedelta(seconds=2),
        now: datetime | None = None,
    ) -> DeliveryRecord | None:
        if not owner.strip():
            raise ValueError("lease owner is required")
        if lease <= timedelta(0):
            raise ValueError("lease must be positive")
        now = (now or datetime.now(UTC)).astimezone(UTC)
        lease_until = now + lease
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,lease_until_utc FROM execution_delivery_ledger WHERE delivery_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                return None
            state = DeliveryState(str(row["state"]))
            lease_expired = row["lease_until_utc"] is None or datetime.fromisoformat(
                str(row["lease_until_utc"])
            ).astimezone(UTC) <= now
            if state in {DeliveryState.ACKNOWLEDGED, DeliveryState.REJECTED, DeliveryState.EXPIRED}:
                db.execute("ROLLBACK")
                return None
            if state is DeliveryState.LEASED and not lease_expired:
                db.execute("ROLLBACK")
                return None
            db.execute(
                """
                UPDATE execution_delivery_ledger
                SET state='LEASED',attempt_count=attempt_count+1,lease_owner=?,lease_until_utc=?,
                    updated_ts_utc=?
                WHERE delivery_id=?
                """,
                (owner, lease_until.isoformat(), now.isoformat(), identifier),
            )
            db.execute("COMMIT")
        return self.get(identifier)

    def acknowledge(
        self,
        identifier: str,
        *,
        owner: str,
        accepted: bool,
        reason: str = "",
        now: datetime | None = None,
    ) -> DeliveryRecord:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        terminal = DeliveryState.ACKNOWLEDGED if accepted else DeliveryState.REJECTED
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,lease_owner FROM execution_delivery_ledger WHERE delivery_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise KeyError(identifier)
            state = DeliveryState(str(row["state"]))
            if state in {DeliveryState.ACKNOWLEDGED, DeliveryState.REJECTED, DeliveryState.EXPIRED}:
                db.execute("ROLLBACK")
                return self.get(identifier)
            if state is not DeliveryState.LEASED or str(row["lease_owner"]) != owner:
                db.execute("ROLLBACK")
                raise ValueError("delivery acknowledgement does not own the active lease")
            db.execute(
                """
                UPDATE execution_delivery_ledger
                SET state=?,lease_owner='',lease_until_utc=NULL,updated_ts_utc=?,terminal_reason=?
                WHERE delivery_id=?
                """,
                (terminal.value, now.isoformat(), reason[:1000], identifier),
            )
            db.execute("COMMIT")
        return self.get(identifier)

    def expire(
        self,
        identifier: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> DeliveryRecord:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        with self._connect() as db:
            db.execute(
                """
                UPDATE execution_delivery_ledger
                SET state='EXPIRED',lease_owner='',lease_until_utc=NULL,updated_ts_utc=?,
                    terminal_reason=?
                WHERE delivery_id=? AND state NOT IN ('ACKNOWLEDGED','REJECTED','EXPIRED')
                """,
                (now.isoformat(), reason[:1000], identifier),
            )
        return self.get(identifier)

    def get(self, identifier: str) -> DeliveryRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM execution_delivery_ledger WHERE delivery_id=?",
                (identifier,),
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        lease_until = (
            datetime.fromisoformat(str(row["lease_until_utc"])).astimezone(UTC)
            if row["lease_until_utc"] is not None
            else None
        )
        return DeliveryRecord(
            delivery_id=str(row["delivery_id"]),
            event_id=str(row["event_id"]),
            effect_kind=str(row["effect_kind"]),
            contract_key=str(row["contract_key"]),
            generation=int(row["generation"]),
            quantity=int(row["quantity"]),
            limit_price=str(row["limit_price"]),
            state=DeliveryState(str(row["state"])),
            attempt_count=int(row["attempt_count"]),
            lease_owner=str(row["lease_owner"]),
            lease_until_utc=lease_until,
            created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC),
            updated_ts_utc=datetime.fromisoformat(str(row["updated_ts_utc"])).astimezone(UTC),
            terminal_reason=str(row["terminal_reason"]),
        )
