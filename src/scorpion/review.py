"""The semantic review queue: turn advisory evidence into a durable human worklist.

The precision layer could compute calibration, novelty, contract and quote evidence, and the
calibrated layer could compose it into an envelope. None of that told an operator what to do
next. This module closes the loop:

* an actionable event whose evidence is weak becomes a durable review item, once, with a
  deterministic id and a priority derived from the same `active_learning` scorer;
* the item carries the parser evidence, the association confidence and the envelope that made
  it review-worthy, so a reviewer is not asked to re-derive why it is there;
* claim, resolve and dismiss are explicit state transitions over a single SQLite writer
  transaction, and a resolution writes the adjudication that calibrates future parses;
* the queue reports age, SLA, reason mix and resolution progress, because a backlog is a
  safety property in a system whose dangerous failure is silent automation;
* backfill ranks the existing unadjudicated corpus without inventing evidence for it.

The queue has no execution authority. It cannot mutate the book, approve an effect, size a
position or submit an order. It changes what a human sees, and the adjudication changes what the
next calibration says - nothing else.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .accuracy import DecisionEvidence
from .active_learning import ReviewCandidate, ReviewPriority, ReviewTier, prioritize
from .calibration import CalibrationStatus, calibrate_rules
from .domain import EventKind, SignalEvent
from .novelty import NoveltyIndex
from .semantic_envelope import (
    SemanticDisposition,
    SemanticEvidenceEnvelope,
    build_semantic_evidence_envelope,
)
from .store import Store

DEFAULT_SLA = {
    ReviewTier.URGENT: timedelta(minutes=5),
    ReviewTier.HIGH: timedelta(hours=1),
    ReviewTier.NORMAL: timedelta(hours=8),
    ReviewTier.LOW: timedelta(days=1),
}


class ReviewStatus(StrEnum):
    OPEN = "OPEN"
    CLAIMED = "CLAIMED"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class ReviewError(ValueError):
    """A review transition that cannot be performed, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class ReviewItem:
    review_id: str
    event_id: str
    message_id: str
    disposition: str
    priority_score: float
    tier: ReviewTier
    reason_codes: tuple[str, ...]
    status: ReviewStatus
    assignee: str
    evidence: dict[str, Any]
    created_ts_utc: str
    updated_ts_utc: str
    resolved_ts_utc: str | None = None
    resolution: str = ""

    @property
    def open(self) -> bool:
        return self.status in {ReviewStatus.OPEN, ReviewStatus.CLAIMED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "event_id": self.event_id,
            "message_id": self.message_id,
            "disposition": self.disposition,
            "priority_score": round(self.priority_score, 2),
            "tier": self.tier.value,
            "reason_codes": list(self.reason_codes),
            "status": self.status.value,
            "assignee": self.assignee,
            "created_ts_utc": self.created_ts_utc,
            "updated_ts_utc": self.updated_ts_utc,
            "resolved_ts_utc": self.resolved_ts_utc,
            "resolution": self.resolution,
            "evidence": self.evidence,
        }


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _review_id(event_id: str) -> str:
    return hashlib.sha256(f"scorpion.review/1|{event_id}".encode()).hexdigest()[:32]


def _row_to_item(row: dict[str, Any]) -> ReviewItem:
    return ReviewItem(
        review_id=str(row["review_id"]),
        event_id=str(row["event_id"]),
        message_id=str(row["message_id"]),
        disposition=str(row["disposition"]),
        priority_score=float(row["priority_score"]),
        tier=ReviewTier(row["tier"]),
        reason_codes=tuple(json.loads(row["reason_codes_json"])),
        status=ReviewStatus(row["status"]),
        assignee=str(row["assignee"]),
        evidence=json.loads(row["evidence_json"]),
        created_ts_utc=str(row["created_ts_utc"]),
        updated_ts_utc=str(row["updated_ts_utc"]),
        resolved_ts_utc=row["resolved_ts_utc"],
        resolution=str(row["resolution"]),
    )


