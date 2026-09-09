from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .label_consensus import (
    Annotation,
    LabelConsensusPolicy,
    LabelConsensusReport,
    evaluate_label_consensus,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS adjudication_annotations (
    annotation_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    label TEXT NOT NULL,
    note TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    UNIQUE(event_id, reviewer_id, label, note)
);
CREATE INDEX IF NOT EXISTS idx_adjudication_annotations_event
ON adjudication_annotations(event_id, created_ts_utc, annotation_id);
CREATE INDEX IF NOT EXISTS idx_adjudication_annotations_reviewer
ON adjudication_annotations(reviewer_id, created_ts_utc, annotation_id);
"""


@dataclass(frozen=True, slots=True)
class StoredAnnotation:
    annotation_id: str
    event_id: str
    reviewer_id: str
    label: str
    note: str
    created_ts_utc: datetime


def _identity(event_id: str, reviewer_id: str, label: str, note: str) -> str:
    material = "\x1f".join((event_id, reviewer_id, label, note))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def append_annotation(
    path: str | Path,
    *,
    event_id: str,
    reviewer_id: str,
    label: str,
    note: str = "",
    created_ts_utc: datetime | None = None,
) -> bool:
    for name, value in (("event_id", event_id), ("reviewer_id", reviewer_id), ("label", label)):
        if not value.strip():
            raise ValueError(f"{name} is required")
    timestamp = created_ts_utc or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("created_ts_utc must be timezone-aware")
    annotation_id = _identity(event_id, reviewer_id, label, note)
    with sqlite3.connect(str(path)) as db:
        db.executescript(_SCHEMA)
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO adjudication_annotations
            (annotation_id,event_id,reviewer_id,label,note,created_ts_utc)
            VALUES (?,?,?,?,?,?)
            """,
            (annotation_id, event_id, reviewer_id, label, note, timestamp.isoformat()),
        )
        return cursor.rowcount == 1


def load_annotations(path: str | Path) -> tuple[StoredAnnotation, ...]:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        rows = db.execute(
            """
            SELECT annotation_id,event_id,reviewer_id,label,note,created_ts_utc
            FROM adjudication_annotations
            ORDER BY created_ts_utc,annotation_id
            """
        ).fetchall()
    return tuple(
        StoredAnnotation(
            annotation_id=str(row["annotation_id"]),
            event_id=str(row["event_id"]),
            reviewer_id=str(row["reviewer_id"]),
            label=str(row["label"]),
            note=str(row["note"]),
            created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])),
        )
        for row in rows
    )


def consensus_from_pool(
    path: str | Path,
    *,
    policy: LabelConsensusPolicy | None = None,
) -> LabelConsensusReport:
    rows = load_annotations(path)
    annotations = tuple(
        Annotation(item.event_id, item.reviewer_id, item.label)
        for item in rows
    )
    return evaluate_label_consensus(annotations, policy=policy)
