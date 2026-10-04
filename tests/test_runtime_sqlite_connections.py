import sqlite3

import pytest

from scorpion.durable_ingress import append_raw_with_receipt
from scorpion.failure_quarantine import (
    load_quarantined,
    quarantined_count,
    record_processing_failure,
    requeue_quarantined,
)
from scorpion.integrity import IntegrityLedger, verify_database_evidence
from scorpion.pipeline import Pipeline
from scorpion.processing_order import (
    inspect_processing_order,
    load_pending_raw_in_receipt_order,
    load_signals_in_processing_order,
    register_raw_receipt,
)
from scorpion.state_checkpoint import create_state_checkpoint
from scorpion.store import Store


@pytest.mark.parametrize(
    "entry_point",
    (
        "store",
        "durable_ingress",
        "receipt_registration",
        "signal_order_load",
        "pending_order_load",
        "order_inspection",
        "failure_record",
        "failure_requeue",
        "quarantine_load",
        "quarantine_count",
        "integrity_open",
        "integrity_verification",
        "checkpoint",
        "pipeline_restart",
    ),
)
def test_runtime_writers_restore_connection_policy_before_mutation(
    tmp_path, raw_factory, monkeypatch, entry_point
):
    """A compatible SQLite build/factory must not silently weaken durable runtime writes."""
    store = Store(tmp_path / "runtime.db")
    raw = raw_factory("synthetic diagnostic", message_id="sqlite-policy-probe")
    store.append_raw(raw)
    record_processing_failure(store.path, raw.revision_id, "synthetic", maximum_attempts=1)
    real_connect = sqlite3.connect
    observed = []
    connections = []

    class WitnessConnection(sqlite3.Connection):
        def witness(self, sql):
            if sql.lstrip().upper().startswith(
                ("BEGIN", "CREATE", "ALTER", "INSERT", "UPDATE", "DELETE")
            ):
                observed.append(
                    tuple(
                        sqlite3.Connection.execute(self, f"PRAGMA {name}").fetchone()[0]
                        for name in ("synchronous", "foreign_keys", "busy_timeout")
                    )
                )

        def execute(self, sql, parameters=()):
            self.witness(sql)
            return super().execute(sql, parameters)

        def executescript(self, sql):
            # Some startup readers create/migrate tables. They are writers too.
            self.witness(sql)
            return super().executescript(sql)

    def weak_default_connect(*args, **kwargs):
        db = real_connect(*args, **kwargs, factory=WitnessConnection)
        db.execute("PRAGMA synchronous=OFF")
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA busy_timeout=1")
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", weak_default_connect)
    operations = {
        "store": lambda: store.set_halt(True, "synthetic"),
        "durable_ingress": lambda: append_raw_with_receipt(store.path, raw),
        "receipt_registration": lambda: register_raw_receipt(store.path, raw.revision_id),
        "signal_order_load": lambda: load_signals_in_processing_order(store.path),
        "pending_order_load": lambda: load_pending_raw_in_receipt_order(store.path),
        "order_inspection": lambda: inspect_processing_order(store.path),
        "failure_record": lambda: record_processing_failure(store.path, raw.revision_id, "test"),
        "failure_requeue": lambda: requeue_quarantined(
            store.path, raw.revision_id, operator="test", note="synthetic"
        ),
        "quarantine_load": lambda: load_quarantined(store.path),
        "quarantine_count": lambda: quarantined_count(store.path),
        "integrity_open": lambda: IntegrityLedger(store.path),
        "integrity_verification": lambda: verify_database_evidence(store.path),
        "checkpoint": lambda: create_state_checkpoint(store.path),
        "pipeline_restart": lambda: Pipeline(store),
    }
    try:
        operations[entry_point]()
        assert observed, "test must exercise a runtime write or schema migration"
        assert all(settings == (2, 1, 5000) for settings in observed), observed
    finally:
        for db in connections:
            db.close()
