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

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from .calendar import et_date
from .instruments import OptionContract
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


@dataclass(frozen=True)
class PaperOrderObservation:
    """A normalized terminal paper-order observation; never a live order instruction."""

    client_id: str
    osi: str
    action: str
    requested_qty: int
    limit: Decimal
    filled_qty: int
    average_price: Decimal | None
    state: str

    def __post_init__(self) -> None:
        if not self.client_id or not self.osi or self.action not in ("buy", "sell"):
            raise ReconcileError("invalid paper order identity")
        if (
            type(self.requested_qty) is not int or self.requested_qty <= 0
            or type(self.filled_qty) is not int
            or not 0 <= self.filled_qty <= self.requested_qty
        ):
            raise ReconcileError("invalid paper order quantity")
        if not self.limit.is_finite() or self.limit <= 0:
            raise ReconcileError("invalid paper order limit")
        if self.filled_qty:
            if (
                self.average_price is None or not self.average_price.is_finite()
                or self.average_price <= 0
            ):
                raise ReconcileError("filled paper order has no valid average price")
        elif self.average_price is not None:
            raise ReconcileError("unfilled paper order cannot claim a fill price")
        if self.state not in (
            "FILLED", "CANCELLED", "REJECTED", "EXPIRED", "PENDING", "WORKING",
            "PARTIAL", "UNKNOWN",
        ):
            raise ReconcileError("unknown paper order state")
        if self.state == "FILLED" and self.filled_qty != self.requested_qty:
            raise ReconcileError("filled paper order has incomplete quantity")
        if self.state in ("REJECTED", "PENDING", "WORKING") and self.filled_qty:
            raise ReconcileError("paper order state contradicts fills")
        if self.state == "PARTIAL" and not 0 < self.filled_qty < self.requested_qty:
            raise ReconcileError("partial paper order has invalid quantity")

    @property
    def terminal(self) -> bool:
        return self.state in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED")


def paper_orders_from_payload(payload: object) -> list[PaperOrderObservation]:
    """Read explicitly normalized rows; never guess vendor field names or order identities."""
    if not isinstance(payload, list):
        raise ReconcileError("paper orders must be an array")
    rows: list[PaperOrderObservation] = []
    for row in payload:
        if not isinstance(row, dict):
            raise ReconcileError("paper order must be an object")
        try:
            identity = (row["client_id"], row["osi"], row["action"], row["state"])
            if not all(isinstance(v, str) and v for v in identity):
                raise ReconcileError("paper order identity fields must be strings")
            price = row["average_price"]
            rows.append(PaperOrderObservation(
                client_id=row["client_id"], osi=row["osi"], action=row["action"],
                requested_qty=row["requested_qty"], limit=Decimal(str(row["limit"])),
                filled_qty=row["filled_qty"],
                average_price=Decimal(str(price)) if price is not None else None,
                state=row["state"],
            ))
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise ReconcileError("malformed normalized paper order") from exc
    return rows