def _candidate(
    parser: DecisionEvidence,
    envelope: SemanticEvidenceEnvelope,
    *,
    association_confidence: float,
) -> ReviewCandidate:
    reasons = set(envelope.reason_codes)
    return ReviewCandidate(
        event_id=envelope.event_id,
        parser_confidence=parser.confidence,
        association_confidence=association_confidence,
        novelty_score=envelope.novelty_score,
        ensemble_entropy=envelope.ensemble_entropy or 0.0,
        actionable_disagreement="ensemble_actionability_disagreement" in reasons,
        unseen_rule="calibration_uncalibrated" in reasons,
        contract_review="contract_semantics" in reasons,
        quote_review=bool(reasons & {"quote_semantics", "quote_invalid"}),
    )


def _envelope_payload(envelope: SemanticEvidenceEnvelope) -> dict[str, Any]:
    return {
        "disposition": envelope.disposition.value,
        "calibrated": {
            "rule_id": envelope.calibrated.rule_id,
            "stated_confidence": envelope.calibrated.stated_confidence,
            "effective_confidence": envelope.calibrated.effective_confidence,
            "lower_bound": envelope.calibrated.lower_bound,
            "samples": envelope.calibrated.samples,
            "status": envelope.calibrated.status.value,
        },
        "novelty_score": envelope.novelty_score,
        "ensemble_consensus_kind": (
            envelope.ensemble_consensus_kind.value
            if envelope.ensemble_consensus_kind is not None
            else None
        ),
        "ensemble_vote_share": envelope.ensemble_vote_share,
        "ensemble_support_confidence": envelope.ensemble_support_confidence,
        "ensemble_entropy": envelope.ensemble_entropy,
        "contract_status": (
            envelope.contract_status.value
            if envelope.contract_status is not None
            else None
        ),
        "quote_status": (
            envelope.quote_status.value if envelope.quote_status is not None else None
        ),
        "reason_codes": list(envelope.reason_codes),
    }


def _event_payload(event: SignalEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "message_id": event.message_id,
        "kind": event.kind.value,
        "contract_key": event.contract_key,
        "ticker": event.ticker,
        "option_side": event.option_side,
        "strike": str(event.strike) if event.strike is not None else None,
        "expiry": event.expiry.isoformat() if event.expiry is not None else None,
        "source_ts_utc": event.source_ts_utc.isoformat(),
        "received_ts_utc": event.received_ts_utc.isoformat(),
        "raw_text": event.raw_text,
        "reason": event.reason,
    }


