import asyncio
import sqlite3

from scorpion.integrity import IntegrityLedger
from scorpion.pipeline import Pipeline
from scorpion.store import Store


def test_integrity_ledger_matches_durable_source_rows(tmp_path, raw_factory):
    path = tmp_path / "evidence.db"
    store = Store(path)
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))

    verification = IntegrityLedger(path).verify_database()
    assert verification.ok is True
    assert verification.checked == 1
    assert verification.legacy_uncovered_signals == 0
    assert verification.failures == ()


def test_integrity_source_verification_detects_audit_mutation(tmp_path, raw_factory):
    path = tmp_path / "tamper.db"
    store = Store(path)
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))

    with sqlite3.connect(path) as db:
        db.execute("UPDATE decision_audit SET parser_rule='tampered'")

    verification = IntegrityLedger(path).verify_database()
    assert verification.ok is False
    assert any("source_evidence_mismatch" in item for item in verification.failures)
