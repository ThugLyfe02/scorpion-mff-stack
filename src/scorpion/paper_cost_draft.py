"""Deterministic offline cost evidence assembly; independent raw sources required.

No fee inference, market data download, order submission or signing is performed.
The normalized input is an attestation, not proof of vendor authenticity.
"""
from __future__ import annotations

import hashlib
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .causal_features import PaperIntentFeatureRow, load_paper_intent_features
from .config import MARKET_TZ
from .execution_attribution import (
    PaperExecutionCostReport,
    _load_cost_policy,
    _nonnegative_money,
    build_paper_execution_cost_report,
)
from .execution_meta_labels import (
    PaperOutcomeAdmission,
    PaperOutcomeLabel,
    _bounded_bytes,
    _fields,
    _price,
    _stamp,
    _text,
    _unique_json,
)
from .paper_evidence_collector import _private_write, verify_reviewed_outcomes
from .provenance import canonical_json
from .session_calendar import session_hours

_MAX_QUOTES = 100_000
_MAX_ORDERS = 500
_MAX_INPUT = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    identity: str
    ticker: str
    event: datetime
    received: datetime
    bid: Decimal | None
    ask: Decimal | None
    kind: str


@dataclass(frozen=True, slots=True)
class CostDraftReceipt:
    source_sha256: str
    fee_source_sha256: str
    quote_source_sha256: str
    draft_sha256: str
    outcome_export_sha256: str
    expected_routes: int
    fee_rows: int
    mark_rows: int
    withheld: tuple[tuple[str, str], ...]
    authority: str = "UNSIGNED_RESEARCH_DRAFT"
    certifies_economics: bool = False
    authorizes_training: bool = False
    authorizes_orders: bool = False


def _pin(path: Path, pin: str, *, limit: int = _MAX_INPUT) -> bytes:
    raw = _bounded_bytes(path, limit)
    if not raw or hashlib.sha256(raw).hexdigest() != pin:
        raise ValueError("cost source pin mismatch")
    return raw


def _quote_index(
    values: object, *, through: datetime,
) -> dict[str, tuple[list[datetime], list[QuoteEvent]]]:
    if not isinstance(values, list) or len(values) > _MAX_QUOTES:
        raise ValueError("cost quote roster bound")
    result: dict[str, tuple[list[datetime], list[QuoteEvent]]] = {}
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("cost quote row")
        _fields(value, {"quote_id", "ticker", "event_ts_utc", "received_ts_utc",
                        "kind", "bid", "ask"})
        identity, ticker = _text(value["quote_id"]), _text(value["ticker"])
        event, received = _stamp(value["event_ts_utc"]), _stamp(value["received_ts_utc"])
        kind = _text(value["kind"])
        if identity in seen or not event <= received <= through:
            raise ValueError("cost quote identity or chronology")
        seen.add(identity)
        if kind == "QUOTE":
            bid, ask = _price(value["bid"]), _price(value["ask"])
            if ask < bid:
                raise ValueError("crossed cost quote")
        elif kind in ("GAP", "WITHDRAWAL"):
            if value["bid"] is not None or value["ask"] is not None:
                raise ValueError("cost invalidation must not carry prices")
            bid = ask = None
        else:
            raise ValueError("cost quote event kind")
        clocks, rows = result.setdefault(ticker, ([], []))
        if rows and (received < rows[-1].received or event < rows[-1].event):
            raise ValueError("cost quote stream regression")
        clocks.append(received)
        rows.append(QuoteEvent(identity, ticker, event, received, bid, ask, kind))
    return result


def _select_mark(
    index: dict[str, tuple[list[datetime], list[QuoteEvent]]], *,
    ticker: str, initial: datetime, target: datetime, max_age_ms: int,
) -> QuoteEvent | None:
    clocks, rows = index.get(ticker, ([], []))
    i = bisect_right(clocks, target)-1
    if i < 0:
        return None
    row = rows[i]
    if (
        row.kind != "QUOTE" or row.bid is None or row.ask is None
        or not initial <= row.event <= row.received <= target
        or target-row.event > timedelta(milliseconds=max_age_ms)
    ):
        return None
    return row


def _route_closed(
    first: PaperOutcomeLabel, chase: PaperOutcomeLabel | None, target: datetime,
) -> bool:
    # Unknown initial orders, rejection and zero-fill paths with no independently
    # observed chase do not prove closure. Do not turn abstention into zero cost.
    if (
        first.observed_fill_target is None or first.terminal_ts_utc is None
        or first.terminal_ts_utc > target
    ):
        return False
    if first.observed_fill_target > 0:
        return chase is None or chase.venue_order_id is None
    return (
        chase is not None and chase.observed_fill_target is not None
        and chase.terminal_ts_utc is not None and chase.terminal_ts_utc <= target
    )


