from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from pathlib import Path

from .causal_features import PaperIntentFeatureRow, load_paper_intent_features
from .domain import EventKind
from .execution_attribution import build_execution_attribution_report
from .microstructure import OptionMicrostructureTape
from .microstructure_forensics import (
    MicroForensicStatus,
    MicrostructureForensicsReport,
)
from .provenance import stable_hash


class ExecutionMetaOutcome(StrEnum):
    CERTIFIED_PROFITABLE = "CERTIFIED_PROFITABLE"
    CERTIFIED_NONPOSITIVE = "CERTIFIED_NONPOSITIVE"
    CERTIFIED_EARLY_TOXIC = "CERTIFIED_EARLY_TOXIC"
    EXECUTION_UNCERTAIN = "EXECUTION_UNCERTAIN"
    NO_CERTIFIED_ENTRY = "NO_CERTIFIED_ENTRY"
    INCOMPLETE_LIFECYCLE = "INCOMPLETE_LIFECYCLE"
    RESEARCH_ONLY = "RESEARCH_ONLY"


@dataclass(frozen=True, slots=True)
class ExecutionMetaLabelPolicy:
    markout_horizon_ms: int = 500
    adverse_markout_threshold_bps: float = -50.0
    profitable_return_threshold: float = 0.0

    def __post_init__(self) -> None:
        if self.markout_horizon_ms <= 0:
            raise ValueError("markout_horizon_ms must be positive")


@dataclass(frozen=True, slots=True)
class ExecutionMetaLabel:
    event_id: str
    contract_key: str
    outcome: ExecutionMetaOutcome
    certified_entry_quantity: int
    possible_entry_quantity: int
    completed_lifecycle: bool
    realized_return_fraction: float | None
    markout_horizon_ms: int
    signed_markout_bps: float | None
    source_status: str
    reason: str

    @property
    def positive_shadow_target(self) -> bool:
        return self.outcome is ExecutionMetaOutcome.CERTIFIED_PROFITABLE


