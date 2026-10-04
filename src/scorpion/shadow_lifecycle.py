from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .auto_trainer import TrainingRunReport
from .governance import PromotionDecision, PromotionStatus


class ShadowReleaseState(StrEnum):
    CANDIDATE = "CANDIDATE"
    ACTIVE_SHADOW = "ACTIVE_SHADOW"
    SUPERSEDED = "SUPERSEDED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class ShadowReleaseRecord:
    shadow_release_id: str
    component: str
    training_run_id: str
    artifact_sha256: str
    parent_release_id: str
    state: ShadowReleaseState
    created_ts_utc: datetime
    activated_ts_utc: datetime | None
    quarantine_reason: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_model_releases (
    shadow_release_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    training_run_id TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    parent_release_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_ts_utc TEXT NOT NULL,
    activated_ts_utc TEXT,
    quarantine_reason TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_model_run_component
ON shadow_model_releases(component,training_run_id);
CREATE INDEX IF NOT EXISTS idx_shadow_model_component_state
ON shadow_model_releases(component,state,created_ts_utc);
"""


class ShadowModelRegistry:
    """Automatic model lifecycle that is structurally isolated from live execution authority."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.executescript(_SCHEMA)

    def register_and_activate(
        self,
        *,
        component: str,
        training: TrainingRunReport,
        promotion: PromotionDecision,
        parent_release_id: str,
        now: datetime | None = None,
    ) -> ShadowReleaseRecord:
        if not component.strip() or not parent_release_id.strip():
            raise ValueError("component and parent_release_id are required")
        if not training.shadow_ready:
            raise ValueError("training run is not shadow-ready")
        if not training.artifact_sha256:
            raise ValueError("shadow release requires a serialized artifact digest")
        if promotion.status is not PromotionStatus.READY_FOR_OPERATOR_REVIEW:
            raise ValueError("independent promotion evidence gates have not passed")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        identifier = _shadow_release_id(
            component=component,
            training_run_id=training.run_id,
            artifact_sha256=training.artifact_sha256,
            parent_release_id=parent_release_id,
        )
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE shadow_model_releases
                SET state='SUPERSEDED'
                WHERE component=? AND state='ACTIVE_SHADOW' AND shadow_release_id<>?
                """,
                (component, identifier),
            )
            db.execute(
                """
                INSERT INTO shadow_model_releases
                (shadow_release_id,component,training_run_id,artifact_sha256,parent_release_id,
                 state,created_ts_utc,activated_ts_utc)
                VALUES (?,?,?,?,?,'ACTIVE_SHADOW',?,?)
                ON CONFLICT(shadow_release_id) DO UPDATE SET
                    state='ACTIVE_SHADOW',
                    activated_ts_utc=excluded.activated_ts_utc,
                    quarantine_reason=''
                """,
                (
                    identifier,
                    component,
                    training.run_id,
                    training.artifact_sha256,
                    parent_release_id,
                    timestamp.isoformat(),
                    timestamp.isoformat(),
                ),
            )
            db.commit()
        return self.get(identifier)

    def quarantine_active(
        self,
        component: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> ShadowReleaseRecord | None:
        if not reason.strip():
            raise ValueError("quarantine reason is required")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT shadow_release_id FROM shadow_model_releases
                WHERE component=? AND state='ACTIVE_SHADOW'
                ORDER BY activated_ts_utc DESC LIMIT 1
                """,
                (component,),
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            identifier = str(row["shadow_release_id"])
            db.execute(
                """
                UPDATE shadow_model_releases
                SET state='QUARANTINED',quarantine_reason=?
                WHERE shadow_release_id=?
                """,
                (f"{timestamp.isoformat()}|{reason[:900]}", identifier),
            )
            db.commit()
        return self.get(identifier)

    def active(self, component: str) -> ShadowReleaseRecord | None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """
                SELECT * FROM shadow_model_releases
                WHERE component=? AND state='ACTIVE_SHADOW'
                ORDER BY activated_ts_utc DESC LIMIT 1
                """,
                (component,),
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def get(self, identifier: str) -> ShadowReleaseRecord:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM shadow_model_releases WHERE shadow_release_id=?",
                (identifier,),
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return _row_to_record(row)


def _shadow_release_id(
    *,
    component: str,
    training_run_id: str,
    artifact_sha256: str,
    parent_release_id: str,
) -> str:
    material = "|".join(
        (
            "shadow-model-release-v1",
            component,
            training_run_id,
            artifact_sha256,
            parent_release_id,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_to_record(row: sqlite3.Row) -> ShadowReleaseRecord:
    activated = (
        datetime.fromisoformat(str(row["activated_ts_utc"])).astimezone(UTC)
        if row["activated_ts_utc"] is not None
        else None
    )
    return ShadowReleaseRecord(
        shadow_release_id=str(row["shadow_release_id"]),
        component=str(row["component"]),
        training_run_id=str(row["training_run_id"]),
        artifact_sha256=str(row["artifact_sha256"]),
        parent_release_id=str(row["parent_release_id"]),
        state=ShadowReleaseState(str(row["state"])),
        created_ts_utc=datetime.fromisoformat(str(row["created_ts_utc"])).astimezone(UTC),
        activated_ts_utc=activated,
        quarantine_reason=str(row["quarantine_reason"]),
    )
