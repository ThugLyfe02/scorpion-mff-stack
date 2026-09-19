from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .accuracy import percentile

STAGE_COLUMNS = (
    "raw_persist_us",
    "parse_us",
    "association_us",
    "reduce_validate_us",
    "db_precommit_us",
)

_STAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS transition_stage_latency (
    event_id TEXT PRIMARY KEY,
    raw_revision_id TEXT NOT NULL,
    raw_persist_us INTEGER NOT NULL,
    parse_us INTEGER NOT NULL,
    association_us INTEGER NOT NULL,
    reduce_validate_us INTEGER NOT NULL,
    db_precommit_us INTEGER NOT NULL,
    created_ts_utc TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class StageStats:
    count: int
    p50_us: float
    p95_us: float
    p99_us: float
    max_us: int


@dataclass(frozen=True, slots=True)
class StageLatencyReport:
    count: int
    stages: dict[str, StageStats]
    bottleneck_stage: str | None


@dataclass(frozen=True, slots=True)
class SQLiteStorageSnapshot:
    db_bytes: int
    wal_bytes: int
    shm_bytes: int
    page_count: int
    freelist_pages: int
    page_size: int
    journal_mode: str


def ensure_stage_trace_schema(db: sqlite3.Connection) -> None:
    db.execute(_STAGE_SCHEMA)


def append_stage_trace(
    db: sqlite3.Connection,
    *,
    event_id: str,
    raw_revision_id: str,
    stage_latencies_us: Mapping[str, int],
    db_precommit_us: int,
    created_ts_utc: str,
) -> None:
    ensure_stage_trace_schema(db)
    db.execute(
        """
        INSERT OR IGNORE INTO transition_stage_latency
        (event_id,raw_revision_id,raw_persist_us,parse_us,association_us,
         reduce_validate_us,db_precommit_us,created_ts_utc)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            raw_revision_id,
            int(stage_latencies_us.get("raw_persist_us", 0)),
            int(stage_latencies_us.get("parse_us", 0)),
            int(stage_latencies_us.get("association_us", 0)),
            int(stage_latencies_us.get("reduce_validate_us", 0)),
            max(0, db_precommit_us),
            created_ts_utc,
        ),
    )


def _stats(values: list[int]) -> StageStats:
    if not values:
        return StageStats(0, 0.0, 0.0, 0.0, 0)
    return StageStats(
        count=len(values),
        p50_us=percentile(values, 0.50),
        p95_us=percentile(values, 0.95),
        p99_us=percentile(values, 0.99),
        max_us=max(values),
    )


def load_stage_latency_report(
    path: str | Path,
    *,
    limit: int = 500,
) -> StageLatencyReport:
    if limit <= 0:
        raise ValueError("limit must be positive")
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='transition_stage_latency'"
        ).fetchone()
        if exists is None:
            return StageLatencyReport(0, {}, None)
        rows = db.execute(
            "SELECT * FROM transition_stage_latency "
            "ORDER BY created_ts_utc DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        db.close()
    stages = {
        column: _stats([int(row[column]) for row in rows])
        for column in STAGE_COLUMNS
    }
    bottleneck = max(stages, key=lambda name: stages[name].p95_us) if rows else None
    return StageLatencyReport(len(rows), stages, bottleneck)


def storage_snapshot(path: str | Path) -> SQLiteStorageSnapshot:
    db_path = Path(path)
    db = sqlite3.connect(str(db_path))
    try:
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        freelist_pages = int(db.execute("PRAGMA freelist_count").fetchone()[0])
        page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
        journal_mode = str(db.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        db.close()
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")
    return SQLiteStorageSnapshot(
        db_bytes=db_path.stat().st_size if db_path.exists() else 0,
        wal_bytes=wal_path.stat().st_size if wal_path.exists() else 0,
        shm_bytes=shm_path.stat().st_size if shm_path.exists() else 0,
        page_count=page_count,
        freelist_pages=freelist_pages,
        page_size=page_size,
        journal_mode=journal_mode,
    )