_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_meta_labels (
    event_id TEXT NOT NULL,
    research_manifest_hash TEXT NOT NULL,
    contract_key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    PRIMARY KEY(event_id,research_manifest_hash)
)
"""


def build_execution_meta_labels(
    report: MicrostructureForensicsReport,
    tape: OptionMicrostructureTape,
    *,
    policy: ExecutionMetaLabelPolicy | None = None,
) -> tuple[ExecutionMetaLabel, ...]:
    policy = policy or ExecutionMetaLabelPolicy()
    _, attributed = build_execution_attribution_report(
        report,
        tape,
        horizons_ms=(policy.markout_horizon_ms,),
    )
    attribution_by_event = {item.event_id: item for item in attributed}
    completed_by_entry = {trade.entry_event_id: trade for trade in report.completed_trades}
    labels: list[ExecutionMetaLabel] = []

    for leg in report.legs:
        if leg.event_kind is not EventKind.ENTRY or leg.contract_key is None:
            continue
        completed = completed_by_entry.get(leg.event_id)
        attribution = attribution_by_event.get(leg.event_id)
        markout: float | None = None
        if attribution is not None and attribution.markouts:
            markout = attribution.markouts[0].signed_markout_bps

        if leg.status is MicroForensicStatus.RESEARCH_ONLY:
            outcome = ExecutionMetaOutcome.RESEARCH_ONLY
            reason = "strategy bucket was research-only"
        elif leg.conservative_fill_quantity <= 0 and leg.possible_fill_quantity > 0:
            outcome = ExecutionMetaOutcome.EXECUTION_UNCERTAIN
            reason = "entry may have filled but queue/depth evidence cannot certify it"
        elif leg.conservative_fill_quantity <= 0:
            outcome = ExecutionMetaOutcome.NO_CERTIFIED_ENTRY
            reason = "no conservative entry fill was supported by market evidence"
        elif completed is None:
            outcome = ExecutionMetaOutcome.INCOMPLETE_LIFECYCLE
            reason = "entry was certified but the full source lifecycle was not reconstructible"
        elif markout is not None and markout < policy.adverse_markout_threshold_bps:
            outcome = ExecutionMetaOutcome.CERTIFIED_EARLY_TOXIC
            reason = (
                f"certified fill had {markout:.2f} bps signed markout at "
                f"{policy.markout_horizon_ms}ms"
            )
        elif float(completed.return_fraction) > policy.profitable_return_threshold:
            outcome = ExecutionMetaOutcome.CERTIFIED_PROFITABLE
            reason = "certified entry and complete source lifecycle produced positive realized return"
        else:
            outcome = ExecutionMetaOutcome.CERTIFIED_NONPOSITIVE
            reason = "certified entry and complete source lifecycle were non-positive"

        labels.append(
            ExecutionMetaLabel(
                event_id=leg.event_id,
                contract_key=leg.contract_key,
                outcome=outcome,
                certified_entry_quantity=leg.conservative_fill_quantity,
                possible_entry_quantity=leg.possible_fill_quantity,
                completed_lifecycle=completed is not None,
                realized_return_fraction=(
                    float(completed.return_fraction) if completed is not None else None
                ),
                markout_horizon_ms=policy.markout_horizon_ms,
                signed_markout_bps=markout,
                source_status=leg.status.value,
                reason=reason,
            )
        )
    return tuple(labels)


def persist_execution_meta_labels(
    path: str | Path,
    labels: tuple[ExecutionMetaLabel, ...],
    *,
    research_manifest_hash: str,
) -> int:
    if not research_manifest_hash.strip():
        raise ValueError("research_manifest_hash is required")
    created = datetime.now(UTC).isoformat()
    inserted = 0
    with sqlite3.connect(str(path)) as db:
        db.execute(_SCHEMA)
        for label in labels:
            payload = json.dumps(asdict(label), sort_keys=True, separators=(",", ":"))
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO execution_meta_labels
                (event_id,research_manifest_hash,contract_key,outcome,payload_json,created_ts_utc)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    label.event_id,
                    research_manifest_hash,
                    label.contract_key,
                    label.outcome.value,
                    payload,
                    created,
                ),
            )
            inserted += int(cursor.rowcount > 0)
        db.commit()
    return inserted


def load_execution_meta_labels(
    path: str | Path,
    *,
    research_manifest_hash: str,
) -> tuple[ExecutionMetaLabel, ...]:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.execute(_SCHEMA)
        rows = db.execute(
            """
            SELECT payload_json FROM execution_meta_labels
            WHERE research_manifest_hash=? ORDER BY event_id
            """,
            (research_manifest_hash,),
        ).fetchall()
    result: list[ExecutionMetaLabel] = []
    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        result.append(
            ExecutionMetaLabel(
                event_id=str(payload["event_id"]),
                contract_key=str(payload["contract_key"]),
                outcome=ExecutionMetaOutcome(str(payload["outcome"])),
                certified_entry_quantity=int(payload["certified_entry_quantity"]),
                possible_entry_quantity=int(payload["possible_entry_quantity"]),
                completed_lifecycle=bool(payload["completed_lifecycle"]),
                realized_return_fraction=(
                    float(payload["realized_return_fraction"])
                    if payload["realized_return_fraction"] is not None
                    else None
                ),
                markout_horizon_ms=int(payload["markout_horizon_ms"]),
                signed_markout_bps=(
                    float(payload["signed_markout_bps"])
                    if payload["signed_markout_bps"] is not None
                    else None
                ),
                source_status=str(payload["source_status"]),
                reason=str(payload["reason"]),
            )
        )
    return tuple(result)


# The existing forensic labels above describe historical modeled evidence. The following
# boundary admits independent paper observations without promoting either class to live truth.
_MAX_EXPORT_BYTES = 32 * 1024 * 1024
_COLLECTOR_DOMAIN = b"scorpion.paper-outcome-export.v1:collector\x00"
_REVIEWER_DOMAIN = b"scorpion.paper-outcome-export.v1:reviewer\x00"
_PRICE_PATTERN = re.compile(r"[0-9]{1,10}(?:\.[0-9]{1,8})?")


class PaperOutcomeError(ValueError):
    """Evidence cannot be used for a paper outcome label."""


class PaperOutcome(StrEnum):
    FULL_FILL = "FULL_FILL"
    PARTIAL_FILL = "PARTIAL_FILL"
    CANCELLED_NO_FILL = "CANCELLED_NO_FILL"
    EXPIRED_NO_FILL = "EXPIRED_NO_FILL"
    REJECTED = "REJECTED"
    CENSORED = "CENSORED"
    UNOBSERVED = "UNOBSERVED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class PaperOutcomeLabel:
    feature_sha256: str
    intent_seq: int
    call_id: str
    ticker: str
    role: str
    outcome: PaperOutcome
    reason: str
    requested_quantity: int
    filled_quantity: int | None
    average_fill_price: Decimal | None
    venue_order_id: str | None
    available_ts_utc: datetime | None
    label_sha256: str
    authority: str = "RESEARCH_ONLY"
    evidence_kind: str = "DUAL_ATTESTED_EXTERNAL_PAPER"
    filled_notional_usd: Decimal | None = None
    terminal_ts_utc: datetime | None = None
    submitted_ts_utc: datetime | None = None

    @property
    def observed_fill_target(self) -> int | None:
        """A rejected/unobserved order is not evidence of zero market liquidity."""
        if self.outcome in (
            PaperOutcome.FULL_FILL, PaperOutcome.PARTIAL_FILL,
            PaperOutcome.CANCELLED_NO_FILL, PaperOutcome.EXPIRED_NO_FILL,
        ):
            return self.filled_quantity
        return None


@dataclass(frozen=True, slots=True)
class PaperOutcomeReport:
    journal_sha256: str
    trust_sha256: str
    export_sha256: str
    source_artifact_sha256: str
    source_id: str
    account_ref: str
    collector_id: str
    reviewer_id: str
    as_of_ts_utc: datetime
    labels: tuple[PaperOutcomeLabel, ...]
    feature_refusals: tuple[tuple[str, int], ...]
    extra_feature_ids: tuple[str, ...]
    outcome_counts: tuple[tuple[str, int], ...]
    observed_fill_targets: int
    report_sha256: str
    authority: str = "RESEARCH_ONLY"
    certifies_live_fills: bool = False
    authorizes_training: bool = False
    authorizes_promotion: bool = False
    observed_through_ts_utc: datetime | None = None
    reviewed_ts_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class _PaperTrust:
    source_id: str
    account_ref: str
    rules_sha256: str
    registered: datetime
    horizon_seconds: int
    collector_id: str
    collector_key: bytes
    reviewer_id: str
    reviewer_key: bytes


@dataclass(frozen=True, slots=True)
class _PaperOrder:
    feature_hash: str
    order_id: str
    call_id: str
    ticker: str
    role: str
    intent_seq: int
    quantity: int
    limit: Decimal
    state: str
    submitted: datetime
    terminal: datetime | None
    fills: tuple[tuple[str, int, Decimal, datetime], ...]


def _bounded_bytes(path: str | Path, limit: int = _MAX_EXPORT_BYTES) -> bytes:
    with Path(path).open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise PaperOutcomeError("evidence_size_bound")
    return data


def _unique_json(raw: bytes) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise PaperOutcomeError("duplicate_json_key")
            result[key] = value
        return result

    def constant(value: str) -> object:
        raise PaperOutcomeError("nonfinite_json_constant")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, PaperOutcomeError):
            raise
        raise PaperOutcomeError("invalid_evidence_json") from exc
    if not isinstance(value, dict):
        raise PaperOutcomeError("expected_evidence_object")
    return value


def _fields(value: dict[str, object], expected: set[str]) -> None:
    if value.keys() != expected:
        raise PaperOutcomeError("evidence_schema_fields")


def _text(value: object) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > 256 or any(ord(char) < 32 for char in value)
    ):
        raise PaperOutcomeError("invalid_evidence_identity")
    return value


def _hex(value: object, length: int) -> bytes:
    if (
        not isinstance(value, str) or len(value) != length * 2
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise PaperOutcomeError("invalid_evidence_hex")
    return bytes.fromhex(value)


def _stamp(value: object) -> datetime:
    try:
        stamp = datetime.fromisoformat(_text(value))
    except ValueError as exc:
        raise PaperOutcomeError("invalid_evidence_timestamp") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise PaperOutcomeError("naive_evidence_timestamp")
    return stamp.astimezone(UTC)


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 1_000_000_000:
        raise PaperOutcomeError("invalid_evidence_quantity")
    return value


def _price(value: object) -> Decimal:
    if not isinstance(value, str) or not _PRICE_PATTERN.fullmatch(value):
        raise PaperOutcomeError("invalid_evidence_price")
    price = Decimal(value)
    if price <= 0:
        raise PaperOutcomeError("invalid_evidence_price")
    return price


def _parse_trust(raw: bytes, expected_hash: str) -> _PaperTrust:
    _hex(expected_hash, 32)
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise PaperOutcomeError("trust_pin_mismatch")
    data = _unique_json(raw)
    _fields(data, {
        "schema", "authority", "source_id", "account_ref", "rules_sha256",
        "registered_ts_utc", "horizon_seconds", "collector_id", "collector_public_key",
        "reviewer_id", "reviewer_public_key",
    })
    if data["schema"] != "scorpion.paper-outcome-trust.v1" or data["authority"] != "PAPER_ONLY":
        raise PaperOutcomeError("trust_scope_mismatch")
    collector, reviewer = _text(data["collector_id"]), _text(data["reviewer_id"])
    collector_key = _hex(data["collector_public_key"], 32)
    reviewer_key = _hex(data["reviewer_public_key"], 32)
    if collector == reviewer or collector_key == reviewer_key:
        raise PaperOutcomeError("independent_signers_required")
    horizon = _count(data["horizon_seconds"])
    if horizon > 86400:
        raise PaperOutcomeError("observation_horizon_bound")
    return _PaperTrust(
        _text(data["source_id"]), _text(data["account_ref"]),
        _hex(data["rules_sha256"], 32).hex(), _stamp(data["registered_ts_utc"]),
        horizon, collector, collector_key, reviewer, reviewer_key,
    )


def _verify_signatures(
    trust: _PaperTrust, raw: bytes, collector_signature: str, reviewer_signature: str,
    *, collector_domain: bytes = _COLLECTOR_DOMAIN, reviewer_domain: bytes = _REVIEWER_DOMAIN,
) -> None:
    # Research-only optional dependency; never imported by the decision hot path.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(trust.collector_key).verify(
            _hex(collector_signature, 64), collector_domain + raw,
        )
        Ed25519PublicKey.from_public_bytes(trust.reviewer_key).verify(
            _hex(reviewer_signature, 64), reviewer_domain + raw,
        )
    except (InvalidSignature, ValueError) as exc:
        raise PaperOutcomeError("paper_export_signature_invalid") from exc


def _parse_order(value: object) -> _PaperOrder:
    if not isinstance(value, dict):
        raise PaperOutcomeError("invalid_paper_order")
    _fields(value, {
        "feature_sha256", "venue_order_id", "call_id", "ticker", "role",
        "intent_seq", "requested_quantity", "limit", "state", "submitted_ts_utc",
        "terminal_ts_utc", "fills", "asset_class", "side", "currency", "quantity_unit",
    })
    if (
        value["asset_class"] != "EQUITY" or value["side"] != "BUY"
        or value["currency"] != "USD" or value["quantity_unit"] != "SHARES"
    ):
        raise PaperOutcomeError("paper_order_asset_scope_mismatch")
    if not isinstance(value["fills"], list):
        raise PaperOutcomeError("invalid_fill_list")
    fills: list[tuple[str, int, Decimal, datetime]] = []
    for fill in value["fills"]:
        if not isinstance(fill, dict):
            raise PaperOutcomeError("invalid_paper_fill")
        _fields(fill, {"fill_id", "quantity", "price", "filled_ts_utc"})
        fills.append((
            _text(fill["fill_id"]), _count(fill["quantity"]),
            _price(fill["price"]), _stamp(fill["filled_ts_utc"]),
        ))
    order = _PaperOrder(
        _hex(value["feature_sha256"], 32).hex(), _text(value["venue_order_id"]),
        _text(value["call_id"]), _text(value["ticker"]), _text(value["role"]),
        _count(value["intent_seq"]), _count(value["requested_quantity"]),
        _price(value["limit"]), _text(value["state"]), _stamp(value["submitted_ts_utc"]),
        None if value["terminal_ts_utc"] is None else _stamp(value["terminal_ts_utc"]),
        tuple(fills),
    )
    if order.role not in ("INITIAL", "CHASE"):
        raise PaperOutcomeError("invalid_paper_order_role")
    if order.state not in (
        "FILLED", "CANCELLED", "EXPIRED", "REJECTED", "WORKING", "PARTIAL", "UNKNOWN",
    ):
        raise PaperOutcomeError("invalid_paper_order_state")
    return order


def _observe(
    feature: PaperIntentFeatureRow, order: _PaperOrder, trust: _PaperTrust,
    observed_through: datetime,
) -> tuple[PaperOutcome, str, int | None, Decimal | None]:
    def abstain(reason: str) -> tuple[PaperOutcome, str, None, None]:
        return PaperOutcome.QUARANTINED, reason, None, None

    intent_time = _stamp(feature.features["intent_ts_utc"])
    deadline = intent_time + timedelta(seconds=trust.horizon_seconds)
    if (
        feature.rules_sha256 != trust.rules_sha256 or trust.registered > intent_time
        or (feature.call_id, feature.ticker, feature.role, feature.intent_seq)
        != (order.call_id, order.ticker, order.role, order.intent_seq)
        or order.quantity != feature.features["quantity"]
        or order.limit != Decimal(str(feature.features["limit"]))
    ):
        return abstain("intent_binding_mismatch")
    if not intent_time <= order.submitted <= observed_through:
        return abstain("submission_chronology")
    terminal = order.state in ("FILLED", "CANCELLED", "EXPIRED", "REJECTED")
    if terminal != (order.terminal is not None):
        return abstain("terminal_state_conflict")
    if order.terminal is not None and not order.submitted <= order.terminal <= observed_through:
        return abstain("terminal_chronology")
    total = sum(fill[1] for fill in order.fills)
    if total > order.quantity:
        return abstain("overfill")
    for _, _, price, when in order.fills:
        if not order.submitted <= when <= (order.terminal or observed_through):
            return abstain("fill_chronology")
        # Forward equity entries are buys; a buy fill above its bound is a discrepancy.
        if price > order.limit:
            return abstain("limit_violation")
    if (
        (order.state == "FILLED" and total != order.quantity)
        or (order.state != "FILLED" and total == order.quantity)
        or (order.state in ("REJECTED", "WORKING") and total)
        or (order.state == "PARTIAL" and not total)
    ):
        return abstain("fill_state_conflict")
    if (
        observed_through < deadline or order.submitted > deadline
        or order.terminal is None or order.terminal > deadline
    ):
        return PaperOutcome.CENSORED, "terminal_horizon_not_observed", None, None
    # Keep legacy label identity deterministic even inside a high-precision cost calculation.
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        average = sum((price * qty for _, qty, price, _ in order.fills), Decimal(0))
        avg = average / total if total else None
    if order.state == "REJECTED":
        return PaperOutcome.REJECTED, "explicit_venue_rejection", 0, None
    if total == order.quantity:
        return PaperOutcome.FULL_FILL, "terminal_full_fill", total, avg
    if total:
        return PaperOutcome.PARTIAL_FILL, "terminal_partial_fill", total, avg
    outcome = (
        PaperOutcome.CANCELLED_NO_FILL if order.state == "CANCELLED"
        else PaperOutcome.EXPIRED_NO_FILL
    )
    return outcome, "explicit_terminal_zero_fill", 0, None


def _exact_fill_notional(order: _PaperOrder) -> Decimal:
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        return sum((qty * px for _, qty, px, _ in order.fills), Decimal(0))


def admit_paper_outcome_labels(
    journal_path: str | Path, *,
    trust_path: str | Path, expected_trust_sha256: str,
    export_path: str | Path, source_artifact_path: str | Path,
    collector_signature: str, reviewer_signature: str, as_of: datetime,
) -> PaperOutcomeReport:
    """Bind dual-attested external paper outcomes to sealed point-in-time features.

    Keys and the trust-file hash must be established outside the optimizer and before
    observation. Signatures establish attestation, not vendor truth or independent custody.
    This function writes no training labels, money-path state, or brokerage requests.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise PaperOutcomeError("naive_as_of")
    as_of = as_of.astimezone(UTC)
    trust = _parse_trust(_bounded_bytes(trust_path, 65536), expected_trust_sha256)
    raw = _bounded_bytes(export_path)
    _verify_signatures(trust, raw, collector_signature, reviewer_signature)
    data = _unique_json(raw)
    _fields(data, {
        "schema", "authority", "source_id", "account_ref", "trust_sha256",
        "journal_sha256", "source_artifact_sha256", "complete_order_export",
        "observed_through_ts_utc", "collected_ts_utc", "reviewed_ts_utc", "orders",
    })
    evidence = load_paper_intent_features(journal_path)
    source_bytes = _bounded_bytes(source_artifact_path)
    if not source_bytes:
        raise PaperOutcomeError("empty_source_artifact")
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    export_hash = hashlib.sha256(raw).hexdigest()
    if (
        data["schema"] != "scorpion.paper-outcome-export.v1"
        or data["authority"] != "PAPER_ONLY"
        or data["source_id"] != trust.source_id or data["account_ref"] != trust.account_ref
        or data["trust_sha256"] != expected_trust_sha256
        or data["journal_sha256"] != evidence.journal_sha256
        or data["source_artifact_sha256"] != source_hash
        or data["complete_order_export"] is not True
    ):
        raise PaperOutcomeError("paper_export_scope_mismatch")
    if source_hash in (evidence.journal_sha256, export_hash):
        raise PaperOutcomeError("self_referential_source_artifact")
    through = _stamp(data["observed_through_ts_utc"])
    collected, reviewed = _stamp(data["collected_ts_utc"]), _stamp(data["reviewed_ts_utc"])
    if not trust.registered <= through <= collected <= reviewed <= as_of:
        raise PaperOutcomeError("paper_export_availability_mismatch")
    if not isinstance(data["orders"], list):
        raise PaperOutcomeError("invalid_paper_orders")
    orders: dict[str, _PaperOrder] = {}
    order_ids: set[str] = set()
    fill_ids: set[str] = set()
    for value in data["orders"]:
        parsed_order = _parse_order(value)
        if parsed_order.feature_hash in orders or parsed_order.order_id in order_ids:
            raise PaperOutcomeError("duplicate_venue_order_identity")
        orders[parsed_order.feature_hash] = parsed_order
        order_ids.add(parsed_order.order_id)
        for fill_id, *_ in parsed_order.fills:
            if fill_id in fill_ids:
                raise PaperOutcomeError("duplicate_venue_fill_identity")
            fill_ids.add(fill_id)
    features = {row.feature_sha256: row for row in evidence.rows}
    extras = tuple(sorted(orders.keys() - features.keys()))
    observed: dict[str, tuple[PaperOutcome, str, int | None, Decimal | None]] = {}
    for key, feature in features.items():
        order = orders.get(key)
        if order is None:
            observed[key] = PaperOutcome.UNOBSERVED, "missing_venue_order", None, None
        else:
            observed[key] = _observe(feature, order, trust, through)

    # A recorded chase is conditional, not an independent second order in the same route.
    initial_by_call = {f.call_id: key for key, f in features.items() if f.role == "INITIAL"}
    for key, feature in features.items():
        if feature.role != "CHASE" or key not in orders:
            continue
        prior_key = initial_by_call.get(feature.call_id)
        prior = orders.get(prior_key or "")
        prior_outcome = observed.get(prior_key or "")
        if (
            prior is None or prior_outcome is None or prior_outcome[0] not in (
                PaperOutcome.CANCELLED_NO_FILL, PaperOutcome.EXPIRED_NO_FILL,
            )
            or prior.terminal is None or prior.terminal > orders[key].submitted
        ):
            observed[key] = PaperOutcome.QUARANTINED, "chase_condition_unsubstantiated", None, None
            # If both were submitted, a known initial fill violates the exclusive route.
            if prior_key is not None and prior_outcome is not None and prior_outcome[2]:
                observed[prior_key] = (
                    PaperOutcome.QUARANTINED, "conflicting_chase_route", None, None,
                )

    labels: list[PaperOutcomeLabel] = []
    counts: dict[str, int] = {}
    for key, feature in sorted(features.items(), key=lambda item: item[1].intent_seq):
        outcome, reason, quantity, price = observed[key]
        order = orders.get(key)
        available = reviewed if quantity is not None else None
        material = {
            "version": "paper-outcome-label-v1", "feature_sha256": key,
            "journal_sha256": evidence.journal_sha256, "trust_sha256": expected_trust_sha256,
            "export_sha256": export_hash, "source_artifact_sha256": source_hash,
            "outcome": outcome.value, "reason": reason, "filled_quantity": quantity,
            "average_fill_price": price, "available_ts_utc": available,
            "venue_order_id": order.order_id if order else None,
        }
        label = PaperOutcomeLabel(
            key, feature.intent_seq, feature.call_id, feature.ticker, feature.role,
            outcome, reason, int(feature.features["quantity"]), quantity, price,
            order.order_id if order else None, available, stable_hash(material),
            filled_notional_usd=(
                _exact_fill_notional(order)
                if order is not None and quantity is not None else None
            ),
            terminal_ts_utc=order.terminal if order is not None and quantity is not None else None,
            submitted_ts_utc=order.submitted if order is not None and quantity is not None else None,
        )
        labels.append(label)
        counts[outcome.value] = counts.get(outcome.value, 0) + 1
    target_count = sum(label.observed_fill_target is not None for label in labels)
    report_hash = stable_hash({
        "version": "paper-outcome-report-v1", "journal_sha256": evidence.journal_sha256,
        "trust_sha256": expected_trust_sha256, "export_sha256": export_hash,
        "source_artifact_sha256": source_hash, "as_of": as_of,
        "labels": [label.label_sha256 for label in labels],
        "extra_feature_ids": extras, "feature_refusals": evidence.refusals,
    })
    return PaperOutcomeReport(
        evidence.journal_sha256, expected_trust_sha256, export_hash, source_hash,
        trust.source_id, trust.account_ref, trust.collector_id, trust.reviewer_id, as_of,
        tuple(labels), evidence.refusals, extras, tuple(sorted(counts.items())),
        target_count, report_hash, observed_through_ts_utc=through, reviewed_ts_utc=reviewed,
    )