def compare_paper_orders(
    modeled: Iterable[PaperOrderObservation], external: Iterable[PaperOrderObservation],
) -> dict[str, Any]:
    """Compare matched terminal orders without treating missing orders as zero fills."""
    def index(rows: Iterable[PaperOrderObservation]) -> dict[str, PaperOrderObservation]:
        result: dict[str, PaperOrderObservation] = {}
        for row in rows:
            if row.client_id in result:
                raise ReconcileError("duplicate paper client identity")
            result[row.client_id] = row
        return result

    local, venue = index(modeled), index(external)
    missing = sorted(local.keys() - venue.keys())
    extra = sorted(venue.keys() - local.keys())
    issues: list[dict[str, str]] = []
    equal_fill_samples = 0
    cost = Decimal(0)
    for key in sorted(local.keys() & venue.keys()):
        a, b = local[key], venue[key]
        reasons: list[str] = []
        identity_match = (
            (a.osi, a.action, a.requested_qty, a.limit)
            == (b.osi, b.action, b.requested_qty, b.limit)
        )
        if not identity_match:
            reasons.append("intent_mismatch")
        if not a.terminal or not b.terminal:
            reasons.append("unresolved_order")
        if a.state != b.state:
            reasons.append("terminal_state_mismatch")
        if a.filled_qty != b.filled_qty:
            reasons.append("fill_quantity_mismatch")
        if b.average_price is not None and (
            (b.action == "buy" and b.average_price > b.limit)
            or (b.action == "sell" and b.average_price < b.limit)
        ):
            reasons.append("venue_limit_violation")
        # Price economics are comparable only on an identical, terminal, filled quantity.
        if (
            identity_match and a.terminal and b.terminal and a.filled_qty == b.filled_qty
            and a.filled_qty > 0 and a.average_price is not None
            and b.average_price is not None
        ):
            equal_fill_samples += 1
            direction = 1 if a.action == "buy" else -1
            cost += (b.average_price - a.average_price) * a.filled_qty * 100 * direction
            if a.average_price != b.average_price:
                reasons.append("fill_price_difference")
        if reasons:
            issues.extend({"client_id": key, "reason": reason} for reason in reasons)
    return {
        "modeled_orders": len(local), "external_orders": len(venue),
        "matched_orders": len(local.keys() & venue.keys()),
        "missing_order_ids": missing, "extra_order_ids": extra, "issues": issues,
        "equal_quantity_price_samples": equal_fill_samples,
        "external_minus_modeled_cost_usd": str(cost) if equal_fill_samples else None,
        "coverage_complete": bool(local) and not missing and not extra,
        "exact_parity": bool(local) and not missing and not extra and not issues,
    }


# Read-only Alpaca paper REST collection. No POST, DELETE, configurable endpoint,
# live hostname, or credential transmission to a caller-provided URL.
_ALPACA_ORIGIN = "https://paper-api.alpaca.markets"
_ALPACA_LIMIT = 500
_ALPACA_ACTIVITY_PAGE = 100
_ALPACA_ACTIVITY_MAX_PAGES = 10
_ALPACA_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_ALPACA_MAX_RESPONSE = 16 * 1024 * 1024


class _NoPaperRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _paper_get(request: urllib.request.Request, timeout: float) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoPaperRedirect())
    try:
        with opener.open(request, timeout=timeout) as reply:
            raw = reply.read(_ALPACA_MAX_RESPONSE + 1)
            if not isinstance(raw, bytes) or len(raw) > _ALPACA_MAX_RESPONSE:
                raise ReconcileError("paper venue response bound")
            return raw
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ReconcileError("paper venue read failed") from None


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ReconcileError("noninteger venue quantity")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ReconcileError("noninteger venue quantity") from None
    if not number.is_finite() or number != number.to_integral_value() or not 0 <= number <= 10**9:
        raise ReconcileError("noninteger venue quantity")
    return int(number)


