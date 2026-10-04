from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CheckpointMode = Literal["PASSIVE", "FULL", "RESTART", "TRUNCATE"]
_ALLOWED_MODES = frozenset({"PASSIVE", "FULL", "RESTART", "TRUNCATE"})


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    database_bytes: int
    wal_bytes: int
    shm_bytes: int
    journal_mode: str
    synchronous: int
    wal_autocheckpoint_pages: int
    page_size: int
    page_count: int
    freelist_count: int
    busy_timeout_ms: int

    @property
    def wal_to_database_ratio(self) -> float:
        return self.wal_bytes / max(self.database_bytes, 1)


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    mode: CheckpointMode
    busy: int
    log_frames: int
    checkpointed_frames: int


def inspect_storage(path: str | Path) -> StorageSnapshot:
    database = Path(path)
    if not database.exists():
        raise FileNotFoundError(database)
    with sqlite3.connect(str(database), isolation_level=None) as db:
        journal_mode = str(db.execute("PRAGMA journal_mode").fetchone()[0])
        synchronous = int(db.execute("PRAGMA synchronous").fetchone()[0])
        wal_autocheckpoint = int(db.execute("PRAGMA wal_autocheckpoint").fetchone()[0])
        page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(db.execute("PRAGMA freelist_count").fetchone()[0])
        busy_timeout = int(db.execute("PRAGMA busy_timeout").fetchone()[0])
    wal_path = Path(f"{database}-wal")
    shm_path = Path(f"{database}-shm")
    return StorageSnapshot(
        database_bytes=database.stat().st_size,
        wal_bytes=wal_path.stat().st_size if wal_path.exists() else 0,
        shm_bytes=shm_path.stat().st_size if shm_path.exists() else 0,
        journal_mode=journal_mode,
        synchronous=synchronous,
        wal_autocheckpoint_pages=wal_autocheckpoint,
        page_size=page_size,
        page_count=page_count,
        freelist_count=freelist_count,
        busy_timeout_ms=busy_timeout,
    )


def evaluate_storage_health(
    snapshot: StorageSnapshot,
    *,
    max_wal_bytes: int = 64 * 1024 * 1024,
    max_wal_to_database_ratio: float = 2.0,
    max_freelist_ratio: float = 0.25,
) -> tuple[str, ...]:
    failures: list[str] = []
    if snapshot.journal_mode.lower() != "wal":
        failures.append(f"journal_mode:{snapshot.journal_mode}")
    if snapshot.wal_bytes > max_wal_bytes:
        failures.append(f"wal_bytes:{snapshot.wal_bytes}>{max_wal_bytes}")
    if snapshot.wal_to_database_ratio > max_wal_to_database_ratio:
        failures.append(
            "wal_to_database_ratio:"
            f"{snapshot.wal_to_database_ratio:.3f}>{max_wal_to_database_ratio:.3f}"
        )
    freelist_ratio = snapshot.freelist_count / max(snapshot.page_count, 1)
    if freelist_ratio > max_freelist_ratio:
        failures.append(f"freelist_ratio:{freelist_ratio:.3f}>{max_freelist_ratio:.3f}")
    return tuple(failures)


def checkpoint_wal(path: str | Path, mode: CheckpointMode = "PASSIVE") -> CheckpointResult:
    if mode not in _ALLOWED_MODES:
        raise ValueError("invalid checkpoint mode")
    database = Path(path)
    if not database.exists():
        raise FileNotFoundError(database)
    with sqlite3.connect(str(database), isolation_level=None) as db:
        row = db.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    if row is None:
        raise RuntimeError("wal checkpoint returned no status")
    return CheckpointResult(mode, int(row[0]), int(row[1]), int(row[2]))
