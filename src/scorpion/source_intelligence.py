from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SourceBehaviorProfile:
    author_id: str
    channel_id: str
    messages: int
    edits: int
    actionable_events: int
    ambiguous_events: int
    ignored_events: int
    unique_contracts: int
    edit_rate: float
    actionable_rate: float
    ambiguity_rate: float


@dataclass(frozen=True, slots=True)
class SourceBehaviorShift:
    score: float
    suspicious: bool
    reasons: tuple[str, ...]


def _safe_div(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def load_source_behavior_profile(
    path: str | Path,
    *,
    author_id: str,
    channel_id: str,
    limit: int = 200,
) -> SourceBehaviorProfile:
    if limit <= 0:
        raise ValueError("limit must be positive")
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        raw_rows = db.execute(
            """
            SELECT message_id,edited_ts_utc FROM raw_discord_events
            WHERE author_id=? AND channel_id=?
            ORDER BY source_ts_utc DESC,received_ts_utc DESC
            LIMIT ?
            """,
            (author_id, channel_id, limit),
        ).fetchall()
        message_ids = [str(row["message_id"]) for row in raw_rows]
        if not message_ids:
            return SourceBehaviorProfile(
                author_id,
                channel_id,
                0,
                0,
                0,
                0,
                0,
                0,
                0.0,
                0.0,
                0.0,
            )
        placeholders = ",".join("?" for _ in message_ids)
        signal_rows = db.execute(
            f"""
            SELECT kind,contract_key FROM signal_events
            WHERE message_id IN ({placeholders})
            """,
            message_ids,
        ).fetchall()
    finally:
        db.close()

    unique_messages = len(set(message_ids))
    edits = sum(row["edited_ts_utc"] is not None for row in raw_rows)
    actionable = sum(row["kind"] in {"ENTRY", "ADD", "TRIM", "EXIT"} for row in signal_rows)
    ambiguous = sum(row["kind"] == "AMBIGUOUS" for row in signal_rows)
    ignored = sum(row["kind"] == "IGNORE" for row in signal_rows)
    contracts = {str(row["contract_key"]) for row in signal_rows if row["contract_key"] is not None}
    total_signals = len(signal_rows)
    return SourceBehaviorProfile(
        author_id=author_id,
        channel_id=channel_id,
        messages=unique_messages,
        edits=edits,
        actionable_events=actionable,
        ambiguous_events=ambiguous,
        ignored_events=ignored,
        unique_contracts=len(contracts),
        edit_rate=_safe_div(edits, len(raw_rows)),
        actionable_rate=_safe_div(actionable, total_signals),
        ambiguity_rate=_safe_div(ambiguous, total_signals),
    )


def compare_source_behavior(
    recent: SourceBehaviorProfile,
    baseline: SourceBehaviorProfile,
    *,
    min_baseline_messages: int = 20,
    suspicious_score: float = 1.0,
) -> SourceBehaviorShift:
    if recent.author_id != baseline.author_id or recent.channel_id != baseline.channel_id:
        raise ValueError("source profiles must describe the same author/channel")
    if baseline.messages < min_baseline_messages or recent.messages == 0:
        return SourceBehaviorShift(0.0, False, ("insufficient_baseline",))

    reasons: list[str] = []
    score = 0.0

    edit_delta = recent.edit_rate - baseline.edit_rate
    if edit_delta >= 0.15:
        score += min(1.0, edit_delta / 0.30)
        reasons.append(f"edit_rate_shift:{edit_delta:+.3f}")

    ambiguity_delta = recent.ambiguity_rate - baseline.ambiguity_rate
    if ambiguity_delta >= 0.10:
        score += min(1.0, ambiguity_delta / 0.25)
        reasons.append(f"ambiguity_rate_shift:{ambiguity_delta:+.3f}")

    action_delta = abs(recent.actionable_rate - baseline.actionable_rate)
    if action_delta >= 0.25:
        score += min(0.75, action_delta / 0.50)
        reasons.append(f"actionable_rate_shift:{action_delta:.3f}")

    baseline_contract_density = _safe_div(baseline.unique_contracts, baseline.messages)
    recent_contract_density = _safe_div(recent.unique_contracts, recent.messages)
    density_ratio = (
        recent_contract_density / baseline_contract_density
        if baseline_contract_density > 0.0
        else 1.0
    )
    if math.isfinite(density_ratio) and density_ratio >= 2.0:
        score += min(0.5, (density_ratio - 1.0) / 2.0)
        reasons.append(f"contract_density_ratio:{density_ratio:.2f}")

    return SourceBehaviorShift(
        score=score,
        suspicious=score >= suspicious_score,
        reasons=tuple(reasons),
    )