class ReviewQueue:
    """A durable worklist over the semantic evidence envelope."""

    def __init__(self, store: Store, *, sla: dict[ReviewTier, timedelta] | None = None) -> None:
        self.store = store
        self.sla = dict(DEFAULT_SLA)
        if sla:
            self.sla.update(sla)

    def get(self, review_id: str) -> ReviewItem | None:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM review_items WHERE review_id=?", (review_id,)
            ).fetchone()
        return _row_to_item(dict(row)) if row is not None else None

    def by_event(self, event_id: str) -> ReviewItem | None:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM review_items WHERE event_id=?", (event_id,)
            ).fetchone()
        return _row_to_item(dict(row)) if row is not None else None

    def enqueue(
        self,
        event: SignalEvent,
        parser: DecisionEvidence,
        envelope: SemanticEvidenceEnvelope,
        *,
        association_confidence: float = 1.0,
        now: datetime | None = None,
        force: bool = False,
    ) -> ReviewItem | None:
        """Create or refresh one event's review item.

        A resolved or dismissed item is never silently reopened: the reviewer's decision is
        durable, and a new revision arrives with a new event id because the parser hashes the
        edit revision into it. An open item is refreshed so new evidence is visible.
        """
        if not force and envelope.disposition is SemanticDisposition.OBSERVE:
            return None
        if not 0.0 <= association_confidence <= 1.0:
            raise ReviewError("bad_association_confidence", str(association_confidence))
        priority: ReviewPriority = prioritize(
            _candidate(parser, envelope, association_confidence=association_confidence)
        )
        evidence = {
            "event": _event_payload(event),
            "parser": {
                "rule_id": parser.rule_id,
                "confidence": parser.confidence,
                "matched_terms": list(parser.matched_terms),
                "conflicts": list(parser.conflicts),
                "normalized_text": parser.normalized_text,
                "latency_us": parser.latency_us,
            },
            "association_confidence": association_confidence,
            "envelope": _envelope_payload(envelope),
            "evidence_digest": hashlib.sha256(
                _canonical(
                    {
                        "event_id": event.event_id,
                        "disposition": envelope.disposition.value,
                        "reasons": list(envelope.reason_codes),
                        "priority": priority.score,
                    }
                ).encode("utf-8")
            ).hexdigest(),
        }
        stamp = _now_iso(now)
        review_id = _review_id(event.event_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                exists = db.execute(
                    "SELECT 1 FROM signal_events WHERE event_id=?", (event.event_id,)
                ).fetchone()
                if exists is None:
                    raise ReviewError("event_not_stored", event.event_id)
                current = db.execute(
                    "SELECT * FROM review_items WHERE event_id=?", (event.event_id,)
                ).fetchone()
                if current is not None:
                    item = _row_to_item(dict(current))
                    if item.status in {ReviewStatus.RESOLVED, ReviewStatus.DISMISSED}:
                        db.execute("COMMIT")
                        return item
                    db.execute(
                        """
                        UPDATE review_items
                        SET disposition=?,priority_score=?,tier=?,reason_codes_json=?,
                            evidence_json=?,updated_ts_utc=?
                        WHERE event_id=?
                        """,
                        (
                            envelope.disposition.value,
                            priority.score,
                            priority.tier.value,
                            json.dumps(list(priority.reasons)),
                            _canonical(evidence),
                            stamp,
                            event.event_id,
                        ),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO review_items
                        (review_id,event_id,message_id,disposition,priority_score,tier,
                         reason_codes_json,status,assignee,evidence_json,created_ts_utc,
                         updated_ts_utc,resolved_ts_utc,resolution)
                        VALUES (?,?,?,?,?,?,?,'OPEN','',?,?,?,NULL,'')
                        """,
                        (
                            review_id,
                            event.event_id,
                            event.message_id,
                            envelope.disposition.value,
                            priority.score,
                            priority.tier.value,
                            json.dumps(list(priority.reasons)),
                            _canonical(evidence),
                            stamp,
                            stamp,
                        ),
                    )
                db.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.OperationalError):
                    db.execute("ROLLBACK")
                raise
        created = self.by_event(event.event_id)
        if created is None:
            raise ReviewError("enqueue_lost", event.event_id)
        return created

    def claim(
        self, review_id: str, assignee: str, *, now: datetime | None = None
    ) -> ReviewItem:
        if not assignee.strip():
            raise ReviewError("assignee_required", review_id)
        stamp = _now_iso(now)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM review_items WHERE review_id=?", (review_id,)
                ).fetchone()
                if row is None:
                    raise ReviewError("not_found", review_id)
                item = _row_to_item(dict(row))
                if item.status in {ReviewStatus.RESOLVED, ReviewStatus.DISMISSED}:
                    raise ReviewError("not_open", item.status.value)
                if item.status is ReviewStatus.CLAIMED and item.assignee != assignee:
                    raise ReviewError("already_claimed", item.assignee)
                db.execute(
                    "UPDATE review_items SET status='CLAIMED',assignee=?,updated_ts_utc=? "
                    "WHERE review_id=?",
                    (assignee, stamp, review_id),
                )
                db.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.OperationalError):
                    db.execute("ROLLBACK")
                raise
        claimed = self.get(review_id)
        if claimed is None:
            raise ReviewError("claim_lost", review_id)
        return claimed

    def resolve(
        self,
        review_id: str,
        expected_kind: EventKind,
        *,
        reviewer: str,
        expected_contract_key: str | None = None,
        note: str = "",
        now: datetime | None = None,
    ) -> ReviewItem:
        """Record the reviewer's label and the adjudication that calibrates future parses.

        The adjudication and the queue transition share one writer transaction. A half-applied
        resolution would either lose the label or leave the item open with no way to tell that
        the label was already recorded.
        """
        if not reviewer.strip():
            raise ReviewError("reviewer_required", review_id)
        stamp = _now_iso(now)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM review_items WHERE review_id=?", (review_id,)
                ).fetchone()
                if row is None:
                    raise ReviewError("not_found", review_id)
                item = _row_to_item(dict(row))
                if item.status in {ReviewStatus.RESOLVED, ReviewStatus.DISMISSED}:
                    raise ReviewError("not_open", item.status.value)
                db.execute(
                    """
                    INSERT OR REPLACE INTO adjudications
                    (event_id,expected_kind,expected_contract_key,reviewer,
                     adjudicated_ts_utc,note)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        item.event_id,
                        expected_kind.value,
                        expected_contract_key,
                        reviewer,
                        stamp,
                        note,
                    ),
                )
                db.execute(
                    """
                    UPDATE review_items
                    SET status='RESOLVED',assignee=?,resolution='ADJUDICATED',
                        resolved_ts_utc=?,updated_ts_utc=?
                    WHERE review_id=?
                    """,
                    (reviewer, stamp, stamp, review_id),
                )
                db.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.OperationalError):
                    db.execute("ROLLBACK")
                raise
        resolved = self.get(review_id)
        if resolved is None:
            raise ReviewError("resolve_lost", review_id)
        return resolved

    def dismiss(
        self,
        review_id: str,
        *,
        reviewer: str,
        note: str = "",
        now: datetime | None = None,
    ) -> ReviewItem:
        if not reviewer.strip():
            raise ReviewError("reviewer_required", review_id)
        stamp = _now_iso(now)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM review_items WHERE review_id=?", (review_id,)
                ).fetchone()
                if row is None:
                    raise ReviewError("not_found", review_id)
                item = _row_to_item(dict(row))
                if item.status in {ReviewStatus.RESOLVED, ReviewStatus.DISMISSED}:
                    raise ReviewError("not_open", item.status.value)
                db.execute(
                    """
                    UPDATE review_items
                    SET status='DISMISSED',assignee=?,resolution=?,resolved_ts_utc=?,
                        updated_ts_utc=?
                    WHERE review_id=?
                    """,
                    (reviewer, note or "DISMISSED", stamp, stamp, review_id),
                )
                db.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.OperationalError):
                    db.execute("ROLLBACK")
                raise
        dismissed = self.get(review_id)
        if dismissed is None:
            raise ReviewError("dismiss_lost", review_id)
        return dismissed

    def list_open(
        self, *, tier: ReviewTier | None = None, limit: int = 100
    ) -> tuple[ReviewItem, ...]:
        if limit < 1:
            raise ReviewError("bad_limit", str(limit))
        query = (
            "SELECT * FROM review_items WHERE status IN ('OPEN','CLAIMED') "
        )
        params: list[object] = []
        if tier is not None:
            query += "AND tier=? "
            params.append(tier.value)
        query += "ORDER BY priority_score DESC, created_ts_utc, review_id LIMIT ?"
        params.append(limit)
        with self.store.connect() as db:
            rows = db.execute(query, params).fetchall()
        return tuple(_row_to_item(dict(row)) for row in rows)

    def backfill(
        self,
        *,
        limit: int | None = None,
        include_ready: bool = False,
        now: datetime | None = None,
    ) -> tuple[ReviewItem, ...]:
        """Triage unadjudicated stored signals without inventing evidence.

        Calibration comes only from adjudications that actually exist. Novelty comes from the
        raw text this store already holds. An event with no decision audit is skipped rather
        than given a fabricated rule or confidence.
        """
        if limit is not None and limit < 1:
            raise ReviewError("bad_limit", str(limit))
        signals = self.store.load_signals()
        audits = {str(row["event_id"]): row for row in self.store.load_decision_audits()}
        with self.store.connect() as db:
            adjudicated = {
                str(row["event_id"])
                for row in db.execute("SELECT event_id FROM adjudications")
            }
            existing = {
                str(row["event_id"])
                for row in db.execute("SELECT event_id FROM review_items")
            }
        calibrations = calibrate_rules(self.store.calibration_observations())
        index = NoveltyIndex([event.raw_text for event in signals if event.raw_text])

        created: list[ReviewItem] = []
        for event in signals:
            if limit is not None and len(created) >= limit:
                break
            if event.event_id in adjudicated or event.event_id in existing:
                continue
            audit = audits.get(event.event_id)
            if audit is None:
                continue
            parser = DecisionEvidence(
                rule_id=str(audit["parser_rule"]),
                confidence=float(str(audit["parser_confidence"])),
                matched_terms=tuple(json.loads(str(audit["matched_terms_json"]))),
                conflicts=tuple(json.loads(str(audit["conflicts_json"]))),
                normalized_text=event.raw_text,
                latency_us=int(str(audit["parser_latency_us"])),
            )
            calibration = calibrations.get(parser.rule_id)
            novelty = index.score(event.raw_text).novelty_score if event.raw_text else 1.0
            envelope = build_semantic_evidence_envelope(
                event,
                parser,
                calibration=calibration,
                novelty_score=novelty,
            )
            if envelope.disposition is SemanticDisposition.OBSERVE:
                continue
            if (
                envelope.disposition is SemanticDisposition.READY_FOR_OPERATOR_REVIEW
                and not include_ready
            ):
                continue
            item = self.enqueue(
                event,
                parser,
                envelope,
                association_confidence=float(str(audit["association_confidence"])),
                now=now,
            )
            if item is not None:
                created.append(item)
        return tuple(created)

    def metrics(self, *, now: datetime | None = None) -> dict[str, Any]:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM review_items")]
        open_items = [row for row in rows if row["status"] in {"OPEN", "CLAIMED"}]
        by_tier: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        oldest_age = 0.0
        breaches = 0
        for row in open_items:
            tier = ReviewTier(row["tier"])
            by_tier[tier.value] = by_tier.get(tier.value, 0) + 1
            for reason in json.loads(row["reason_codes_json"]):
                by_reason[str(reason)] = by_reason.get(str(reason), 0) + 1
            created = datetime.fromisoformat(str(row["created_ts_utc"]))
            age = max(0.0, (moment - created).total_seconds())
            oldest_age = max(oldest_age, age)
            if age > self.sla[tier].total_seconds():
                breaches += 1
        resolved = sum(1 for row in rows if row["status"] == "RESOLVED")
        dismissed = sum(1 for row in rows if row["status"] == "DISMISSED")
        observations = self.store.calibration_observations()
        calibrations = calibrate_rules(observations)
        calibration_payload: dict[str, Any] = {}
        for rule_id, calibration in sorted(calibrations.items()):
            status = (
                CalibrationStatus.TRUSTED.value
                if calibration.wilson_lower_95 >= 0.95 and calibration.samples >= 25
                else (
                    CalibrationStatus.DEGRADED.value
                    if calibration.samples >= 25
                    else CalibrationStatus.PROVISIONAL.value
                )
            )
            calibration_payload[rule_id] = {
                "samples": calibration.samples,
                "accuracy": round(calibration.empirical_accuracy, 4),
                "wilson_lower_95": round(calibration.wilson_lower_95, 4),
                "calibration_gap": round(calibration.calibration_gap, 4),
                "brier_score": round(calibration.brier_score, 4),
                "status": status,
            }
        return {
            "items": len(rows),
            "open": len(open_items),
            "resolved": resolved,
            "dismissed": dismissed,
            "resolution_rate": (
                round(resolved / (resolved + dismissed), 4)
                if resolved + dismissed
                else 0.0
            ),
            "by_tier": dict(sorted(by_tier.items())),
            "by_reason": dict(sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))),
            "oldest_open_age_seconds": round(oldest_age, 1),
            "sla_breaches": breaches,
            "adjudications": len(observations),
            "calibration": calibration_payload,
        }


