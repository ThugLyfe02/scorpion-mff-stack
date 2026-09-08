from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .governance import PromotionDecision, PromotionStatus


class ReleaseState(StrEnum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class ReleaseRecord:
    release_id: str
    component: str
    artifact_hash: str
    policy_fingerprint: str
    research_manifest_hash: str
    previous_release_id: str
    state: ReleaseState
    registered_ts_utc: datetime
    activated_ts_utc: datetime | None
    activated_by: str
    quarantine_reason: str


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    component: str
    quarantined_release_id: str
    rollback_release_id: str | None
    requires_operator_activation: bool
    reason: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS component_releases (
    release_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    research_manifest_hash TEXT NOT NULL,
    previous_release_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    registered_ts_utc TEXT NOT NULL,
    activated_ts_utc TEXT,
    activated_by TEXT NOT NULL DEFAULT '',
    quarantine_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_component_releases_component_state
ON component_releases(component,state,registered_ts_utc);
"""


def _release_id(
    *,
    component: str,
    artifact_hash: str,
    policy_fingerprint: str,
    research_manifest_hash: str,
) -> str:
    material = "|".join(
        (
            "release-v1",
            component,
            artifact_hash,
            policy_fingerprint,
            research_manifest_hash,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ReleaseRegistry:
    """Immutable release lineage with fail-safe quarantine.

    Registration and activation are distinct. Automated monitoring may quarantine a release,
    but selecting/activating a rollback target still requires an explicit operator action.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def register(
        self,
        *,
        component: str,
        artifact_hash: str,
        policy_fingerprint: str,
        research_manifest_hash: str,
        previous_release_id: str = "",
        now: datetime | None = None,
    ) -> ReleaseRecord:
        if not all(
            value.strip()
            for value in (component, artifact_hash, policy_fingerprint, research_manifest_hash)
        ):
            raise ValueError("component and provenance hashes are required")
        now = (now or datetime.now(UTC)).astimezone(UTC)
        identifier = _release_id(
            component=component,
            artifact_hash=artifact_hash,
            policy_fingerprint=policy_fingerprint,
            research_manifest_hash=research_manifest_hash,
        )
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO component_releases
                (release_id,component,artifact_hash,policy_fingerprint,research_manifest_hash,
                 previous_release_id,state,registered_ts_utc)
                VALUES (?,?,?,?,?,?,'CANDIDATE',?)
                """,
                (
                    identifier,
                    component,
                    artifact_hash,
                    policy_fingerprint,
                    research_manifest_hash,
                    previous_release_id,
                    now.isoformat(),
                ),
            )
        return self.get(identifier)

    def activate(
        self,
        identifier: str,
        *,
        operator: str,
        promotion: PromotionDecision,
        now: datetime | None = None,
    ) -> ReleaseRecord:
        if not operator.strip():
            raise ValueError("operator identity is required")
        if promotion.status is not PromotionStatus.READY_FOR_OPERATOR_REVIEW:
            raise ValueError("release cannot activate before automated evidence gates pass")
        now = (now or datetime.now(UTC)).astimezone(UTC)
        target = self.get(identifier)
        if target.state is ReleaseState.QUARANTINED:
            raise ValueError("quarantined release cannot be activated")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE component_releases
                SET state='SUPERSEDED'
                WHERE component=? AND state='ACTIVE' AND release_id<>?
                """,
                (target.component, identifier),
            )
            cursor = db.execute(
                """
                UPDATE component_releases
                SET state='ACTIVE',activated_ts_utc=?,activated_by=?
                WHERE release_id=? AND state IN ('CANDIDATE','SUPERSEDED')
                """,
                (now.isoformat(), operator, identifier),
            )
            if cursor.rowcount != 1:
                db.execute("ROLLBACK")
                raise ValueError("release is not activatable")
            db.execute("COMMIT")
        return self.get(identifier)

    def quarantine_active(
        self,
        component: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> RollbackPlan | None:
        if not reason.strip():
            raise ValueError("quarantine reason is required")
        now = (now or datetime.now(UTC)).astimezone(UTC)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                """
                SELECT release_id,previous_release_id
                FROM component_releases
                WHERE component=? AND state='ACTIVE'
                ORDER BY activated_ts_utc DESC LIMIT 1
                """,
                (component,),
            ).fetchone()
            if active is None:
                db.execute("ROLLBACK")
                return None
            identifier = str(active["release_id"])
            previous = str(active["previous_release_id"] or "")
            db.execute(
                """
                UPDATE component_releases
                SET state='QUARANTINED',quarantine_reason=?
                WHERE release_id=?
                """,
                (f"{now.isoformat()}|{reason[:900]}", identifier),
            )
            db.execute("COMMIT")
        rollback_id = previous or self._latest_superseded(component, exclude=identifier)
        return RollbackPlan(
            component=component,
            quarantined_release_id=identifier,
            rollback_release_id=rollback_id,
            requires_operator_activation=True,
            reason=(
                "active release quarantined automatically; prior known release is only a "
                "rollback candidate until explicitly activated by an operator"
            ),
        )

    def _latest_superseded(self, component: str, *, exclude: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT release_id FROM component_releases
                WHERE component=? AND state='SUPERSEDED' AND release_id<>?
                ORDER BY activated_ts_utc DESC,registered_ts_utc DESC LIMIT 1
                """,
                (component, exclude),
            ).fetchone()
        return str(row["release_id"]) if row is not None else None

    def get(self, identifier: str) -> ReleaseRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM component_releases WHERE release_id=?",
                (identifier,),
            ).fetchone()
        if row is None:
            raise KeyError(identifier)
        activated = (
            datetime.fromisoformat(str(row["activated_ts_utc"])).astimezone(UTC)
            if row["activated_ts_utc"] is not None
            else None
        )
        return ReleaseRecord(
            release_id=str(row["release_id"]),
            component=str(row["component"]),
            artifact_hash=str(row["artifact_hash"]),
            policy_fingerprint=str(row["policy_fingerprint"]),
            research_manifest_hash=str(row["research_manifest_hash"]),
            previous_release_id=str(row["previous_release_id"]),
            state=ReleaseState(str(row["state"])),
            registered_ts_utc=datetime.fromisoformat(str(row["registered_ts_utc"])).astimezone(
                UTC
            ),
            activated_ts_utc=activated,
            activated_by=str(row["activated_by"]),
            quarantine_reason=str(row["quarantine_reason"]),
        )
