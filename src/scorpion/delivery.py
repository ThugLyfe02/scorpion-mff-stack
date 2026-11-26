from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from .fastpath import PreparedExecutionIntent
from .provenance import canonical_json, stable_hash


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


# External paper dispatch deliberately does not inherit the generic lease/retry
# semantics above. A lost HTTP acknowledgement is an unresolved side effect.
_PAPER_GENESIS = "0" * 64
_PAPER_MAX_EVENTS = 10_000
_PAPER_MAX_ORDERS = 200
_PAPER_TERMINAL = frozenset({"FILLED", "CANCELLED", "EXPIRED", "REJECTED"})
_PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_delivery_events (
    sequence INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS paper_delivery_head (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    sequence INTEGER NOT NULL,
    event_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS paper_delivery_events_no_update
BEFORE UPDATE ON paper_delivery_events
BEGIN SELECT RAISE(ABORT,'paper_delivery_append_only'); END;
CREATE TRIGGER IF NOT EXISTS paper_delivery_events_no_delete
BEFORE DELETE ON paper_delivery_events
BEGIN SELECT RAISE(ABORT,'paper_delivery_append_only'); END;
"""


class PaperDeliveryError(ValueError):
    pass


def paper_money(value: object, *, positive: bool = False) -> Decimal:
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise PaperDeliveryError("invalid paper decimal")
    raw = str(value)
    if not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,8})?", raw):
        raise PaperDeliveryError("invalid paper decimal")
    try:
        result = Decimal(raw)
    except InvalidOperation as exc:
        raise PaperDeliveryError("invalid paper decimal") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise PaperDeliveryError("invalid paper decimal")
    return result


def paper_quantity(value: object, *, positive: bool = False) -> int:
    number = paper_money(value, positive=positive)
    if number != number.to_integral_value() or number > 1_000_000:
        raise PaperDeliveryError("paper whole-share quantity required")
    return int(number)


def _paper_clock(value: object) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise PaperDeliveryError("naive paper clock")
    return stamp.astimezone(UTC)


def _paper_digest(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PaperDeliveryError("invalid paper digest")
    return value


def _paper_symbol(value: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,11}", value):
        raise PaperDeliveryError("unsupported equity symbol")
    return value


@dataclass(frozen=True, slots=True)
class PaperRiskPolicy:
    account_id: str
    session_date: date
    maximum_order_usd: Decimal
    maximum_session_buy_usd: Decimal
    maximum_symbol_buy_usd: Decimal
    maximum_group_buy_usd: Decimal
    fee_cash_buffer_usd: Decimal
    maximum_working_orders: int
    symbols_and_groups: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not self.account_id or len(self.account_id) > 128:
            raise PaperDeliveryError("paper account pin required")
        for amount in (
            self.maximum_order_usd, self.maximum_session_buy_usd, self.maximum_symbol_buy_usd,
            self.maximum_group_buy_usd,
        ):
            paper_money(amount, positive=True)
        paper_money(self.fee_cash_buffer_usd)
        if (
            not 1 <= self.maximum_working_orders <= 20
            or not 1 <= len(self.symbols_and_groups) <= 100
            or len(dict(self.symbols_and_groups)) != len(self.symbols_and_groups)
            or tuple(sorted(self.symbols_and_groups)) != self.symbols_and_groups
        ):
            raise PaperDeliveryError("invalid paper portfolio limits or symbol roster")
        for symbol, group in self.symbols_and_groups:
            _paper_symbol(symbol)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", group):
                raise PaperDeliveryError("invalid paper exposure group")

    @property
    def digest(self) -> str:
        return stable_hash(("paper-risk-v1", self))


@dataclass(frozen=True, slots=True)
class PaperOrderRequest:
    call_id: str
    role: str
    symbol: str
    quantity: int
    limit: Decimal
    intent_sha256: str
    prefix_sha256: str
    intent_seq: int
    created: datetime
    deadline: datetime

    def __post_init__(self) -> None:
        _paper_symbol(self.symbol)
        _paper_digest(self.intent_sha256)
        _paper_digest(self.prefix_sha256)
        paper_quantity(self.quantity, positive=True)
        price = paper_money(self.limit, positive=True)
        if not self.call_id or len(self.call_id) > 256 or self.role not in (
            "INITIAL", "CHASE", "EXIT",
        ):
            raise PaperDeliveryError("invalid paper intent identity")
        if type(self.intent_seq) is not int or self.intent_seq <= 0:
            raise PaperDeliveryError("invalid paper intent sequence")
        if not _paper_clock(self.created) < _paper_clock(self.deadline):
            raise PaperDeliveryError("invalid paper intent window")
        # Alpaca US equity sub-penny restrictions; no silent price rounding.
        tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
        if price % tick:
            raise PaperDeliveryError("unsupported equity tick size")

    @property
    def side(self) -> str:
        return "sell" if self.role == "EXIT" else "buy"

    def client_id(self, policy: PaperRiskPolicy) -> str:
        # Stable across later checkpoints and process restarts, not across accounts.
        return "sc-paper-" + stable_hash((
            policy.account_id, policy.session_date, self.call_id, self.role,
        ))[:48]


@dataclass(frozen=True, slots=True)
class PaperVenueOrder:
    client_id: str
    venue_id: str
    symbol: str
    side: str
    requested: int
    limit: Decimal
    filled: int
    average: Decimal | None
    state: str
    submitted: datetime
    updated: datetime

    def __post_init__(self) -> None:
        if not self.client_id or not self.venue_id or self.side not in ("buy", "sell"):
            raise PaperDeliveryError("invalid venue order identity")
        _paper_symbol(self.symbol)
        paper_quantity(self.requested, positive=True)
        paper_quantity(self.filled)
        paper_money(self.limit, positive=True)
        if (
            self.filled > self.requested
            or (self.state == "FILLED") != (self.filled == self.requested)
            or (self.state == "REJECTED" and self.filled)
            or (self.filled > 0) != (self.average is not None)
            or self.state not in _PAPER_TERMINAL | {"WORKING", "PARTIAL", "UNKNOWN"}
            or (self.state == "PARTIAL" and not 0 < self.filled < self.requested)
        ):
            raise PaperDeliveryError("venue order state/quantity conflict")
        if self.average is not None:
            paper_money(self.average, positive=True)
            if (self.side == "buy" and self.average > self.limit) or (
                self.side == "sell" and self.average < self.limit
            ):
                raise PaperDeliveryError("venue average violates limit")
        if _paper_clock(self.updated) < _paper_clock(self.submitted):
            raise PaperDeliveryError("venue order clock conflict")

    @property
    def terminal(self) -> bool:
        return self.state in _PAPER_TERMINAL

    @property
    def notional(self) -> Decimal:
        return self.filled * (self.average or Decimal(0))


@dataclass(frozen=True, slots=True)
class PaperAccountSnapshot:
    account_id: str
    observed_at: datetime
    cash: Decimal
    buying_power: Decimal
    orders: tuple[PaperVenueOrder, ...]
    positions: tuple[tuple[str, int], ...]
    source_sha256: str
    trading_blocked: bool

    def __post_init__(self) -> None:
        _paper_clock(self.observed_at)
        _paper_digest(self.source_sha256)
        paper_money(self.cash)
        paper_money(self.buying_power)
        if (
            not self.account_id or len(self.orders) > 500
            or len({o.client_id for o in self.orders}) != len(self.orders)
            or len({o.venue_id for o in self.orders}) != len(self.orders)
            or len(dict(self.positions)) != len(self.positions)
            or any(o.updated > self.observed_at for o in self.orders)
        ):
            raise PaperDeliveryError("invalid account snapshot")
        for symbol, qty in self.positions:
            _paper_symbol(symbol)
            paper_quantity(qty, positive=True)


@dataclass(frozen=True, slots=True)
class PaperOrderRecord:
    request: PaperOrderRequest
    client_id: str
    dispatched_at: datetime
    observation: PaperVenueOrder | None = None
    cancel_requested: bool = False

    @property
    def state(self) -> str:
        if self.observation is None:
            return "DISPATCH_UNCERTAIN"
        if self.observation.terminal:
            return self.observation.state
        return "CANCEL_UNCERTAIN" if self.cancel_requested else self.observation.state

    @property
    def filled(self) -> int:
        return self.observation.filled if self.observation else 0

    @property
    def buy_commitment(self) -> Decimal:
        if self.request.side != "buy":
            return Decimal(0)
        actual = self.observation.notional if self.observation else Decimal(0)
        if self.observation is not None and self.observation.terminal:
            return actual
        return actual + (self.request.quantity - self.filled) * self.request.limit


@dataclass(frozen=True, slots=True)
class PaperDeliveryReport:
    account_id: str
    policy_sha256: str
    chain_sha256: str
    events: int
    orders: tuple[PaperOrderRecord, ...]
    session_buy_commitment_usd: Decimal
    symbol_buy_commitments: tuple[tuple[str, Decimal], ...]
    group_buy_commitments: tuple[tuple[str, Decimal], ...]
    expected_positions: tuple[tuple[str, int], ...]
    pending_sell_quantities: tuple[tuple[str, int], ...]
    cash_residual_usd: Decimal | None
    blockers: tuple[str, ...]
    authority: str = "PAPER_ONLY"
    live_authorized: bool = False
    training_authorized: bool = False


@dataclass
class _PaperState:
    policy_hash: str
    opening_cash: Decimal
    orders: dict[str, PaperOrderRecord]
    head: str
    sequence: int


def _request_from(data: dict[str, object]) -> PaperOrderRequest:
    return PaperOrderRequest(
        str(data["call_id"]), str(data["role"]), str(data["symbol"]),
        paper_quantity(data["quantity"], positive=True), paper_money(data["limit"], positive=True),
        str(data["intent_sha256"]), str(data["prefix_sha256"]),
        paper_quantity(data["intent_seq"], positive=True),
        _paper_clock(data["created"]), _paper_clock(data["deadline"]),
    )


def _observation_from(data: dict[str, object]) -> PaperVenueOrder:
    return PaperVenueOrder(
        str(data["client_id"]), str(data["venue_id"]), str(data["symbol"]), str(data["side"]),
        paper_quantity(data["requested"], positive=True), paper_money(data["limit"], positive=True),
        paper_quantity(data["filled"]),
        None if data["average"] is None else paper_money(data["average"], positive=True),
        str(data["state"]), _paper_clock(data["submitted"]), _paper_clock(data["updated"]),
    )


def _apply_observation(record: PaperOrderRecord, venue: PaperVenueOrder) -> PaperOrderRecord:
    req = record.request
    if (
        (venue.client_id, venue.symbol, venue.side, venue.requested, venue.limit)
        != (record.client_id, req.symbol, req.side, req.quantity, req.limit)
        or venue.submitted < record.dispatched_at
    ):
        raise PaperDeliveryError("venue observation does not bind dispatch")
    prior = record.observation
    if prior is not None:
        if (
            venue.venue_id != prior.venue_id or venue.submitted != prior.submitted
            or venue.updated < prior.updated or venue.filled < prior.filled
            or (venue.filled == prior.filled and venue.average != prior.average)
            or (prior.terminal and (venue.state, venue.filled) != (prior.state, prior.filled))
        ):
            raise PaperDeliveryError("venue state regression or identity conflict")
        if venue.filled > prior.filled:
            increment = venue.notional - prior.notional
            qty = venue.filled - prior.filled
            if increment <= 0 or (req.side == "buy" and increment > qty * req.limit) or (
                req.side == "sell" and increment < qty * req.limit
            ):
                raise PaperDeliveryError("incremental venue notional violates limit")
    return replace(record, observation=venue)


def _inventory(orders: dict[str, PaperOrderRecord]) -> tuple[dict[str, int], dict[str, int]]:
    held: dict[str, int] = {}
    pending: dict[str, int] = {}
    for order in orders.values():
        symbol = order.request.symbol
        direction = 1 if order.request.side == "buy" else -1
        held[symbol] = held.get(symbol, 0) + direction * order.filled
        if direction == -1 and not (order.observation and order.observation.terminal):
            pending[symbol] = pending.get(symbol, 0) + order.request.quantity - order.filled
    if any(qty < 0 for qty in held.values()):
        raise PaperDeliveryError("negative reconstructed paper inventory")
    return ({key: value for key, value in held.items() if value},
            {key: value for key, value in pending.items() if value})


class PaperDeliveryLedger:
    """Dedicated per-account, per-session append-only external paper order ledger.

    Every dispatch is durably recorded BEFORE network I/O. No lease expiration
    permits resubmission. Reservations and the dispatch transition share one
    BEGIN IMMEDIATE transaction. A second process cannot double-reserve locally.
    The account must be exclusive; an external checkpoint is still needed to
    detect restoring a valid older database.
    """

    def __init__(self, path: Path, policy: PaperRiskPolicy) -> None:
        self.path = path
        self.policy = policy

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        if not create and not self.path.is_file():
            raise PaperDeliveryError("paper ledger not initialized")
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        if create:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_PAPER_SCHEMA)
        return db

    def initialize(self, snapshot: PaperAccountSnapshot, *, now: datetime) -> PaperDeliveryReport:
        now = _paper_clock(now)
        self._check_snapshot(snapshot, now)
        if snapshot.orders or snapshot.positions or snapshot.trading_blocked:
            raise PaperDeliveryError("dedicated opening-flat paper account required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect(create=True)) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM paper_delivery_events LIMIT 1").fetchone():
                raise PaperDeliveryError("paper ledger already initialized")
            if db.execute("SELECT 1 FROM paper_delivery_head LIMIT 1").fetchone():
                raise PaperDeliveryError("unexpected paper ledger checkpoint")
            state = _PaperState(self.policy.digest, snapshot.cash, {}, _PAPER_GENESIS, 0)
            self._append(db, state, "INIT", {
                "policy_sha256": self.policy.digest, "opening_cash": snapshot.cash,
                "snapshot_sha256": stable_hash(snapshot),
            })
            db.execute("COMMIT")
        return self._report(state, snapshot)

    def _append(self, db: sqlite3.Connection, state: _PaperState,
                kind: str, payload: object) -> None:
        if state.sequence >= _PAPER_MAX_EVENTS:
            raise PaperDeliveryError("paper ledger event capacity exhausted")
        raw = canonical_json(payload)
        number = state.sequence + 1
        digest = stable_hash(("paper-delivery-v1", number, kind, raw, state.head))
        db.execute("INSERT INTO paper_delivery_events VALUES (?,?,?,?,?)",
                   (number, kind, raw, state.head, digest))
        db.execute(
            "INSERT INTO paper_delivery_head VALUES (1,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET sequence=excluded.sequence,"
            "event_hash=excluded.event_hash", (number, digest),
        )
        state.sequence, state.head = number, digest

    def _load(self, db: sqlite3.Connection, expected_head: str | None = None) -> _PaperState:
        state = _PaperState("", Decimal(0), {}, _PAPER_GENESIS, 0)
        venue_ids: dict[str, str] = {}
        for row in db.execute("SELECT * FROM paper_delivery_events ORDER BY sequence"):
            seq, kind, raw = int(row["sequence"]), str(row["kind"]), str(row["payload_json"])
            digest = stable_hash(("paper-delivery-v1", seq, kind, raw, state.head))
            if (
                seq != state.sequence + 1 or seq > _PAPER_MAX_EVENTS
                or row["previous_hash"] != state.head or row["event_hash"] != digest
            ):
                raise PaperDeliveryError("paper ledger chain mismatch")
            data = json.loads(raw)
            if kind == "INIT" and seq == 1:
                state.policy_hash = str(data["policy_sha256"])
                state.opening_cash = paper_money(data["opening_cash"])
            elif kind == "DISPATCH" and seq > 1:
                req = _request_from(data["request"])
                key = req.client_id(self.policy)
                if key in state.orders or key != data["client_id"]:
                    raise PaperDeliveryError("duplicate paper dispatch identity")
                state.orders[key] = PaperOrderRecord(req, key, _paper_clock(data["dispatched_at"]))
            elif kind == "OBSERVE" and seq > 1:
                obs = _observation_from(data["observation"])
                if obs.venue_id in venue_ids and venue_ids[obs.venue_id] != obs.client_id:
                    raise PaperDeliveryError("venue order identity replay")
                venue_ids[obs.venue_id] = obs.client_id
                state.orders[obs.client_id] = _apply_observation(state.orders[obs.client_id], obs)
            elif kind == "CANCEL" and seq > 1:
                key = str(data["client_id"])
                prior = state.orders[key]
                if prior.cancel_requested or prior.observation is None or prior.observation.terminal:
                    raise PaperDeliveryError("invalid cancel transition")
                state.orders[key] = replace(prior, cancel_requested=True)
            else:
                raise PaperDeliveryError("unknown paper delivery event")
            state.head, state.sequence = digest, seq
        checkpoint = db.execute("SELECT sequence,event_hash FROM paper_delivery_head").fetchone()
        if (
            not state.sequence or checkpoint is None
            or (checkpoint["sequence"], checkpoint["event_hash"]) != (state.sequence, state.head)
            or state.policy_hash != self.policy.digest
            or (expected_head is not None and state.head != _paper_digest(expected_head))
        ):
            raise PaperDeliveryError("paper policy or ledger checkpoint mismatch")
        return state

    def _check_snapshot(self, snapshot: PaperAccountSnapshot, now: datetime) -> None:
        from .config import MARKET_TZ

        if (
            snapshot.account_id != self.policy.account_id
            or not timedelta(0) <= now - snapshot.observed_at <= timedelta(seconds=10)
            or now.astimezone(MARKET_TZ).date() != self.policy.session_date
        ):
            raise PaperDeliveryError("paper account snapshot stale or wrong scope")

    def _report(self, state: _PaperState,
                snapshot: PaperAccountSnapshot | None = None) -> PaperDeliveryReport:
        symbols: dict[str, Decimal] = {}
        groups: dict[str, Decimal] = {}
        mapping = dict(self.policy.symbols_and_groups)
        blockers: set[str] = set()
        held, sells = _inventory(state.orders)
        for order in state.orders.values():
            symbol = order.request.symbol
            if symbol not in mapping:
                raise PaperDeliveryError("paper ledger symbol outside policy")
            amount = order.buy_commitment
            symbols[symbol] = symbols.get(symbol, Decimal(0)) + amount
            group = mapping[symbol]
            groups[group] = groups.get(group, Decimal(0)) + amount
            if order.state in ("DISPATCH_UNCERTAIN", "CANCEL_UNCERTAIN", "UNKNOWN"):
                blockers.add("unresolved_order_state")
            if order.observation and order.observation.terminal and (
                order.observation.updated > order.request.deadline and order.filled
            ):
                blockers.add("terminal_fill_outside_route_deadline")
        commitment = sum(symbols.values(), Decimal(0))
        residual = None
        if snapshot is not None:
            external = {order.client_id: order for order in snapshot.orders}
            if external.keys() - state.orders.keys():
                blockers.add("unexpected_external_order")
            if any(
                key not in external or order.observation != external[key]
                for key, order in state.orders.items()
            ):
                blockers.add("external_order_coverage_or_state_mismatch")
            if dict(snapshot.positions) != held:
                blockers.add("external_position_mismatch")
            cash = state.opening_cash + sum(
                (o.observation.notional * (1 if o.request.side == "sell" else -1)
                 for o in state.orders.values() if o.observation), Decimal(0),
            )
            residual = snapshot.cash - cash
            if abs(residual) > self.policy.fee_cash_buffer_usd:
                blockers.add("external_cash_residual_exceeds_buffer")
            if snapshot.trading_blocked:
                blockers.add("venue_trading_blocked")
        else:
            blockers.add("fresh_account_reconciliation_required")
        return PaperDeliveryReport(
            self.policy.account_id, state.policy_hash, state.head, state.sequence,
            tuple(state.orders[key] for key in sorted(state.orders)), commitment,
            tuple(sorted(symbols.items())), tuple(sorted(groups.items())),
            tuple(sorted(held.items())), tuple(sorted(sells.items())), residual,
            tuple(sorted(blockers)),
        )

    def status(self, *, expected_head: str | None = None) -> PaperDeliveryReport:
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            return self._report(self._load(db, expected_head))

    def _merge_snapshot(self, db: sqlite3.Connection, state: _PaperState,
                        snapshot: PaperAccountSnapshot) -> None:
        for venue in snapshot.orders:
            record = state.orders.get(venue.client_id)
            if record is None:
                continue
            updated = _apply_observation(record, venue)
            if updated != record:
                if any(
                    o.observation and o.observation.venue_id == venue.venue_id
                    and o.client_id != venue.client_id for o in state.orders.values()
                ):
                    raise PaperDeliveryError("duplicate venue order identity")
                self._append(db, state, "OBSERVE", {
                    "observation": venue, "source_sha256": snapshot.source_sha256,
                })
                state.orders[venue.client_id] = updated

    def reconcile(self, snapshot: PaperAccountSnapshot, *, now: datetime,
                  expected_head: str) -> PaperDeliveryReport:
        """Read-side venue recovery only; never transmits or releases unknown reservations."""
        self._check_snapshot(snapshot, _paper_clock(now))
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._load(db, expected_head)
            self._merge_snapshot(db, state, snapshot)
            report = self._report(state, snapshot)
            db.execute("COMMIT")
        return report

    def reserve_dispatch(self, req: PaperOrderRequest, snapshot: PaperAccountSnapshot,
                         *, now: datetime, expected_head: str) -> PaperDeliveryReport:
        from .config import MARKET_TZ
        from .session_calendar import session_hours

        clock = _paper_clock(now)
        self._check_snapshot(snapshot, clock)
        hours = session_hours(self.policy.session_date)
        if hours is None or not hours[0] <= req.created <= clock < req.deadline <= hours[1]:
            raise PaperDeliveryError("paper order outside authorized regular-session window")
        if clock.astimezone(MARKET_TZ).date() != self.policy.session_date:
            raise PaperDeliveryError("paper session date mismatch")
        key = req.client_id(self.policy)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._load(db, expected_head)
            if key in state.orders or len(state.orders) >= _PAPER_MAX_ORDERS:
                raise PaperDeliveryError("duplicate or exhausted paper dispatch")
            self._merge_snapshot(db, state, snapshot)
            report = self._report(state, snapshot)
            if report.blockers:
                raise PaperDeliveryError("paper reconciliation blocks dispatch")
            if req.symbol not in dict(self.policy.symbols_and_groups):
                raise PaperDeliveryError("paper symbol not approved")
            related = [o for o in state.orders.values() if o.request.call_id == req.call_id]
            if any(o.request.symbol != req.symbol for o in related):
                raise PaperDeliveryError("paper call symbol conflict")
            if req.role == "INITIAL" and related:
                raise PaperDeliveryError("paper initial route already exists")
            if req.role == "CHASE":
                if (
                    len(related) != 1 or related[0].request.role != "INITIAL"
                    or related[0].observation is None
                    or related[0].observation.state not in ("CANCELLED", "EXPIRED")
                    or related[0].filled != 0 or req.quantity > related[0].request.quantity
                ):
                    raise PaperDeliveryError("chase requires terminal confirmed zero initial fill")
            if req.role == "EXIT":
                if (
                    not related or any(o.request.role == "EXIT" or o.observation is None
                                       or not o.observation.terminal for o in related)
                    or req.quantity != sum(o.filled for o in related)
                ):
                    raise PaperDeliveryError("exit requires exact terminal call inventory")
                available = dict(report.expected_positions).get(req.symbol, 0)
                reserved = dict(report.pending_sell_quantities).get(req.symbol, 0)
                if req.quantity > available - reserved:
                    raise PaperDeliveryError("paper sell inventory already reserved")
            working = sum(not (o.observation and o.observation.terminal) for o in state.orders.values())
            if working >= self.policy.maximum_working_orders:
                raise PaperDeliveryError("paper working-order cap")
            if req.side == "buy":
                amount = req.quantity * req.limit
                group = dict(self.policy.symbols_and_groups)[req.symbol]
                ceiling = min(
                    self.policy.maximum_session_buy_usd,
                    state.opening_cash - self.policy.fee_cash_buffer_usd,
                )
                pending_buy = sum(
                    ((o.request.quantity - o.filled) * o.request.limit
                     for o in state.orders.values() if o.request.side == "buy"
                     and not (o.observation and o.observation.terminal)), Decimal(0),
                )
                if (
                    amount > self.policy.maximum_order_usd
                    or report.session_buy_commitment_usd + amount > ceiling
                    or dict(report.symbol_buy_commitments).get(req.symbol, Decimal(0)) + amount
                    > self.policy.maximum_symbol_buy_usd
                    or dict(report.group_buy_commitments).get(group, Decimal(0)) + amount
                    > self.policy.maximum_group_buy_usd
                    or pending_buy + amount + self.policy.fee_cash_buffer_usd
                    > min(snapshot.cash, snapshot.buying_power)
                ):
                    raise PaperDeliveryError("paper exposure reservation refused")
            self._append(db, state, "DISPATCH", {
                "request": asdict(req), "client_id": key, "dispatched_at": clock,
                "snapshot_sha256": stable_hash(snapshot),
            })
            state.orders[key] = PaperOrderRecord(req, key, clock)
            db.execute("COMMIT")
        return self._report(state)

    def observe(self, venue: PaperVenueOrder, *, now: datetime,
                expected_head: str, source_sha256: str) -> PaperDeliveryReport:
        _paper_digest(source_sha256)
        if venue.updated > _paper_clock(now):
            raise PaperDeliveryError("future venue observation")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._load(db, expected_head)
            prior = state.orders[venue.client_id]
            if any(
                o.observation and o.observation.venue_id == venue.venue_id
                and o.client_id != venue.client_id for o in state.orders.values()
            ):
                raise PaperDeliveryError("duplicate venue order identity")
            updated = _apply_observation(prior, venue)
            if updated != prior:
                self._append(db, state, "OBSERVE", {
                    "observation": venue, "source_sha256": source_sha256,
                })
                state.orders[venue.client_id] = updated
            db.execute("COMMIT")
        return self._report(state)

    def claim_cancel(self, client_id: str, *, expected_head: str,
                     now: datetime) -> PaperDeliveryReport:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._load(db, expected_head)
            record = state.orders[client_id]
            if record.cancel_requested or record.observation is None or record.observation.terminal:
                raise PaperDeliveryError("cancel requires a known working order")
            self._append(db, state, "CANCEL", {
                "client_id": client_id, "requested_at": _paper_clock(now),
            })
            state.orders[client_id] = replace(record, cancel_requested=True)
            db.execute("COMMIT")
        return self._report(state)