def _material(
    admission: PaperOutcomeAdmission, *, outcome_draft: Path, outcome_draft_pin: str,
    cost_policy: Path, cost_policy_pin: str, source: Path, source_pin: str,
    fee_source: Path, quote_source: Path,
) -> tuple[dict[str, object], CostDraftReceipt]:
    outcome = verify_reviewed_outcomes(
        admission, draft_path=outcome_draft, draft_pin=outcome_draft_pin,
    )
    evidence = load_paper_intent_features(admission.journal_path)
    if (
        evidence.journal_sha256 != outcome.journal_sha256
        or evidence.refusals or outcome.extra_feature_ids or outcome.feature_refusals
    ):
        raise ValueError("cost outcome feature coverage conflict")
    policy = _load_cost_policy(
        _bounded_bytes(cost_policy, 65536), cost_policy_pin, outcome.trust_sha256,
    )
    data = _unique_json(_pin(source, source_pin))
    _fields(data, {
        "schema", "authority", "account_ref", "market_source_id", "journal_sha256",
        "outcome_export_sha256", "fee_source_sha256", "quote_source_sha256",
        "observed_through_ts_utc", "collected_ts_utc", "complete_fee_export",
        "fees", "quotes", "session_coverage",
    })
    if (
        data["schema"] != "scorpion.paper-cost-source.v1"
        or data["authority"] != "RESEARCH_ONLY"
        or data["account_ref"] != outcome.account_ref
        or data["market_source_id"] != policy.market_source_id
        or policy.market_source_id == outcome.source_id
        or data["journal_sha256"] != outcome.journal_sha256
        or data["outcome_export_sha256"] != outcome.export_sha256
        or data["complete_fee_export"] is not True
    ):
        raise ValueError("cost normalized source scope")
    fee_pin, quote_pin = _text(data["fee_source_sha256"]), _text(data["quote_source_sha256"])
    _pin(fee_source, fee_pin)
    _pin(quote_source, quote_pin)
    identities = (source_pin, fee_pin, quote_pin, outcome.journal_sha256,
                  outcome.export_sha256, outcome.source_artifact_sha256)
    if len(set(identities)) != len(identities):
        raise ValueError("cost source identity reuse")
    through, collected = _stamp(data["observed_through_ts_utc"]), _stamp(data["collected_ts_utc"])
    if (
        outcome.observed_through_ts_utc is None or outcome.reviewed_ts_utc is None
        or not policy.registered_ts_utc <= through <= collected <= admission.as_of
        or collected < outcome.reviewed_ts_utc
    ):
        raise ValueError("cost source chronology")
    labels = {label.feature_sha256: label for label in outcome.labels}
    if set(labels) != {row.feature_sha256 for row in evidence.rows}:
        raise ValueError("cost feature roster mismatch")
    actual = {label.venue_order_id: label for label in outcome.labels if label.venue_order_id}
    raw_fees = data["fees"]
    if not isinstance(raw_fees, list) or len(raw_fees) > _MAX_ORDERS:
        raise ValueError("cost fee roster bound")
    fees: dict[str, dict[str, object]] = {}
    for fee in raw_fees:
        if not isinstance(fee, dict):
            raise ValueError("cost fee row")
        _fields(fee, {"venue_order_id", "fees_usd", "rebates_usd"})
        order = _text(fee["venue_order_id"])
        if order not in actual or order in fees:
            raise ValueError("cost fee order identity")
        fees[order] = {
            "venue_order_id": order, "feature_sha256": actual[order].feature_sha256,
            "fees_usd": format(_nonnegative_money(fee["fees_usd"]), "f"),
            "rebates_usd": format(_nonnegative_money(fee["rebates_usd"]), "f"),
        }
    if set(fees) != set(actual):
        raise ValueError("cost fees missing; explicit charges and rebates required")
    index = _quote_index(data["quotes"], through=through)
    by_call: dict[str, list[PaperIntentFeatureRow]] = defaultdict(list)
    for feature in evidence.rows:
        by_call[feature.call_id].append(feature)
    marks: list[dict[str, object]] = []
    withheld: list[tuple[str, str]] = []
    for call, rows in sorted(by_call.items()):
        firsts = [row for row in rows if row.role == "INITIAL"]
        chases = [row for row in rows if row.role == "CHASE"]
        if len(firsts) != 1 or len(chases) > 1:
            raise ValueError("cost route roster conflict")
        first = firsts[0]
        initial = _stamp(first.features["intent_ts_utc"])
        target = initial + timedelta(seconds=policy.horizon_seconds)
        hours = session_hours(initial.astimezone(MARKET_TZ).date())
        if (
            hours is None or not hours[0] <= initial <= target < hours[1]
            or policy.registered_ts_utc > _stamp(first.features["source_ts_utc"])
            or target > min(through, outcome.observed_through_ts_utc)
        ):
            withheld.append((call, "HORIZON_NOT_QUALIFIED"))
            continue
        if not _route_closed(
            labels[first.feature_sha256], labels[chases[0].feature_sha256] if chases else None,
            target,
        ):
            withheld.append((call, "ROUTE_CLOSURE_UNSUBSTANTIATED"))
            continue
        mark = _select_mark(
            index, ticker=first.ticker, initial=initial, target=target,
            max_age_ms=policy.max_mark_age_ms,
        )
        if mark is None:
            withheld.append((call, "CAUSAL_MARK_UNAVAILABLE"))
            continue
        marks.append({
            "call_id": call, "initial_feature_sha256": first.feature_sha256,
            "ticker": first.ticker, "quote_id": mark.identity, "target_ts_utc": target,
            "event_ts_utc": mark.event, "received_ts_utc": mark.received,
            "bid": format(mark.bid, "f") if mark.bid is not None else None,
            "ask": format(mark.ask, "f") if mark.ask is not None else None,
            "route_closed": True,
        })
    material: dict[str, object] = {
        "schema": "scorpion.paper-cost-export.v1", "authority": "PAPER_ONLY",
        "trust_sha256": outcome.trust_sha256, "cost_policy_sha256": cost_policy_pin,
        "journal_sha256": outcome.journal_sha256, "outcome_export_sha256": outcome.export_sha256,
        "market_source_id": policy.market_source_id, "market_artifact_sha256": source_pin,
        "complete_fee_export": True, "observed_through_ts_utc": through,
        "collected_ts_utc": collected, "reviewed_ts_utc": None,
        "fees": [fees[key] for key in sorted(fees)], "marks": marks,
    }
    coverage = data["session_coverage"]
    if coverage is not None:
        if not isinstance(coverage, dict):
            raise ValueError("cost coverage declaration")
        # Never derive a full-session attestation from local receipt alone. It
        # remains independently reviewed and is checked by Stage 2 admission.
        material["session_coverage"] = coverage
    raw = canonical_json(material).encode()
    receipt = CostDraftReceipt(
        source_pin, fee_pin, quote_pin, hashlib.sha256(raw).hexdigest(), outcome.export_sha256,
        len(by_call), len(fees), len(marks), tuple(withheld),
    )
    return material, receipt