def _venue_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReconcileError("missing venue timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReconcileError("invalid venue timestamp") from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ReconcileError("naive venue timestamp")
    return stamp.astimezone(UTC)


def _alpaca_osi(value: object) -> str:
    if not isinstance(value, str):
        raise ReconcileError("missing option symbol")
    # Alpaca compact OCC symbols use the fixed 15-character date/side/strike suffix.
    if not 16 <= len(value) <= 21 or any(c.isspace() for c in value):
        raise ReconcileError("invalid compact option symbol")
    try:
        return OptionContract.parse_osi_symbol(value[:-15].ljust(6) + value[-15:]).osi_symbol
    except ValueError:
        raise ReconcileError("unsupported option identity") from None


def _alpaca_order(row: object, day: date, cutoff: datetime) -> PaperOrderObservation:
    if not isinstance(row, dict):
        raise ReconcileError("invalid paper venue order")
    if (
        row.get("asset_class") != "us_option" or row.get("type") != "limit"
        or row.get("order_class") not in (None, "", "simple")
        or row.get("legs") not in (None, [])
        or row.get("replaced_by") is not None or row.get("replaces") is not None
    ):
        raise ReconcileError("unsupported paper order scope or replacement")
    submitted = _venue_timestamp(row.get("submitted_at"))
    updated = _venue_timestamp(row.get("updated_at"))
    if et_date(submitted) != day or not submitted <= updated <= cutoff:
        raise ReconcileError("paper venue order outside observation cutoff")
    state_map = {
        "filled": "FILLED", "canceled": "CANCELLED", "expired": "EXPIRED",
        "rejected": "REJECTED", "new": "WORKING", "accepted": "PENDING",
        "pending_new": "PENDING", "partially_filled": "PARTIAL",
    }
    state = state_map.get(str(row.get("status")), "UNKNOWN")
    client_id = row.get("client_order_id")
    if not isinstance(client_id, str) or not client_id or not row.get("id"):
        raise ReconcileError("missing paper venue order identity")
    try:
        quantity = _integer(row.get("filled_qty"))
        avg = row.get("filled_avg_price")
        return PaperOrderObservation(
            client_id, _alpaca_osi(row.get("symbol")), str(row.get("side")),
            _integer(row.get("qty")), Decimal(str(row.get("limit_price"))),
            quantity, Decimal(str(avg)) if avg is not None else None, state,
        )
    except InvalidOperation:
        raise ReconcileError("invalid paper venue price") from None


@dataclass(frozen=True)
class PaperFillActivity:
    fill_id: str
    venue_order_id: str
    client_order_id: str
    osi: str
    side: str
    quantity: int
    cumulative_quantity: int
    leaves_quantity: int
    price: Decimal
    transaction_ts_utc: datetime


def _validate_fill_activities(
    raw_orders: list[Any], orders: list[PaperOrderObservation], activities: list[Any],
    day: date, cutoff: datetime,
) -> tuple[PaperFillActivity, ...]:
    """Cross-check detailed executions against the independent order summaries."""
    by_id = {raw["id"]: (raw, row) for raw, row in zip(raw_orders, orders, strict=True)}
    result: list[PaperFillActivity] = []
    seen: set[str] = set()
    groups: dict[str, list[PaperFillActivity]] = {}
    for activity in activities:
        if not isinstance(activity, dict) or activity.get("activity_type") != "FILL":
            raise ReconcileError("unexpected account activity")
        identity, order_id = activity.get("id"), activity.get("order_id")
        if (
            not isinstance(identity, str) or not identity or len(identity) > 256
            or identity in seen or not isinstance(order_id, str) or order_id not in by_id
        ):
            raise ReconcileError("duplicate or unmatched fill activity")
        seen.add(identity)
        raw, order = by_id[order_id]
        stamp = _venue_timestamp(activity.get("transaction_time"))
        if (
            et_date(stamp) != day or not _venue_timestamp(raw.get("submitted_at"))
            <= stamp <= _venue_timestamp(raw.get("updated_at")) <= cutoff
            or _alpaca_osi(activity.get("symbol")) != order.osi
            or activity.get("side") != order.action
        ):
            raise ReconcileError("fill activity identity or clock mismatch")
        qty = _integer(activity.get("qty"))
        cum, leaves = _integer(activity.get("cum_qty")), _integer(activity.get("leaves_qty"))
        try:
            price = Decimal(str(activity.get("price")))
        except InvalidOperation:
            raise ReconcileError("invalid fill activity price") from None
        if (
            qty <= 0 or not price.is_finite() or price <= 0 or price > 1_000_000
            or cum + leaves != order.requested_qty
            or (activity.get("type") != ("fill" if leaves == 0 else "partial_fill"))
            or (order.action == "buy" and price > order.limit)
            or (order.action == "sell" and price < order.limit)
        ):
            raise ReconcileError("fill activity quantity or limit conflict")
        fill = PaperFillActivity(
            identity, order_id, order.client_id, order.osi, order.action,
            qty, cum, leaves, price, stamp,
        )
        groups.setdefault(order_id, []).append(fill)
        result.append(fill)
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        for order_id, (_, order) in by_id.items():
            # Cum quantity orders the actual progression, including same-time partials.
            fills = sorted(groups.get(order_id, []), key=lambda row: row.cumulative_quantity)
            total = 0
            premium = Decimal(0)
            last: datetime | None = None
            for fill in fills:
                total += fill.quantity
                if total != fill.cumulative_quantity or (
                    last is not None and fill.transaction_ts_utc < last
                ):
                    raise ReconcileError("fill activity cumulative sequence mismatch")
                last = fill.transaction_ts_utc
                premium += fill.quantity * fill.price
            if total != order.filled_qty:
                raise ReconcileError("order and fill activity quantity mismatch")
            if total and (
                order.average_price is None or premium != order.average_price * total
            ):
                # Never silently round away a venue-average discrepancy.
                raise ReconcileError("order and fill activity premium mismatch")
    return tuple(sorted(result, key=lambda row: (row.transaction_ts_utc, row.fill_id)))


def collect_alpaca_paper_export(
    *, account_id: str, session_date: date, input_sha256: str, capture_end_ts: float,
    destination: Path, transport: Callable[[urllib.request.Request, float], bytes] = _paper_get,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Fetch a stable, bounded account snapshot and normalize for existing comparator.

    Dedicated options paper account only. A capped or changing export is refused;
    repeated reads are not a claim of atomicity or independently verified fills.
    """
    key = os.environ.get("SCORPION_ALPACA_PAPER_KEY", "").strip()
    secret = os.environ.get("SCORPION_ALPACA_PAPER_SECRET", "").strip()
    if not key or not secret or not account_id:
        raise ReconcileError("paper-specific credentials and account pin required")
    if len(input_sha256) != 64 or any(c not in "0123456789abcdef" for c in input_sha256):
        raise ReconcileError("invalid capture identity")
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ReconcileError("naive paper collection clock")
    now = now.astimezone(UTC)
    if (
        not isinstance(capture_end_ts, (int, float)) or isinstance(capture_end_ts, bool)
        or not 0 < capture_end_ts <= now.timestamp() or et_date(now) != session_date
    ):
        raise ReconcileError("paper collection outside capture session")
    # Exclusive after boundary: include midnight by subtracting a microsecond.
    from zoneinfo import ZoneInfo

    start = datetime.combine(session_date, datetime.min.time(), tzinfo=ZoneInfo("America/New_York"))
    after = (start - timedelta(microseconds=1)).astimezone(UTC).isoformat()
    query = urllib.parse.urlencode({
        "status": "all", "limit": _ALPACA_LIMIT, "after": after,
        "until": now.isoformat(), "direction": "asc", "nested": "false",
    })
    headers = {
        "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }

    collection_start = time.monotonic()
    total_bytes = 0

    def get(path: str) -> tuple[bytes, Any]:
        nonlocal total_bytes
        remaining = 60 - (time.monotonic() - collection_start)
        if remaining <= 0:
            raise ReconcileError("paper collection time budget")
        try:
            raw = transport(urllib.request.Request(
                _ALPACA_ORIGIN + path, headers=headers, method="GET",
            ), min(5.0, remaining))
            total_bytes += len(raw)
            if (
                len(raw) > _ALPACA_MAX_RESPONSE or total_bytes > _ALPACA_MAX_TOTAL_BYTES
                or time.monotonic() - collection_start > 60
            ):
                raise ReconcileError("paper response or collection bound")
            return raw, json.loads(raw)
        except (OSError, ValueError, TypeError):
            raise ReconcileError("paper venue payload unavailable") from None

    def get_fills() -> tuple[list[bytes], list[Any]]:
        pages: list[bytes] = []
        items: list[Any] = []
        seen: set[str] = set()
        token: str | None = None
        for _ in range(_ALPACA_ACTIVITY_MAX_PAGES):
            params = {
                "after": after, "until": now.isoformat(), "direction": "asc",
                "page_size": str(_ALPACA_ACTIVITY_PAGE),
            }
            if token is not None:
                params["page_token"] = token
            page_raw, page = get("/v2/account/activities/FILL?" + urllib.parse.urlencode(params))
            if not isinstance(page, list) or len(page) > _ALPACA_ACTIVITY_PAGE:
                raise ReconcileError("invalid fill activity page")
            pages.append(page_raw)
            for item in page:
                identity = item.get("id") if isinstance(item, dict) else None
                if (
                    not isinstance(identity, str) or not identity
                    or len(identity) > 256 or identity in seen
                ):
                    raise ReconcileError("fill activity pagination replay")
                seen.add(identity)
                items.append(item)
                token = identity
            if len(page) < _ALPACA_ACTIVITY_PAGE:
                return pages, items
        raise ReconcileError("fill activity page bound reached")

    account_raw, account = get("/v2/account")
    if not isinstance(account, dict) or account.get("id") != account_id:
        raise ReconcileError("paper account identity mismatch")
    orders_raw, orders = get("/v2/orders?" + query)
    positions_raw, positions = get("/v2/positions")
    fill_pages, fills = get_fills()
    repeat_orders_raw, repeat_orders = get("/v2/orders?" + query)
    repeat_positions_raw, repeat_positions = get("/v2/positions")
    repeat_fill_pages, repeat_fills = get_fills()
    _, account_again = get("/v2/account")
    if not isinstance(account_again, dict) or account_again.get("id") != account_id:
        raise ReconcileError("paper account changed")
    if not isinstance(orders, list) or len(orders) >= _ALPACA_LIMIT:
        raise ReconcileError("paper order export may be truncated")
    if (
        not isinstance(positions, list) or orders != repeat_orders
        or positions != repeat_positions or fills != repeat_fills
    ):
        raise ReconcileError("paper venue state changed during collection")
    observed = [_alpaca_order(row, session_date, now) for row in orders]
    if len({row.client_id for row in observed}) != len(observed) or len({
        row["id"] for row in orders
    }) != len(orders):
        raise ReconcileError("duplicate paper venue order identity")
    detailed_fills = _validate_fill_activities(orders, observed, fills, session_date, now)
    normalized_positions: list[dict[str, Any]] = []
    for row in positions:
        if not isinstance(row, dict) or row.get("asset_class") != "us_option":
            raise ReconcileError("paper account contains unsupported positions")
        if row.get("side") != "long":
            raise ReconcileError("paper account contains unsupported short position")
        normalized_positions.append(VenuePosition(
            _alpaca_osi(row.get("symbol")), _integer(row.get("qty")),
        ).to_dict())
    if len({row["contract_key"] for row in normalized_positions}) != len(normalized_positions):
        raise ReconcileError("duplicate paper venue position")
    finished = clock()
    if finished.tzinfo is None or finished.utcoffset() is None or (
        not now <= finished <= now + timedelta(seconds=60)
        or et_date(finished) != session_date
    ):
        raise ReconcileError("paper snapshot collection time exceeded")
    normalized = {
        "schema": "scorpion.paper-venue-export.v1", "authority": "PAPER_ONLY",
        "source": "alpaca-paper-rest", "input_sha256": input_sha256,
        "session_date": session_date.isoformat(), "as_of_ts": now.timestamp(),
        "complete_order_export": True, "positions": normalized_positions,
        "fill_activity_reconciled": True,
        "fill_activities": [{
            **asdict(row), "price": str(row.price),
            "transaction_ts_utc": row.transaction_ts_utc.isoformat(),
        } for row in detailed_fills],
        "orders": [{
            **asdict(row), "limit": str(row.limit),
            "average_price": str(row.average_price) if row.average_price is not None else None,
        } for row in observed],
    }
    # Preserve exact response bytes privately. Account payload is sensitive; retain
    # only the validated account-ID digest rather than balances or account PII.
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    artifact_hashes: dict[str, str] = {}
    for name, raw in (
        ("orders.json", orders_raw), ("positions.json", positions_raw),
        ("orders-recheck.json", repeat_orders_raw),
        ("positions-recheck.json", repeat_positions_raw),
        ("normalized.json", json.dumps(normalized, sort_keys=True, allow_nan=False).encode()),
        *((f"fill-page-{i:02d}.json", raw) for i, raw in enumerate(fill_pages)),
        *((f"fill-recheck-{i:02d}.json", raw) for i, raw in enumerate(repeat_fill_pages)),
    ):
        with (destination / name).open("xb") as handle:
            os.chmod(destination / name, 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        artifact_hashes[name] = hashlib.sha256(raw).hexdigest()
    report = {
        "authority": "RESEARCH_ONLY", "source": "alpaca-paper-rest",
        "account_sha256": hashlib.sha256(account_id.encode()).hexdigest(),
        "account_response_sha256": hashlib.sha256(account_raw).hexdigest(),
        "captured_orders": len(observed), "captured_positions": len(normalized_positions),
        "captured_fill_activities": len(detailed_fills), "fill_activity_reconciled": True,
        "artifacts": artifact_hashes, "as_of_ts": now.timestamp(),
        "certifies_live_fills": False, "snapshot_atomic": False,
    }
    with (destination / "collection.json").open("x", encoding="utf-8") as manifest:
        os.chmod(destination / "collection.json", 0o600)
        json.dump(report, manifest, sort_keys=True)
        manifest.flush()
        os.fsync(manifest.fileno())
    return report
