from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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


def _canonical_json(payload: dict[str, float | int | str]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _hash(payload: dict[str, float | int | str]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _payload(row: sqlite3.Row) -> dict[str, object]:
    value = json.loads(str(row["payload_json"]))
    return value if isinstance(value, dict) else {}


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
    """Build features using only normalized events committed before ``event_id``.

    The durable process sequence is the information boundary. Source timestamps are retained as
    data but never used to pull future normalized decisions backward into the feature set.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        db.executescript(_SCHEMA)
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
        "event_kind": str(current.get("kind") or ""),
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
        db.executescript(_SCHEMA)
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


def load_training_rows(
    path: str | Path,
    *,
    feature_set_version: str = _FEATURE_SET_VERSION,
) -> tuple[TrainingRow, ...]:
    """Attach later human labels to frozen point-in-time feature snapshots."""
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        db.executescript(_SCHEMA)
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
        db.executescript(_SCHEMA)
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
