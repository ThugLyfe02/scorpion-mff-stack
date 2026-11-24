from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from pathlib import Path

from .processing_order import ensure_processing_order_schema

_ACTIONABLE = frozenset({"ENTRY", "ADD", "TRIM", "EXIT", "STOP"})
_FEATURE_SET_VERSION = "causal-features-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS causal_feature_snapshots (
    event_id TEXT NOT NULL,
    feature_set_version TEXT NOT NULL,
    process_seq INTEGER NOT NULL,
    feature_json TEXT NOT NULL,
    feature_sha256 TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    PRIMARY KEY(event_id,feature_set_version),
    FOREIGN KEY(event_id) REFERENCES signal_events(event_id)
);
CREATE INDEX IF NOT EXISTS idx_causal_features_process_seq
ON causal_feature_snapshots(process_seq,feature_set_version);
"""


@dataclass(frozen=True, slots=True)
class CausalFeatureSnapshot:
    event_id: str
    feature_set_version: str
    process_seq: int
    features: dict[str, float | int | str]
    feature_sha256: str
    created_ts_utc: datetime


@dataclass(frozen=True, slots=True)
class TrainingRow:
    event_id: str
    process_seq: int
    features: dict[str, float | int | str]
    expected_kind: str
    expected_contract_key: str | None


@dataclass(frozen=True, slots=True)
class FeatureStoreVerification:
    snapshots: int
    labeled_rows: int
    invalid_process_sequences: int
    hash_mismatches: int
    missing_target_events: int
    valid: bool


@dataclass(frozen=True, slots=True)
class FeatureBackfillReport:
    considered: int
    created: int
    existing: int
    feature_set_version: str


def _canonical_json(payload: dict[str, float | int | str]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _hash(payload: dict[str, float | int | str]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _payload(row: sqlite3.Row) -> dict[str, object]:
    value = json.loads(str(row["payload_json"]))
    return value if isinstance(value, dict) else {}


def _ensure(db: sqlite3.Connection) -> None:
    ensure_processing_order_schema(db)
    db.executescript(_SCHEMA)


def _current_context(db: sqlite3.Connection, event_id: str) -> tuple[int, dict[str, object]]:
    row = db.execute(
        """
        SELECT o.process_seq,s.payload_json
        FROM event_processing_order o
        JOIN signal_events s ON s.event_id=o.event_id
        WHERE s.event_id=?
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        raise KeyError(event_id)
    return int(row["process_seq"]), _payload(row)