def prepare_cost_draft(
    admission: PaperOutcomeAdmission, *, outcome_draft: Path, outcome_draft_pin: str,
    cost_policy: Path, cost_policy_pin: str, source: Path, source_pin: str,
    fee_source: Path, quote_source: Path, output: Path,
) -> CostDraftReceipt:
    material, receipt = _material(
        admission, outcome_draft=outcome_draft, outcome_draft_pin=outcome_draft_pin,
        cost_policy=cost_policy, cost_policy_pin=cost_policy_pin, source=source,
        source_pin=source_pin, fee_source=fee_source, quote_source=quote_source,
    )
    _private_write(output, canonical_json(material).encode())
    return receipt


def verify_cost_draft(
    admission: PaperOutcomeAdmission, *, outcome_draft: Path, outcome_draft_pin: str,
    cost_policy: Path, cost_policy_pin: str, source: Path, source_pin: str,
    fee_source: Path, quote_source: Path, draft: Path, draft_pin: str,
    reviewed: Path, collector_signature: str, reviewer_signature: str,
) -> PaperExecutionCostReport:
    """Rebuild deterministic content, then apply the existing dual-signature gate."""
    material, receipt = _material(
        admission, outcome_draft=outcome_draft, outcome_draft_pin=outcome_draft_pin,
        cost_policy=cost_policy, cost_policy_pin=cost_policy_pin, source=source,
        source_pin=source_pin, fee_source=fee_source, quote_source=quote_source,
    )
    frozen = _pin(draft, draft_pin)
    if draft_pin != receipt.draft_sha256 or frozen != canonical_json(material).encode():
        raise ValueError("cost frozen draft differs from independently pinned sources")
    raw = _bounded_bytes(reviewed)
    final = _unique_json(raw)
    if (
        canonical_json({**final, "reviewed_ts_utc": None}) != canonical_json(material)
        or _stamp(final.get("reviewed_ts_utc")) < _stamp(final.get("collected_ts_utc"))
    ):
        raise ValueError("review modified frozen cost content")
    report = build_paper_execution_cost_report(
        admission, cost_policy_path=cost_policy, expected_policy_sha256=cost_policy_pin,
        cost_export_path=reviewed, market_artifact_path=source,
        collector_signature=collector_signature, reviewer_signature=reviewer_signature,
    )
    if (
        report.cost_export_sha256 != hashlib.sha256(raw).hexdigest()
        or report.market_artifact_sha256 != source_pin
        or report.outcome_export_sha256 != receipt.outcome_export_sha256
    ):
        raise ValueError("cost evidence changed during admission")
    return report
