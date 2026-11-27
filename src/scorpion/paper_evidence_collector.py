"""Independent GET-only paper venue capture and offline unsigned outcome drafting.

Never creates a fill from aggregate order data, signs, trains or grants authority.
The broker response archive remains private. Its digest is not a vendor signature.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path
from typing import Any

from .causal_features import paper_features_from_records
from .config import MARKET_TZ
from .delivery import (
    PaperAccountSnapshot,
    PaperDeliveryLedger,
    PaperVenueOrder,
    paper_money,
    paper_quantity,
)
from .execution_meta_labels import _bounded_bytes, _fields, _parse_trust, _text, _unique_json
from .paper_venue import ORIGIN, Transport, _json, _order, _send, _time, _uuid, load_paper_risk_policy
from .paper_worker import load_worker_authorization
from .paper_worker_calibration import _coverage
from .provenance import canonical_json
from .session_calendar import session_hours
from .session_service import _fsync_directory, read_verified_paper_records

_MAX_RAW = 16 * 1024 * 1024
_MAX_ARCHIVE = 32 * 1024 * 1024
_MAX_PAGES = 20
_PAGE_SIZE = 100
_MAX_COLLECTION_SECONDS = 120
_SOURCE = "alpaca-equity-paper"
_TERMINAL_FIELD = {
    "FILLED": "filled_at", "CANCELLED": "canceled_at",
    "EXPIRED": "expired_at", "REJECTED": "failed_at",
}
_Get = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class ObservedFill:
    identity: str
    order_id: str
    quantity: int
    price: Decimal
    when: datetime
    cumulative: int
    remaining: int
    symbol: str
    side: str


@dataclass(frozen=True, slots=True)
class VenuePass:
    cash: Decimal
    buying_power: Decimal
    blocked: bool
    orders: tuple[PaperVenueOrder, ...]
    positions: tuple[tuple[str, int], ...]
    terminal_times: tuple[tuple[str, datetime | None], ...]
    fills: tuple[ObservedFill, ...]


@dataclass(frozen=True, slots=True)
class CaptureReceipt:
    source_artifact_sha256: str
    account_id: str
    session_date: date
    started_at: datetime
    completed_at: datetime
    orders: int
    fills: int
    requests: int
    blockers: tuple[str, ...]
    authority: str = "PAPER_READ_ONLY"
    authorizes_orders: bool = False
    certifies_outcomes: bool = False


def _session_scope(day: date, through: datetime) -> None:
    hours = session_hours(day)
    if (
        through.tzinfo is None or through.utcoffset() is None
        or through.astimezone(MARKET_TZ).date() != day
        or hours is None or through < hours[1]
    ):
        raise ValueError("collector requires post-close same-session window")


def _query(path: str, values: dict[str, str | int]) -> str:
    return path + "?" + urllib.parse.urlencode(values)


def _fill(raw: object, account: str) -> ObservedFill:
    if not isinstance(raw, dict) or raw.get("activity_type") != "FILL":
        raise ValueError("unexpected fill activity")
    if raw.get("account_id") not in (None, account) or raw.get("type") not in (
        "fill", "partial_fill",
    ):
        raise ValueError("fill scope mismatch")
    return ObservedFill(
        _text(raw.get("id")), _uuid(raw.get("order_id")),
        paper_quantity(raw.get("qty"), positive=True),
        paper_money(raw.get("price"), positive=True), _time(raw.get("transaction_time")),
        paper_quantity(raw.get("cum_qty"), positive=True),
        paper_quantity(raw.get("leaves_qty")), _text(raw.get("symbol")), _text(raw.get("side")),
    )


def _read_pass(get: _Get, account: str, day: date, through: datetime) -> VenuePass:
    start = datetime.combine(day, datetime.min.time(), tzinfo=MARKET_TZ)
    after = (start - timedelta(microseconds=1)).astimezone(UTC).isoformat()
    raw_account = get("/v2/account")
    if (
        not isinstance(raw_account, dict) or raw_account.get("id") != account
        or raw_account.get("currency") != "USD" or raw_account.get("status") != "ACTIVE"
        or type(raw_account.get("trading_blocked")) is not bool
        or type(raw_account.get("account_blocked")) is not bool
    ):
        raise ValueError("collector account identity or scope")
    cash = paper_money(raw_account.get("cash"))
    buying = paper_money(raw_account.get("buying_power"))
    blocked = bool(raw_account["trading_blocked"] or raw_account["account_blocked"])
    orders = get(_query("/v2/orders", {
        "status": "all", "limit": 500, "direction": "asc", "nested": "false", "after": after,
    }))
    opened = get("/v2/orders?status=open&limit=500&nested=false")
    positions = get("/v2/positions")
    if (
        not isinstance(orders, list) or not isinstance(opened, list)
        or not isinstance(positions, list) or len(orders) >= 500
        or len(opened) >= 500 or len(positions) > 500
    ):
        raise ValueError("collector snapshot may be truncated")
    by_id: dict[str, PaperVenueOrder] = {}
    terminal: dict[str, datetime | None] = {}
    clients: set[str] = set()
    for raw in orders:
        row = _order(raw)
        if (
            row.venue_id in by_id or row.client_id in clients
            or not start <= row.submitted <= row.updated <= through
        ):
            raise ValueError("collector order identity or clock mismatch")
        by_id[row.venue_id] = row
        clients.add(row.client_id)
        stamp = _time(raw.get(_TERMINAL_FIELD[row.state])) if row.terminal else None
        if stamp is not None and not row.submitted <= stamp <= row.updated:
            raise ValueError("collector terminal chronology")
        terminal[row.venue_id] = stamp
    working: set[str] = set()
    for raw in opened:
        row = _order(raw)
        if row.terminal or row.venue_id in working or by_id.get(row.venue_id) != row:
            raise ValueError("collector working roster conflict")
        working.add(row.venue_id)
    if working != {key for key, row in by_id.items() if not row.terminal}:
        raise ValueError("collector working roster incomplete")
    held: dict[str, int] = {}
    for row in positions:
        if (
            not isinstance(row, dict) or row.get("asset_class") != "us_equity"
            or row.get("side") != "long"
        ):
            raise ValueError("collector position scope")
        symbol = _text(row.get("symbol"))
        if symbol in held:
            raise ValueError("collector duplicate position")
        held[symbol] = paper_quantity(row.get("qty"), positive=True)
    # Explicit cursor exhaustion, not one page or an order's reported average.
    seen: set[str] = set()
    fills: list[ObservedFill] = []
    params: dict[str, str | int] = {
        "activity_types": "FILL", "after": after, "until": through.isoformat(),
        "direction": "asc", "page_size": _PAGE_SIZE,
    }
    for _ in range(_MAX_PAGES):
        page = get(_query("/v2/account/activities", params))
        if not isinstance(page, list) or len(page) > _PAGE_SIZE:
            raise ValueError("collector fill page bound")
        for raw in page:
            fill = _fill(raw, account)
            if fill.identity in seen:
                raise ValueError("collector duplicate fill identity")
            seen.add(fill.identity)
            fills.append(fill)
        if len(page) < _PAGE_SIZE:
            break
        params["page_token"] = fills[-1].identity
    else:
        raise ValueError("collector fill pagination exhausted")
    grouped: dict[str, list[ObservedFill]] = {key: [] for key in by_id}
    for fill in fills:
        if fill.order_id not in by_id:
            raise ValueError("collector fill outside order roster")
        grouped[fill.order_id].append(fill)
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        for key, order in by_id.items():
            count = 0
            notional = Decimal(0)
            previous = order.submitted
            for fill in sorted(grouped[key], key=lambda f: f.cumulative):
                count += fill.quantity
                notional += fill.quantity * fill.price
                if (
                    count != fill.cumulative or count + fill.remaining != order.requested
                    or (fill.symbol, fill.side) != (order.symbol, order.side)
                    or not previous <= fill.when <= (terminal[key] or through)
                    or (order.side == "buy" and fill.price > order.limit)
                    or (order.side == "sell" and fill.price < order.limit)
                ):
                    raise ValueError("collector fill conservation or chronology")
                previous = fill.when
            if count != order.filled or notional != order.notional:
                # Never synthesize missing fills or silently tolerate rounded averages.
                raise ValueError("collector fill aggregate mismatch")
    return VenuePass(
        cash, buying, blocked, tuple(by_id[key] for key in sorted(by_id)),
        tuple(sorted(held.items())), tuple(sorted(terminal.items())),
        tuple(sorted(fills, key=lambda f: (f.order_id, f.cumulative))),
    )


def _private_write(path: Path, raw: bytes) -> None:
    if not raw or len(raw) > _MAX_ARCHIVE:
        raise ValueError("collector artifact byte bound")
    # Exclusive creation; never overwrite an earlier source or follow a symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _blockers(view: VenuePass) -> tuple[str, ...]:
    return tuple(sorted(
        (["working_or_unknown_orders"] if any(not o.terminal for o in view.orders) else [])
        + (["account_not_flat"] if view.positions else [])
        + (["account_blocked"] if view.blocked else [])
    ))


def collect_venue_artifact(
    *, account_id: str, session_date: date, output: Path,
    transport: Transport = _send, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CaptureReceipt:
    """Bounded GET-only capture with exact response bytes, two agreeing normalized reads.

    The collector is independently operated; credentials may be capable of more
    at the provider, but this code has no POST/DELETE path.
    """
    account = _uuid(account_id)
    started = clock()
    _session_scope(session_date, started)
    if output.exists():
        raise ValueError("collector output already exists")
    key = os.environ.get("SCORPION_ALPACA_PAPER_KEY", "").strip()
    secret = os.environ.get("SCORPION_ALPACA_PAPER_SECRET", "").strip()
    if not key or not secret:
        raise ValueError("paper-only collector credentials required")
    events: list[dict[str, object]] = []
    size = 0
    mono = time.monotonic()

    def get(path: str) -> Any:
        nonlocal size
        if time.monotonic() - mono > _MAX_COLLECTION_SECONDS:
            raise ValueError("collector elapsed bound")
        request = urllib.request.Request(ORIGIN + path, method="GET", headers={
            "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json",
        })
        status, raw = transport(request, 3.0)
        if status != 200 or not isinstance(raw, bytes):
            raise ValueError("collector GET refused")
        size += len(raw)
        if size > _MAX_RAW:
            raise ValueError("collector raw byte bound")
        events.append({
            "path": path, "sha256": hashlib.sha256(raw).hexdigest(),
            "body_b64": base64.b64encode(raw).decode("ascii"),
        })
        return _json(raw)

    first = _read_pass(get, account, session_date, started)
    second = _read_pass(get, account, session_date, started)
    completed = clock()
    if (
        first != second or not started <= completed <= started + timedelta(
            seconds=_MAX_COLLECTION_SECONDS,
        ) or time.monotonic() - mono > _MAX_COLLECTION_SECONDS
    ):
        raise ValueError("collector inconsistent double read or time bound")
    _session_scope(session_date, completed)
    raw = canonical_json({
        "schema": "scorpion.alpaca-paper-source.v1", "authority": "PAPER_READ_ONLY",
        "account_id": account, "session_date": session_date, "started_at": started,
        "completed_at": completed, "responses": events,
    }).encode()
    _private_write(output, raw)
    return CaptureReceipt(
        hashlib.sha256(raw).hexdigest(), account, session_date, started, completed,
        len(first.orders), len(first.fills), len(events), _blockers(first),
    )


def replay_venue_artifact(
    path: Path, pin: str, *, account: str, day: date,
) -> tuple[CaptureReceipt, VenuePass]:
    """Replay the exact expected request sequence offline, without credentials."""
    raw = _bounded_bytes(path, _MAX_ARCHIVE)
    if hashlib.sha256(raw).hexdigest() != pin:
        raise ValueError("collector artifact pin mismatch")
    data = _unique_json(raw)
    _fields(data, {
        "schema", "authority", "account_id", "session_date", "started_at",
        "completed_at", "responses",
    })
    if (
        data["schema"] != "scorpion.alpaca-paper-source.v1"
        or data["authority"] != "PAPER_READ_ONLY"
        or data["account_id"] != account or data["session_date"] != day.isoformat()
    ):
        raise ValueError("collector artifact identity mismatch")
    started, completed = _time(data["started_at"]), _time(data["completed_at"])
    _session_scope(day, started)
    _session_scope(day, completed)
    if not started <= completed <= started + timedelta(seconds=_MAX_COLLECTION_SECONDS):
        raise ValueError("collector artifact timing")
    rows = data["responses"]
    if not isinstance(rows, list) or len(rows) > 2*(4+_MAX_PAGES):
        raise ValueError("collector artifact response bound")
    cursor = size = 0

    def get(request_path: str) -> Any:
        nonlocal cursor, size
        if cursor >= len(rows):
            raise ValueError("collector artifact missing response")
        row = rows[cursor]
        cursor += 1
        if not isinstance(row, dict):
            raise ValueError("collector response schema")
        _fields(row, {"path", "sha256", "body_b64"})
        if row["path"] != request_path or not isinstance(row["body_b64"], str):
            raise ValueError("collector artifact request sequence")
        try:
            body = base64.b64decode(row["body_b64"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("collector artifact body encoding") from exc
        size += len(body)
        if size > _MAX_RAW or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise ValueError("collector response identity or bound")
        return _json(body)

    first = _read_pass(get, account, day, started)
    second = _read_pass(get, account, day, started)
    if first != second or cursor != len(rows):
        raise ValueError("collector artifact inconsistent or extra responses")
    return CaptureReceipt(
        pin, account, day, started, completed,
        len(first.orders), len(first.fills), cursor, _blockers(first),
    ), first


@dataclass(frozen=True, slots=True)
class OutcomeDraftReceipt:
    source_artifact_sha256: str
    journal_sha256: str
    worker_audit_sha256: str
    ledger_head: str
    draft_sha256: str
    candidates: int
    entry_orders: int
    exit_orders: int
    fills: int
    undispatched_entries: int
    authority: str = "UNSIGNED_RESEARCH_DRAFT"
    authorizes_training: bool = False
    authorizes_orders: bool = False


def prepare_outcome_draft(
    *, journal: Path, audit_path: Path, worker_config: Path, worker_pin: str,
    risk_path: Path, ledger_path: Path, ledger_head: str, trust_path: Path,
    trust_pin: str, source: Path, source_pin: str, output: Path,
) -> OutcomeDraftReceipt:
    """Cross-check actual fill activities against a sealed worker campaign.

    Output uses the existing outcome schema with reviewed_ts_utc=null. It
    intentionally cannot pass admission until an independent review timestamp
    and both detached signatures are supplied outside this process.
    """
    auth = load_worker_authorization(worker_config, worker_pin)
    policy = load_paper_risk_policy(risk_path, auth.risk_pin)
    trust = _parse_trust(_bounded_bytes(trust_path, 65536), trust_pin)
    if trust.account_ref != policy.account_id or trust.source_id != _SOURCE:
        raise ValueError("collector trust identity mismatch")
    capture, view = replay_venue_artifact(source, source_pin, account=policy.account_id,
                                          day=policy.session_date)
    source_receipt, records = read_verified_paper_records(journal)
    audit_receipt, audit = read_verified_paper_records(audit_path)
    if (
        not records or records[0].get("kind") != "session_start"
        or records[-1].get("kind") != "session_end"
        or records[0].get("session_date") != policy.session_date.isoformat()
        or records[0].get("rules_sha256") != trust.rules_sha256
        or trust.registered > _time(records[0].get("ts_utc"))
        or not audit or audit[0].get("kind") != "worker_start"
        or audit[-1].get("kind") != "worker_shutdown_final"
        or audit[0].get("authorization_sha256") != auth.digest
        or audit[0].get("policy_sha256") != policy.digest
        or audit[0].get("ledger_head") != auth.opening_head
        or audit[-1].get("ledger_head") != ledger_head
        or audit[-1].get("blockers") != []
        or any(row.get("authority") != "RESEARCH_ONLY" or row.get("kind") == "worker_fault"
               for row in audit)
        or capture.started_at < max(_time(records[-1].get("ts_utc")),
                                    _time(audit[-1].get("ts_utc")))
        or capture.blockers
    ):
        raise ValueError("collector requires sealed reconciled campaign")
    candidates, dispositions = _coverage(records, audit)
    snap = PaperAccountSnapshot(
        policy.account_id, capture.started_at, view.cash, view.buying_power, view.orders,
        view.positions, capture.source_artifact_sha256, view.blocked,
    )
    status = PaperDeliveryLedger(ledger_path, policy).inspect_snapshot(
        snap, expected_head=ledger_head,
    )
    if status.blockers or status.expected_positions:
        raise ValueError("collector ledger reconciliation blocked")
    orders = {o.request.intent_seq: o for o in status.orders}
    if len(orders) != len(status.orders) or set(orders) != {
        seq for seq, row in dispositions.items() if row["disposition"] == "DURABLE_DISPATCH"
    }:
        raise ValueError("collector dispatch roster mismatch")
    evidence = paper_features_from_records(source_receipt, records)
    features = {row.intent_seq: row for row in evidence.rows}
    if evidence.refusals or set(features) != {
        seq for seq, row in dispositions.items() if row["role"] != "EXIT"
    }:
        raise ValueError("collector feature coverage mismatch")
    times = dict(view.terminal_times)
    by_order: dict[str, list[ObservedFill]] = {o.venue_id: [] for o in view.orders}
    for fill in view.fills:
        by_order[fill.order_id].append(fill)
    entries: list[dict[str, object]] = []
    exits = 0
    for seq, row in sorted(orders.items()):
        req, obs = row.request, row.observation
        if (
            obs is None or not obs.terminal or times[obs.venue_id] is None
            or req.call_id != candidates[seq].get("call_id")
            or req.role != dispositions[seq].get("role")
            or req.created != _time(candidates[seq].get("ts_utc"))
        ):
            raise ValueError("collector logical order identity mismatch")
        if req.role == "EXIT":
            if req.limit != dict(auth.exit_limits).get(req.symbol):
                raise ValueError("collector exit policy mismatch")
            exits += 1
            continue
        feature = features[seq]
        if (
            (req.call_id, req.symbol, req.role) != (feature.call_id, feature.ticker, feature.role)
            or feature.rules_sha256 != trust.rules_sha256
            or req.quantity != feature.features["quantity"]
            or req.limit != paper_money(feature.features["limit"], positive=True)
        ):
            raise ValueError("collector entry binding mismatch")
        entries.append({
            "feature_sha256": feature.feature_sha256, "venue_order_id": obs.venue_id,
            "call_id": req.call_id, "ticker": req.symbol, "role": req.role,
            "intent_seq": seq, "requested_quantity": req.quantity, "limit": str(req.limit),
            "state": obs.state, "submitted_ts_utc": obs.submitted,
            "terminal_ts_utc": times[obs.venue_id], "asset_class": "EQUITY",
            "side": "BUY", "currency": "USD", "quantity_unit": "SHARES",
            "fills": [{
                "fill_id": fill.identity, "quantity": fill.quantity,
                "price": str(fill.price), "filled_ts_utc": fill.when,
            } for fill in by_order[obs.venue_id]],
        })
    raw = canonical_json({
        "schema": "scorpion.paper-outcome-export.v1", "authority": "PAPER_ONLY",
        "source_id": trust.source_id, "account_ref": trust.account_ref,
        "trust_sha256": trust_pin, "journal_sha256": evidence.journal_sha256,
        "source_artifact_sha256": capture.source_artifact_sha256,
        "complete_order_export": True, "observed_through_ts_utc": capture.started_at,
        "collected_ts_utc": capture.completed_at, "reviewed_ts_utc": None, "orders": entries,
    }).encode()
    _private_write(output, raw)
    return OutcomeDraftReceipt(
        source_pin, evidence.journal_sha256, str(audit_receipt["sha256"]), ledger_head,
        hashlib.sha256(raw).hexdigest(), len(candidates), len(entries), exits,
        len(view.fills), len(features)-len(entries),
    )