def render_items(items: Sequence[ReviewItem]) -> str:
    if not items:
        return "review queue: empty"
    lines = [
        f"{'tier':7} {'score':>6} {'status':8} {'event':10} reasons",
    ]
    for item in items:
        lines.append(
            f"{item.tier.value:7} {item.priority_score:6.1f} {item.status.value:8} "
            f"{item.event_id[:10]:10} {','.join(item.reason_codes) or '-'}"
        )
    return "\n".join(lines)


def render_metrics(payload: dict[str, Any]) -> str:
    lines = [
        f"items {payload['items']}  open {payload['open']}  resolved {payload['resolved']}  "
        f"dismissed {payload['dismissed']}  resolution {payload['resolution_rate']:.1%}",
        f"oldest open {payload['oldest_open_age_seconds']:.0f}s  "
        f"SLA breaches {payload['sla_breaches']}  adjudications {payload['adjudications']}",
        "by tier " + ", ".join(f"{key} {value}" for key, value in payload["by_tier"].items()),
        "by reason "
        + (", ".join(f"{key} {value}" for key, value in payload["by_reason"].items()) or "none"),
    ]
    if payload["calibration"]:
        lines.append("calibration")
        for rule_id, row in payload["calibration"].items():
            lines.append(
                f"  {rule_id:28s} n={row['samples']:<4d} acc={row['accuracy']:.3f} "
                f"wilson={row['wilson_lower_95']:.3f} gap={row['calibration_gap']:+.3f} "
                f"{row['status']}"
            )
    return "\n".join(lines)


