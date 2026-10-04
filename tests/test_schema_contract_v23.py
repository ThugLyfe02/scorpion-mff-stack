import sqlite3

from scorpion.schema_contract import inspect_schema
from scorpion.store import Store


def test_schema_contract_rejects_partial_deployment_feature_group(tmp_path):
    path = tmp_path / "partial-rollout.db"
    Store(path)
    with sqlite3.connect(str(path)) as db:
        db.execute(
            """
            CREATE TABLE deployment_rollouts (
                rollout_id TEXT PRIMARY KEY,
                component TEXT NOT NULL,
                candidate_release_id TEXT NOT NULL,
                previous_release_id TEXT NOT NULL,
                active_release_id TEXT NOT NULL,
                dossier_id TEXT NOT NULL,
                authorization_id TEXT NOT NULL,
                authorization_expires_ts_utc TEXT NOT NULL,
                evidence_bundle_hash TEXT NOT NULL,
                preparation_audit_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                rollback_state TEXT NOT NULL,
                generation INTEGER NOT NULL,
                created_ts_utc TEXT NOT NULL,
                updated_ts_utc TEXT NOT NULL
            )
            """
        )
    report = inspect_schema(path)
    assert report.compatible is False
    assert any(
        failure.startswith("incomplete_advanced_group:deployment_rollout:")
        for failure in report.failures
    )
