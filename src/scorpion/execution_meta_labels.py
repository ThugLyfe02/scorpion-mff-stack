from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .domain import EventKind
from .execution_attribution import build_execution_attribution_report
from .microstructure import OptionMicrostructureTape
from .microstructure_forensics import (
    MicroForensicStatus,
    MicrostructureForensicsReport,
)


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