def review_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="scorpion-review")
    parser.add_argument("--db", default="scorpion.db")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    backfill = sub.add_parser("backfill", help="triage unadjudicated stored signals")
    backfill.add_argument("--limit", type=int, default=None)
    backfill.add_argument("--include-ready", action="store_true")

    listing = sub.add_parser("list", help="list open review items")
    listing.add_argument("--tier", choices=[tier.value for tier in ReviewTier], default=None)
    listing.add_argument("--limit", type=int, default=100)

    claim = sub.add_parser("claim", help="claim an item for a reviewer")
    claim.add_argument("review_id")
    claim.add_argument("--assignee", required=True)

    resolve = sub.add_parser("resolve", help="resolve an item and record its adjudication")
    resolve.add_argument("review_id")
    resolve.add_argument("--kind", choices=[kind.value for kind in EventKind], required=True)
    resolve.add_argument("--contract", default=None)
    resolve.add_argument("--reviewer", required=True)
    resolve.add_argument("--note", default="")

    dismiss = sub.add_parser("dismiss", help="dismiss an item without an adjudication")
    dismiss.add_argument("review_id")
    dismiss.add_argument("--reviewer", required=True)
    dismiss.add_argument("--note", default="")

    sub.add_parser("metrics", help="queue age, SLA, reasons and calibration progress")

    args = parser.parse_args(argv)
    queue = ReviewQueue(Store(args.db))
    try:
        if args.cmd == "backfill":
            items = queue.backfill(limit=args.limit, include_ready=args.include_ready)
            payload = {"created": len(items), "items": [item.to_dict() for item in items]}
            print(
                json.dumps(payload, indent=2, sort_keys=True)
                if args.json
                else render_items(items)
            )
            return 0
        if args.cmd == "list":
            tier = ReviewTier(args.tier) if args.tier else None
            items = queue.list_open(tier=tier, limit=args.limit)
            print(
                json.dumps([item.to_dict() for item in items], indent=2, sort_keys=True)
                if args.json
                else render_items(items)
            )
            return 0
        if args.cmd == "claim":
            item = queue.claim(args.review_id, args.assignee)
            print(json.dumps(item.to_dict(), indent=2, sort_keys=True))
            return 0
        if args.cmd == "resolve":
            item = queue.resolve(
                args.review_id,
                EventKind(args.kind),
                expected_contract_key=args.contract,
                reviewer=args.reviewer,
                note=args.note,
            )
            print(json.dumps(item.to_dict(), indent=2, sort_keys=True))
            return 0
        if args.cmd == "dismiss":
            item = queue.dismiss(
                args.review_id, reviewer=args.reviewer, note=args.note
            )
            print(json.dumps(item.to_dict(), indent=2, sort_keys=True))
            return 0
        if args.cmd == "metrics":
            payload = queue.metrics()
            print(
                json.dumps(payload, indent=2, sort_keys=True)
                if args.json
                else render_metrics(payload)
            )
            return 0
    except ReviewError as error:
        print(json.dumps({"ok": False, "error": error.code, "detail": error.detail}))
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(review_main())
