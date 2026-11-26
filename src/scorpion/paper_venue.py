"""Explicitly authorized Alpaca equity paper pilot. No live hostname or retrying POST.

Transport evidence is provisional venue-reported state, not the signed research
outcome ledger. The existing session runtime remains intent-only.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from .causal_features import paper_features_from_records
from .config import EXECUTION_COMPONENT, FORWARD_RULES_SHA256, MARKET_TZ, load_forward_rules
from .delivery import (
    PaperAccountSnapshot,
    PaperDeliveryError,
    PaperDeliveryLedger,
    PaperDeliveryReport,
    PaperOrderRequest,
    PaperRiskPolicy,
    PaperVenueOrder,
    paper_money,
    paper_quantity,
)
from .execution_model import strict_paper_schedule
from .fail_safe_control import NoTradeSafetyLatch
from .provenance import stable_hash
from .session_calendar import session_hours
from .session_service import (
    MAX_PAPER_JOURNAL_BYTES,
    JournalError,
    _journal_identity,
    decode_paper_record,
    journal_checkpoint_path,
    journal_receipt_path,
)

ORIGIN = "https://paper-api.alpaca.markets"
_MAX_REPLY = 8 * 1024 * 1024
Transport = Callable[[urllib.request.Request, float], tuple[int, bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str,
    ) -> None:
        return None


def _send(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    # Fixed HTTPS origin; no ambient proxy credentials or redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(_MAX_REPLY + 1)
            if not isinstance(raw, bytes) or len(raw) > _MAX_REPLY:
                raise PaperDeliveryError("paper response bound")
            return int(response.status), raw
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (OSError, TimeoutError, urllib.error.URLError):
        # Never include credentials, provider bodies, or request URLs in an error.
        raise PaperDeliveryError("paper transport outcome uncertain") from None


def _json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise PaperDeliveryError("duplicate paper response key")
            result[key] = value
        return result

    def invalid(_: str) -> None:
        raise PaperDeliveryError("nonfinite paper response")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, RecursionError) as exc:
        raise PaperDeliveryError("invalid paper response") from exc


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise PaperDeliveryError("missing venue timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise PaperDeliveryError("naive venue timestamp")
    return stamp.astimezone(UTC)


def _uuid(value: object) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise PaperDeliveryError("invalid venue UUID")
    return value


def _order(raw: object) -> PaperVenueOrder:
    if not isinstance(raw, dict):
        raise PaperDeliveryError("invalid venue order")
    if (
        raw.get("asset_class") != "us_equity" or raw.get("type") != "limit"
        or raw.get("time_in_force") != "day" or raw.get("extended_hours") is not False
        or raw.get("order_class") not in ("", None, "simple")
        or raw.get("legs") not in (None, []) or raw.get("replaced_by") is not None
        or raw.get("replaces") is not None
    ):
        raise PaperDeliveryError("unsupported venue order scope")
    states = {
        "new": "WORKING", "accepted": "WORKING", "pending_new": "WORKING",
        "partially_filled": "PARTIAL", "filled": "FILLED",
        "canceled": "CANCELLED", "expired": "EXPIRED", "rejected": "REJECTED",
    }
    state = states.get(str(raw.get("status")), "UNKNOWN")
    client_id = raw.get("client_order_id")
    if not isinstance(client_id, str) or not client_id or len(client_id) > 128:
        raise PaperDeliveryError("invalid venue client order id")
    average = raw.get("filled_avg_price")
    return PaperVenueOrder(
        client_id, _uuid(raw.get("id")), str(raw.get("symbol")), str(raw.get("side")),
        paper_quantity(raw.get("qty"), positive=True),
        paper_money(raw.get("limit_price"), positive=True),
        paper_quantity(raw.get("filled_qty")),
        None if average is None else paper_money(average, positive=True), state,
        _time(raw.get("submitted_at")), _time(raw.get("updated_at")),
    )


def load_paper_risk_policy(path: Path, expected_sha256: str) -> PaperRiskPolicy:
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PaperDeliveryError("paper policy pin mismatch")
    data = _json(raw)
    if not isinstance(data, dict) or data.keys() != {
        "schema", "authority", "account_id", "session_date", "maximum_order_usd",
        "maximum_session_buy_usd", "maximum_symbol_buy_usd", "maximum_group_buy_usd",
        "fee_cash_buffer_usd", "maximum_working_orders", "symbols_and_groups",
    } or data["schema"] != "scorpion.paper-risk.v1" or data["authority"] != "PAPER_ONLY":
        raise PaperDeliveryError("invalid paper risk policy")
    mapping = data["symbols_and_groups"]
    if not isinstance(mapping, dict) or not all(isinstance(v, str) for v in mapping.values()):
        raise PaperDeliveryError("invalid symbol groups")
    policy = PaperRiskPolicy(
        _uuid(data["account_id"]), date.fromisoformat(data["session_date"]),
        paper_money(data["maximum_order_usd"], positive=True),
        paper_money(data["maximum_session_buy_usd"], positive=True),
        paper_money(data["maximum_symbol_buy_usd"], positive=True),
        paper_money(data["maximum_group_buy_usd"], positive=True),
        paper_money(data["fee_cash_buffer_usd"]),
        paper_quantity(data["maximum_working_orders"], positive=True), tuple(sorted(mapping.items())),
    )
    if policy.maximum_order_usd > load_forward_rules().paper_notional_usd:
        raise PaperDeliveryError("paper order budget exceeds frozen strategy")
    return policy


class AlpacaEquityPaperVenue:
    """One pinned paper account; no live credential fallbacks or configurable base URL."""

    def __init__(
        self, account_id: str, *, transport: Transport = _send,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.account_id = _uuid(account_id)
        self._key = os.environ.get("SCORPION_ALPACA_PAPER_KEY", "").strip()
        self._secret = os.environ.get("SCORPION_ALPACA_PAPER_SECRET", "").strip()
        if not self._key or not self._secret:
            raise PaperDeliveryError("paper-only credentials required")
        self._transport = transport
        self._clock = clock

    def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None,
    ) -> tuple[int, bytes]:
        # Only methods and paths generated by this class may reach the transport.
        if not path.startswith("/v2/") or path.startswith("//") or "#" in path:
            raise PaperDeliveryError("invalid paper API path")
        request = urllib.request.Request(
            ORIGIN + path, method=method,
            headers={
                "APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret,
                "Accept": "application/json", "Content-Type": "application/json",
            },
            data=json.dumps(payload, allow_nan=False).encode() if payload is not None else None,
        )
        status, raw = self._transport(request, 3.0)
        if not isinstance(raw, bytes) or len(raw) > _MAX_REPLY:
            raise PaperDeliveryError("paper response bound")
        return status, raw

    def _get(self, path: str, hashes: list[str]) -> Any:
        status, raw = self._request("GET", path)
        if status != 200:
            raise PaperDeliveryError("paper account read refused")
        hashes.append(hashlib.sha256(raw).hexdigest())
        return _json(raw)

    def _account(self, hashes: list[str]) -> tuple[Decimal, Decimal, bool]:
        raw = self._get("/v2/account", hashes)
        if (
            not isinstance(raw, dict) or raw.get("id") != self.account_id
            or raw.get("currency") != "USD" or raw.get("status") != "ACTIVE"
            or type(raw.get("trading_blocked")) is not bool
            or type(raw.get("account_blocked")) is not bool
        ):
            raise PaperDeliveryError("paper account identity or currency conflict")
        return (
            paper_money(raw.get("cash")), paper_money(raw.get("buying_power")),
            bool(raw["trading_blocked"] or raw["account_blocked"]),
        )

    def snapshot(self, session_date: date) -> PaperAccountSnapshot:
        """Bounded double read of account, current/all open orders, and inventory.

        Positions are normalized before comparison so price marks are not treated as
        inventory changes. This is not an atomic venue snapshot.
        """
        started = self._clock()
        mono = time.monotonic()
        if started.tzinfo is None or started.utcoffset() is None:
            raise PaperDeliveryError("naive collection clock")
        start = datetime.combine(session_date, datetime.min.time(), tzinfo=MARKET_TZ)
        if started < start:
            raise PaperDeliveryError("paper snapshot before session")
        query = urllib.parse.urlencode({
            "status": "all", "limit": 500, "direction": "asc", "nested": "false",
            "after": (start - timedelta(microseconds=1)).astimezone(UTC).isoformat(),
        })
        hashes: list[str] = []

        def read() -> tuple[
            tuple[Decimal, Decimal, bool], tuple[PaperVenueOrder, ...], tuple[tuple[str, int], ...]
        ]:
            account = self._account(hashes)
            raw = self._get("/v2/orders?" + query, hashes)
            opened = self._get("/v2/orders?status=open&limit=500&nested=false", hashes)
            positions = self._get("/v2/positions", hashes)
            if (
                not isinstance(raw, list) or not isinstance(opened, list)
                or len(raw) >= 500 or len(opened) >= 500
                or not isinstance(positions, list) or len(positions) > 500
            ):
                raise PaperDeliveryError("paper snapshot may be truncated")
            by_key: dict[str, PaperVenueOrder] = {}
            for item in raw:
                row = _order(item)
                if row.client_id in by_key:
                    raise PaperDeliveryError("duplicate paper order")
                by_key[row.client_id] = row
            open_ids: set[str] = set()
            for item in opened:
                row = _order(item)
                if row.client_id not in by_key or row != by_key[row.client_id]:
                    raise PaperDeliveryError("prior-session or inconsistent working order")
                if row.client_id in open_ids or row.terminal:
                    raise PaperDeliveryError("invalid open order roster")
                open_ids.add(row.client_id)
            if open_ids != {key for key, row in by_key.items() if not row.terminal}:
                raise PaperDeliveryError("working order roster incomplete")
            if any(row.submitted < start for row in by_key.values()):
                raise PaperDeliveryError("order predates paper session")
            held: dict[str, int] = {}
            for item in positions:
                if (
                    not isinstance(item, dict) or item.get("asset_class") != "us_equity"
                    or item.get("side") != "long" or not isinstance(item.get("symbol"), str)
                    or item["symbol"] in held
                ):
                    raise PaperDeliveryError("unsupported paper position")
                held[item["symbol"]] = paper_quantity(item.get("qty"), positive=True)
            return account, tuple(by_key[key] for key in sorted(by_key)), tuple(sorted(held.items()))

        first = read()
        second = read()
        finished = self._clock()
        if first != second or not started <= finished <= started + timedelta(seconds=30):
            raise PaperDeliveryError("paper account changed during collection")
        if time.monotonic() - mono > 30:
            raise PaperDeliveryError("paper snapshot time bound")
        account, orders, positions = second
        return PaperAccountSnapshot(
            self.account_id, finished, account[0], account[1], orders, positions,
            stable_hash(tuple(hashes)), account[2],
        )

    def market_open(self, now: datetime) -> bool:
        raw = self._get("/v2/clock", [])
        if not isinstance(raw, dict):
            raise PaperDeliveryError("invalid paper market clock")
        stamp = _time(raw.get("timestamp"))
        return raw.get("is_open") is True and abs(now - stamp) <= timedelta(seconds=5)

    def submit_once(self, request: PaperOrderRequest, client_id: str) -> tuple[PaperVenueOrder, str]:
        status, raw = self._request("POST", "/v2/orders", {
            "symbol": request.symbol, "qty": str(request.quantity), "side": request.side,
            "type": "limit", "time_in_force": "day", "extended_hours": False,
            "client_order_id": client_id, "limit_price": str(request.limit),
        })
        if status not in (200, 201):
            # Even an HTTP error is not sufficient proof that a side effect did not happen.
            raise PaperDeliveryError("paper submission outcome uncertain")
        return _order(_json(raw)), hashlib.sha256(raw).hexdigest()

    def lookup(self, client_id: str) -> tuple[PaperVenueOrder, str] | None:
        query = urllib.parse.urlencode({"client_order_id": client_id})
        status, raw = self._request("GET", "/v2/orders:by_client_order_id?" + query)
        if status == 404:
            return None  # never interpreted as a definitive cancellation/rejection
        if status != 200:
            raise PaperDeliveryError("paper lookup failed")
        return _order(_json(raw)), hashlib.sha256(raw).hexdigest()

    def cancel_once(self, venue_id: str) -> None:
        status, _ = self._request("DELETE", "/v2/orders/" + _uuid(venue_id))
        if status not in (200, 204):
            raise PaperDeliveryError("paper cancellation outcome uncertain")


def _durable_prefix(path: Path) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    """Read a committed prefix from an active writer; ignore its nondurable tail.

    The checkpoint may advance while we read. Its previously committed prefix must
    still match byte-for-byte. A closed or failed session is not a source of new orders.
    """
    if journal_receipt_path(path).exists():
        raise PaperDeliveryError("closed journal cannot dispatch")
    with journal_checkpoint_path(path).open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise PaperDeliveryError("checkpoint bound")
    checkpoint = decode_paper_record(raw)
    length = checkpoint.get("bytes")
    if type(length) is not int or not 0 < length <= MAX_PAPER_JOURNAL_BYTES:
        raise PaperDeliveryError("invalid durable prefix size")
    with path.open("rb") as handle:
        prefix = handle.read(length)
    records: list[dict[str, object]] = []
    if len(prefix) != length or _journal_identity(io.BytesIO(prefix), records=records) != checkpoint:
        raise PaperDeliveryError("durable prefix identity mismatch")
    if journal_receipt_path(path).exists():
        raise PaperDeliveryError("journal closed during dispatch preparation")
    return checkpoint, tuple(records)


def request_from_live_journal(
    path: Path, seq: int, *, policy: PaperRiskPolicy, now: datetime,
    ledger_report: PaperDeliveryReport, exit_limit: Decimal | None = None,
    expected_record_sha256: str | None = None,
) -> PaperOrderRequest:
    """Bind the dispatch to a frozen, durable current-session intention.

    Exit prices are independently supplied by the operator, never inferred from
    a synthetic fill. The pilot supports only one exit per call.
    """
    receipt, rows = _durable_prefix(path)
    if not 1 <= seq <= len(rows):
        raise PaperDeliveryError("requested intent is not durable")
    start, row = rows[0], rows[seq - 1]
    if expected_record_sha256 is not None and stable_hash(row) != expected_record_sha256:
        raise PaperDeliveryError("candidate differs from durable journal")
    rules = load_forward_rules()
    hours = session_hours(policy.session_date)
    if (
        hours is None or not hours[0] <= now < hours[1]
        or start.get("kind") != "session_start" or start.get("mode") != "PAPER"
        or start.get("session_date") != policy.session_date.isoformat()
        or start.get("rules_sha256") != FORWARD_RULES_SHA256
        or start.get("signal_ingress_enabled") is not True
        or start.get("latch_armed") is not True
        or any(r.get("kind") in ("session_end", "latch", "market_gap", "market_withdrawal")
               or r.get("reason") == "blocked:no_trade_latch"
               or r.get("action") == "blocked:conflicting_signal_create" for r in rows)
    ):
        raise PaperDeliveryError("live paper journal scope or continuity conflict")
    features = paper_features_from_records(receipt, rows)
    if features.refusals:
        raise PaperDeliveryError("invalid durable intent features")
    created = _time(row.get("ts_utc"))
    # Short dispatch admission window prevents stale orders from a stopped writer.
    if not timedelta(0) <= now - created <= timedelta(seconds=10):
        raise PaperDeliveryError("durable intent too old for dispatch")
    material: tuple[object, ...]
    if row.get("kind") == "intent":
        feature = next((f for f in features.rows if f.intent_seq == seq), None)
        if feature is None or exit_limit is not None or feature.rules_sha256 != rules.sha256:
            raise PaperDeliveryError("invalid entry intent")
        symbol, call, role = feature.ticker, feature.call_id, feature.role
        schedule = strict_paper_schedule(
            _time(feature.features["source_ts_utc"]), _time(feature.features["received_ts_utc"]),
            rules, session_open=hours[0], session_close=hours[1],
        )
        due = schedule.chase_ts if role == "CHASE" else schedule.entry_ts
        deadline = (
            min(schedule.chase_ts + timedelta(minutes=1), schedule.exit_ts)
            if role == "CHASE" else schedule.chase_ts
        )
        if not due <= created <= due + timedelta(seconds=3):
            raise PaperDeliveryError("paper intent timer mismatch")
        declared = tuple(_time(row.get(key)) for key in (
            "scheduled_entry_ts_utc", "scheduled_chase_ts_utc", "scheduled_exit_ts_utc",
        ))
        if declared != (schedule.entry_ts, schedule.chase_ts, schedule.exit_ts):
            raise PaperDeliveryError("paper schedule identity mismatch")
        base_age = int(feature.features["quote_age_ns"])
        age = base_age + int((now - created).total_seconds() * 1e9)
        if not 0 <= age <= 3_000_000_000:
            raise PaperDeliveryError("entry quote stale at dispatch")
        admit_until = min(deadline, created + timedelta(microseconds=(3_000_000_000-base_age)//1000))
        qty = paper_quantity(feature.features["quantity"], positive=True)
        limit = paper_money(feature.features["limit"], positive=True)
        if qty * limit > rules.paper_notional_usd:
            raise PaperDeliveryError("frozen paper notional exceeded")
        material = (rows[feature.decision_seq - 1], row)
    elif row.get("kind") == "conditional_exit_intent":
        if (
            exit_limit is None or row.get("status") != "PAPER_INTENT_ONLY"
            or row.get("conditional_on") != "confirmed_entry_fill"
        ):
            raise PaperDeliveryError("exit requires a conditional intent and operator limit")
        call, role = str(row.get("call_id")), "EXIT"
        entries = [o for o in ledger_report.orders if o.request.call_id == call]
        if not entries or any(o.request.role == "EXIT" for o in entries):
            raise PaperDeliveryError("exit call inventory unavailable")
        symbol = entries[0].request.symbol
        qty = sum(o.filled for o in entries)
        if not 0 < qty <= paper_quantity(row.get("quantity"), positive=True):
            raise PaperDeliveryError("exit exceeds recorded conditional quantity")
        scheduled = _time(row.get("scheduled_exit_ts_utc"))
        if created < scheduled:
            raise PaperDeliveryError("premature exit intent")
        deadline = min(scheduled + timedelta(seconds=60), hours[1])
        limit = paper_money(exit_limit, positive=True)
        admit_until = min(deadline, created + timedelta(seconds=10))
        material = (row, str(limit), qty)
    else:
        raise PaperDeliveryError("selected record is not a paper intent")
    if now >= deadline:
        raise PaperDeliveryError("paper route window expired")
    return PaperOrderRequest(
        call, role, symbol, qty, limit, stable_hash(material), features.journal_sha256,
        seq, created, deadline, admit_until,
    )


def _latch_ok(path: Path) -> bool:
    if not path.is_file():
        return False
    latch = NoTradeSafetyLatch(path)
    return latch.verify_integrity(EXECUTION_COMPONENT).valid and (
        latch.state(EXECUTION_COMPONENT).execution_allowed
    )


def dispatch_paper_intent(
    ledger: PaperDeliveryLedger, venue: AlpacaEquityPaperVenue, *,
    journal: Path, seq: int, expected_head: str, latch_path: Path,
    exit_limit: Decimal | None = None, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    supervisor_guard: Callable[[], bool] = lambda: True,
    expected_record_sha256: str | None = None,
) -> PaperDeliveryReport:
    """Explicit invocation. Persist first, send once, never retry a side effect.

    The account remains exclusively paper-only. Unknown outcomes are reported via
    the ledger even when a transport error prevents returning a venue acknowledgement.
    """
    if venue.account_id != ledger.policy.account_id or not _latch_ok(latch_path):
        raise PaperDeliveryError("paper safety or account gate closed")
    if not supervisor_guard():
        raise PaperDeliveryError("paper supervisor admission closed")
    snapshot = venue.snapshot(ledger.policy.session_date)
    if not venue.market_open(clock()):
        raise PaperDeliveryError("venue market closed")
    status = ledger.status(expected_head=expected_head)
    req = request_from_live_journal(
        journal, seq, policy=ledger.policy, now=clock(),
        ledger_report=status, exit_limit=exit_limit,
        expected_record_sha256=expected_record_sha256,
    )
    # Recheck the actual safety state immediately before reserving and sending.
    if not _latch_ok(latch_path):
        raise PaperDeliveryError("paper safety gate closed")
    if not supervisor_guard():
        raise PaperDeliveryError("paper supervisor admission closed")
    reserved = ledger.reserve_dispatch(req, snapshot, now=clock(), expected_head=expected_head)
    key = req.client_id(ledger.policy)
    # If the process stops here or the check fails, the reservation is deliberately
    # uncertain. Operator recovery must inspect the venue; no automatic retry.
    if not supervisor_guard() or not _latch_ok(latch_path) or clock() >= req.deadline:
        return reserved
    # Never submit a changed or stale durable intent after crossing the dispatch boundary.
    try:
        latest = request_from_live_journal(
            journal, seq, policy=ledger.policy, now=clock(),
            ledger_report=status, exit_limit=exit_limit,
            expected_record_sha256=expected_record_sha256,
        )
        if latest.intent_sha256 != req.intent_sha256:
            return reserved
    except (OSError, ValueError, JournalError):
        return reserved
    final_clock = clock()
    if (
        not supervisor_guard() or not _latch_ok(latch_path) or final_clock >= req.deadline
        or (req.admit_until is not None and final_clock > req.admit_until)
    ):
        return reserved
    try:
        observation, digest = venue.submit_once(req, key)
        if observation.client_id != key:
            raise PaperDeliveryError("submission response client identity mismatch")
        return ledger.observe(
            observation, now=clock(), expected_head=reserved.chain_sha256, source_sha256=digest,
        )
    except (PaperDeliveryError, OSError, ValueError):
        return ledger.status(expected_head=reserved.chain_sha256)


def recover_paper_orders(
    ledger: PaperDeliveryLedger, venue: AlpacaEquityPaperVenue, *, expected_head: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PaperDeliveryReport:
    """Read-only remote recovery, preserving missing and uncertain orders."""
    if venue.account_id != ledger.policy.account_id:
        raise PaperDeliveryError("paper account mismatch")
    snapshot = venue.snapshot(ledger.policy.session_date)
    # A missing client ID stays unknown; an explicit lookup may recover a response.
    current = ledger.status(expected_head=expected_head)
    ids = {order.client_id for order in snapshot.orders}
    for record in current.orders:
        if record.client_id in ids:
            continue
        result = venue.lookup(record.client_id)
        if result is not None:
            observation, digest = result
            if observation.client_id != record.client_id:
                raise PaperDeliveryError("lookup response client identity mismatch")
            current = ledger.observe(
                observation, now=clock(), expected_head=current.chain_sha256,
                source_sha256=digest,
            )
    # Fresh complete snapshot must agree even after an individual lookup.
    snapshot = venue.snapshot(ledger.policy.session_date)
    return ledger.reconcile(snapshot, now=clock(), expected_head=current.chain_sha256)


def cancel_paper_order(
    ledger: PaperDeliveryLedger, venue: AlpacaEquityPaperVenue, *, client_id: str,
    expected_head: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PaperDeliveryReport:
    """Persist cancel intent; no automatic retry or assumption that HTTP 204 means canceled."""
    if venue.account_id != ledger.policy.account_id:
        raise PaperDeliveryError("paper account mismatch")
    # Confirm account identity before sending cancellation.
    snapshot = venue.snapshot(ledger.policy.session_date)
    reconciled = ledger.reconcile(snapshot, now=clock(), expected_head=expected_head)
    target = next((row for row in reconciled.orders if row.client_id == client_id), None)
    if target is None or target.observation is None:
        raise PaperDeliveryError("cancel target unverified")
    if target.observation.terminal:
        return reconciled
    if not any(row.client_id == client_id for row in snapshot.orders):
        raise PaperDeliveryError("cancel target absent from fresh venue snapshot")
    status = ledger.claim_cancel(client_id, expected_head=reconciled.chain_sha256, now=clock())
    record = next(row for row in status.orders if row.client_id == client_id)
    assert record.observation is not None
    with suppress(PaperDeliveryError, OSError):
        venue.cancel_once(record.observation.venue_id)
    return status


def sweep_overdue_paper_orders(
    ledger: PaperDeliveryLedger, venue: AlpacaEquityPaperVenue, *, expected_head: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PaperDeliveryReport:
    """One explicitly authorized recovery/cancel pass. Never issues a new order.

    Only overdue, independently observed working identities receive a cancellation.
    A previous cancel request, absent observation or terminal order cannot be retried.
    """
    report = recover_paper_orders(ledger, venue, expected_head=expected_head, clock=clock)
    targets = tuple(
        order.client_id for order in report.orders
        if order.client_id in report.overdue_working_ids and order.observation is not None
        and not order.observation.terminal and not order.cancel_requested
        and order.state in ("WORKING", "PARTIAL")
    )
    for key in targets:
        report = cancel_paper_order(
            ledger, venue, client_id=key, expected_head=report.chain_sha256, clock=clock,
        )
    if targets:
        report = recover_paper_orders(
            ledger, venue, expected_head=report.chain_sha256, clock=clock,
        )
    return report
