import sqlite3

from scorpion.integrity import IntegrityLedger, build_chain, verify_chain


def test_in_memory_integrity_chain_verifies():
    chain = build_chain(
        [
            ("event-1", {"kind": "ENTRY", "contract": "QQQ"}),
            ("event-2", {"kind": "EXIT", "contract": "QQQ"}),
        ]
    )
    assert verify_chain(chain).ok is True


def test_sqlite_integrity_ledger_detects_tampering(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = IntegrityLedger(path)
    ledger.append("event-1", {"kind": "ENTRY"})
    ledger.append("event-2", {"kind": "EXIT"})
    assert ledger.verify().ok is True

    db = sqlite3.connect(path)
    try:
        db.execute("UPDATE integrity_ledger SET previous_hash='bad' WHERE sequence=2")
        db.commit()
    finally:
        db.close()
    verification = ledger.verify()
    assert verification.ok is False
    assert verification.failure_sequence == 2