@dataclass(frozen=True, slots=True)
class PaperOutcomeAdmission:
    """Operator-supplied inputs shared by outcome and economic research commands."""

    journal_path: Path
    trust_path: Path
    expected_trust_sha256: str
    export_path: Path
    source_artifact_path: Path
    collector_signature: str
    reviewer_signature: str
    as_of: datetime

    def verify(self) -> PaperOutcomeReport:
        return admit_paper_outcome_labels(
            self.journal_path, trust_path=self.trust_path,
            expected_trust_sha256=self.expected_trust_sha256, export_path=self.export_path,
            source_artifact_path=self.source_artifact_path,
            collector_signature=self.collector_signature, reviewer_signature=self.reviewer_signature,
            as_of=self.as_of,
        )

    def verify_cost_supplement(
        self, raw: bytes, collector_signature: str, reviewer_signature: str,
    ) -> None:
        """Reuse pinned Stage 1 identities with a distinct signature domain."""
        trust = _parse_trust(_bounded_bytes(self.trust_path, 65536), self.expected_trust_sha256)
        _verify_signatures(
            trust, raw, collector_signature, reviewer_signature,
            collector_domain=b"scorpion.paper-cost-export.v1:collector\x00",
            reviewer_domain=b"scorpion.paper-cost-export.v1:reviewer\x00",
        )