def build_point_in_time_features(
    path: str | Path,
    event_id: str,
    *,
    window: int = 100,
) -> CausalFeatureSnapshot:
    """Build features using only evidence available before ``event_id`` was committed.

    ``process_seq`` is the point-in-time boundary. Current raw-context fields such as channel,
    author, ticker and source/receive lag are allowed because they are knowable at decision time.
    The current normalized action label and later human adjudications are deliberately excluded.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        _ensure(db)
        process_seq, current = _current_context(db, event_id)
        prior = db.execute(
            """
            SELECT o.process_seq,s.kind,s.payload_json,d.parser_confidence,
                   d.association_confidence,d.parser_latency_us
            FROM event_processing_order o
            JOIN signal_events s ON s.event_id=o.event_id
            LEFT JOIN decision_audit d ON d.event_id=s.event_id
            WHERE o.process_seq < ?
            ORDER BY o.process_seq DESC LIMIT ?
            """,
            (process_seq, window),
        ).fetchall()
    finally:
        db.close()

    channel_id = str(current.get("channel_id") or "")
    author_id = str(current.get("author_id") or "")
    ticker = str(current.get("ticker") or "")
    kinds = [str(row["kind"]) for row in prior]
    payloads = [_payload(row) for row in prior]
    same_channel = [item for item in payloads if str(item.get("channel_id") or "") == channel_id]
    same_author = [item for item in payloads if str(item.get("author_id") or "") == author_id]
    same_ticker = [item for item in payloads if str(item.get("ticker") or "") == ticker and ticker]
    confidences = [
        float(row["parser_confidence"])
        for row in prior
        if row["parser_confidence"] is not None
    ]
    association_confidences = [
        float(row["association_confidence"])
        for row in prior
        if row["association_confidence"] is not None
    ]
    parser_latencies = [
        int(row["parser_latency_us"])
        for row in prior
        if row["parser_latency_us"] is not None
    ]
    source_ts = datetime.fromisoformat(str(current["source_ts_utc"])).astimezone(UTC)
    received_ts = datetime.fromisoformat(str(current["received_ts_utc"])).astimezone(UTC)
    features: dict[str, float | int | str] = {
        "feature_set_version": _FEATURE_SET_VERSION,
        "history_window": window,
        "prior_events": len(prior),
        "prior_actionable_rate": _rate(sum(kind in _ACTIONABLE for kind in kinds), len(kinds)),
        "prior_ambiguity_rate": _rate(sum(kind == "AMBIGUOUS" for kind in kinds), len(kinds)),
        "same_channel_prior": len(same_channel),
        "same_author_prior": len(same_author),
        "same_ticker_prior": len(same_ticker),
        "mean_parser_confidence": (
            sum(confidences) / len(confidences) if confidences else 0.0
        ),
        "mean_association_confidence": (
            sum(association_confidences) / len(association_confidences)
            if association_confidences
            else 0.0
        ),
        "mean_parser_latency_us": (
            sum(parser_latencies) / len(parser_latencies) if parser_latencies else 0.0
        ),
        "source_receive_lag_ms": (received_ts - source_ts).total_seconds() * 1000.0,
        "channel_id": channel_id,
        "author_id": author_id,
        "ticker": ticker,
    }
    created = datetime.now(UTC)
    return CausalFeatureSnapshot(
        event_id=event_id,
        feature_set_version=_FEATURE_SET_VERSION,
        process_seq=process_seq,
        features=features,
        feature_sha256=_hash(features),
        created_ts_utc=created,
    )


def persist_feature_snapshot(path: str | Path, snapshot: CausalFeatureSnapshot) -> None:
    encoded = _canonical_json(snapshot.features)
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != snapshot.feature_sha256:
        raise ValueError("feature snapshot hash does not match payload")
    with sqlite3.connect(str(path)) as db:
        _ensure(db)
        target = db.execute(
            "SELECT process_seq FROM event_processing_order WHERE event_id=?",
            (snapshot.event_id,),
        ).fetchone()
        if target is None or int(target[0]) != snapshot.process_seq:
            raise ValueError("feature snapshot process boundary does not match durable order")
        db.execute(
            """
            INSERT OR REPLACE INTO causal_feature_snapshots
            (event_id,feature_set_version,process_seq,feature_json,feature_sha256,created_ts_utc)
            VALUES (?,?,?,?,?,?)
            """,
            (
                snapshot.event_id,
                snapshot.feature_set_version,
                snapshot.process_seq,
                encoded,
                snapshot.feature_sha256,
                snapshot.created_ts_utc.isoformat(),
            ),
        )


def build_and_persist_features(
    path: str | Path,
    event_id: str,
    *,
    window: int = 100,
) -> CausalFeatureSnapshot:
    snapshot = build_point_in_time_features(path, event_id, window=window)
    persist_feature_snapshot(path, snapshot)
    return snapshot


def backfill_feature_snapshots(
    path: str | Path,
    *,
    window: int = 100,
    limit: int | None = None,
) -> FeatureBackfillReport:
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        _ensure(db)
        rows = db.execute(
            """
            SELECT o.event_id,
                   CASE WHEN f.event_id IS NULL THEN 0 ELSE 1 END AS already_exists
            FROM event_processing_order o
            LEFT JOIN causal_feature_snapshots f
              ON f.event_id=o.event_id AND f.feature_set_version=?
            ORDER BY o.process_seq
            """,
            (_FEATURE_SET_VERSION,),
        ).fetchall()
    if limit is not None:
        rows = rows[:limit]
    created = 0
    existing = 0
    for row in rows:
        if int(row["already_exists"]):
            existing += 1
            continue
        build_and_persist_features(path, str(row["event_id"]), window=window)
        created += 1
    return FeatureBackfillReport(
        considered=len(rows),
        created=created,
        existing=existing,
        feature_set_version=_FEATURE_SET_VERSION,
    )


def load_training_rows(
    path: str | Path,
    *,
    feature_set_version: str = _FEATURE_SET_VERSION,
) -> tuple[TrainingRow, ...]:
    """Attach later human labels to frozen point-in-time feature snapshots."""
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        _ensure(db)
        rows = db.execute(
            """
            SELECT f.event_id,f.process_seq,f.feature_json,a.expected_kind,
                   a.expected_contract_key
            FROM causal_feature_snapshots f
            JOIN adjudications a ON a.event_id=f.event_id
            WHERE f.feature_set_version=?
            ORDER BY f.process_seq
            """,
            (feature_set_version,),
        ).fetchall()
    finally:
        db.close()
    return tuple(
        TrainingRow(
            event_id=str(row["event_id"]),
            process_seq=int(row["process_seq"]),
            features=json.loads(str(row["feature_json"])),
            expected_kind=str(row["expected_kind"]),
            expected_contract_key=(
                str(row["expected_contract_key"])
                if row["expected_contract_key"] is not None
                else None
            ),
        )
        for row in rows
    )


def verify_feature_store(path: str | Path) -> FeatureStoreVerification:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        _ensure(db)
        rows = db.execute(
            """
            SELECT f.*,o.process_seq AS target_process_seq
            FROM causal_feature_snapshots f
            LEFT JOIN event_processing_order o ON o.event_id=f.event_id
            ORDER BY f.process_seq
            """
        ).fetchall()
        labeled_rows = int(
            db.execute(
                """
                SELECT COUNT(*) FROM causal_feature_snapshots f
                JOIN adjudications a ON a.event_id=f.event_id
                """
            ).fetchone()[0]
        )
    finally:
        db.close()
    invalid_seq = 0
    mismatches = 0
    missing = 0
    for row in rows:
        if row["target_process_seq"] is None:
            missing += 1
            continue
        if int(row["target_process_seq"]) != int(row["process_seq"]):
            invalid_seq += 1
        payload = json.loads(str(row["feature_json"]))
        if _hash(payload) != str(row["feature_sha256"]):
            mismatches += 1
    return FeatureStoreVerification(
        snapshots=len(rows),
        labeled_rows=labeled_rows,
        invalid_process_sequences=invalid_seq,
        hash_mismatches=mismatches,
        missing_target_events=missing,
        valid=not (invalid_seq or mismatches or missing),
    )


# Forward paper-intent evidence is intentionally not a labeled TrainingRow.
# A decision log cannot establish that a market fill or profitable outcome occurred.
@dataclass(frozen=True, slots=True)
class PaperIntentFeatureRow:
    journal_sha256: str
    decision_seq: int
    intent_seq: int
    call_id: str
    ticker: str
    rules_sha256: str
    role: str
    features: dict[str, float | int | str]
    feature_sha256: str
    authority: str = "RESEARCH_ONLY"
    label_status: str = "UNLABELED"


@dataclass(frozen=True, slots=True)
class PaperIntentFeatureReport:
    journal_sha256: str
    rows: tuple[PaperIntentFeatureRow, ...]
    refusals: tuple[tuple[str, int], ...]
    labeled_training_rows: int = 0
    authority: str = "RESEARCH_ONLY"


def _paper_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamp")
    return stamp.astimezone(UTC)


def _paper_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("nonnegative integer required")
    return value


def _paper_decimal(value: object) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("positive finite price required")
    return number


def _paper_hash(value: object) -> str:
    if (
        not isinstance(value, str) or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("invalid hash")
    return value


def _paper_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing identity")
    return value


def _intent_features(
    decision: dict[str, object], row: dict[str, object], journal_hash: str,
    max_age_ns: int,
) -> PaperIntentFeatureRow:
    call_id = _paper_text(row.get("call_id"))
    ticker = _paper_text(row.get("ticker"))
    rules = _paper_hash(row.get("rules_sha256"))
    if (
        decision.get("call_id") != call_id or decision.get("ticker") != ticker
        or decision.get("rules_sha256") != rules
        or decision.get("action") != "schedule_entry"
        or row.get("mode") != "PAPER" or row.get("status") != "PAPER_INTENT_ONLY"
    ):
        raise ValueError("intent identity or authority mismatch")
    if type(row.get("chase")) is not bool:
        raise ValueError("intent role missing")
    chase = row["chase"] is True
    if row.get("conditional_on") != ("initial_unfilled" if chase else None):
        raise ValueError("intent condition mismatch")
    # Bind each feature to the source revision, prior decision and quote at admission.
    content_hash = _paper_hash(decision.get("content_sha256"))
    revision = _paper_hash(decision.get("revision_id"))
    source = _paper_timestamp(decision.get("source_ts_utc"))
    received = _paper_timestamp(decision.get("received_ts_utc"))
    decided = _paper_timestamp(decision.get("ts_utc"))
    intent_time = _paper_timestamp(row.get("ts_utc"))
    if not source <= received <= decided <= intent_time:
        raise ValueError("noncausal signal chronology")
    decision_seq, intent_seq = _paper_int(decision["seq"]), _paper_int(row["seq"])
    if not 0 < decision_seq < intent_seq:
        raise ValueError("noncausal sequence")
    evidence = row.get("quote_evidence")
    if not isinstance(evidence, dict):
        raise ValueError("quote evidence missing")
    observed = _paper_timestamp(evidence.get("observed_ts_utc"))
    event_ns = _paper_int(evidence.get("ts_event_ns"))
    receive_ns = _paper_int(evidence.get("recv_mono_ns"))
    decision_ns = _paper_int(evidence.get("decision_mono_ns"))
    source_age_ns = _paper_int(evidence.get("source_age_ns"))
    if (
        event_ns == 0 or observed > intent_time or receive_ns > decision_ns
        or abs(int(observed.timestamp() * 1_000_000_000) - event_ns) > 2000
    ):
        raise ValueError("noncausal quote clock")
    age_ns = source_age_ns + decision_ns - receive_ns
    if age_ns > max_age_ns:
        raise ValueError("quote outside freshness bound")
    bid, ask = _paper_decimal(evidence.get("bid")), _paper_decimal(evidence.get("ask"))
    limit, anchor = _paper_decimal(row.get("limit")), _paper_decimal(row.get("reference_price"))
    quantity = _paper_int(row.get("quantity"))
    if ask < bid or quantity == 0:
        raise ValueError("invalid executable inputs")
    midpoint = (ask + bid) / 2
    features: dict[str, float | int | str] = {
        "feature_set": "paper-intent-features-v1",
        "source_ts_utc": source.isoformat(),
        "received_ts_utc": received.isoformat(),
        "intent_ts_utc": intent_time.isoformat(),
        "source_age_ms": (intent_time - source).total_seconds() * 1000,
        "signal_receive_lag_ms": (received - source).total_seconds() * 1000,
        "quote_event_ns": event_ns,
        "quote_age_ns": age_ns,
        "bid": str(bid), "ask": str(ask), "limit": str(limit),
        "reference_price": str(anchor), "quantity": quantity,
        "spread_bps": str((ask - bid) / midpoint * 10_000),
        "limit_to_ask_bps": str((limit - ask) / ask * 10_000),
        "revision_id": revision, "content_sha256": content_hash,
    }
    return PaperIntentFeatureRow(
        journal_sha256=journal_hash, decision_seq=decision_seq, intent_seq=intent_seq,
        call_id=call_id, ticker=ticker, rules_sha256=rules,
        role="CHASE" if chase else "INITIAL", features=features,
        feature_sha256=_hash({
            **features, "journal_sha256": journal_hash, "call_id": call_id,
            "ticker": ticker, "decision_seq": decision_seq, "intent_seq": intent_seq,
            "rules_sha256": rules, "role": "CHASE" if chase else "INITIAL",
        }),
    )


def load_paper_intent_features(
    path: str | Path, *, max_quote_age_ms: int = 3000,
) -> PaperIntentFeatureReport:
    """Extract only pre-intent information from a sealed, exact-byte journal.

    The sealed journal is local integrity evidence, not authenticated market data.
    This function produces unlabeled research rows, never training targets or fills.
    """
    from .session_service import read_verified_paper_records

    if max_quote_age_ms <= 0:
        raise ValueError("quote age bound must be positive")
    receipt, records = read_verified_paper_records(Path(path))
    journal_hash = _paper_hash(receipt["sha256"])
    decisions: dict[str, dict[str, object]] = {}
    seen_roles: set[tuple[str, str]] = set()
    rows: list[PaperIntentFeatureRow] = []
    refusals: dict[str, int] = {}

    def refuse(reason: str) -> None:
        refusals[reason] = refusals.get(reason, 0) + 1

    for row in records:
        if row.get("kind") == "decision" and row.get("action") == "schedule_entry":
            call_id = row.get("call_id")
            if isinstance(call_id, str) and call_id:
                if call_id in decisions:
                    raise ValueError("duplicate scheduled call in sealed journal")
                decisions[call_id] = row
        if row.get("kind") != "intent":
            continue
        try:
            call_id = _paper_text(row.get("call_id"))
            decision = decisions.get(call_id)
            if decision is None:
                refuse("missing_prior_decision")
                continue
            # Freeze v1 arithmetic so a caller's Decimal context cannot change evidence IDs.
            with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
                feature = _intent_features(
                    decision, row, journal_hash, max_quote_age_ms * 1_000_000,
                )
            role = (feature.call_id, feature.role)
            if role in seen_roles:
                raise ValueError("duplicate intent role")
            if feature.role == "CHASE" and (call_id, "INITIAL") not in seen_roles:
                refuse("missing_qualified_initial")
                continue
            seen_roles.add(role)
            rows.append(feature)
        except (ValueError, TypeError, KeyError, InvalidOperation):
            refuse("invalid_intent_evidence")
    return PaperIntentFeatureReport(journal_hash, tuple(rows), tuple(sorted(refusals.items())))
