"""Evidence writer failures and fill-vs-intent truth are explicit."""

from __future__ import annotations

import hashlib
import json
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from scorpion.broker import PaperIntentBroker
from scorpion.domain import Effect, EffectKind
from scorpion.recovery import recover_paper_session_journal
from scorpion.session_service import (
    Journal,
    JournalError,
    journal_checkpoint_path,
    journal_receipt_path,
    read_verified_paper_records,
    verify_paper_journal,
)


def test_intent_is_not_a_fill() -> None:
    effect = Effect(EffectKind.PROPOSE_OPEN, "XYZ", "synthetic", 1, "test")
    result = PaperIntentBroker().execute(effect, 4, Decimal("7.41"))
    assert result.status == "PAPER_INTENT_ONLY"
    assert result.fill_price is None
    assert result.requested_limit == Decimal("7.41")


def test_journal_ack_and_exclusive_session(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    durable: list[int] = []
    journal = Journal(path, on_durable=lambda seq, _: durable.append(seq))
    row: dict[str, object] = {"kind": "intent"}
    assert journal.submit(row) == 1
    assert journal.submit({"kind": "decision"}) == 2
    journal.close()
    assert "seq" not in row
    assert durable == [1, 2]
    assert [json.loads(line)["seq"] for line in path.read_text().splitlines()] == [1, 2]
    with pytest.raises(JournalError, match="FileExistsError"):
        Journal(path)
    with pytest.raises(JournalError, match="closed"):
        journal.submit({"kind": "late"})


def test_fsync_failure_is_visible_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_: int) -> None:
        raise OSError("synthetic disk fault")

    monkeypatch.setattr("scorpion.session_service.os.fsync", fail)
    journal = Journal(tmp_path / "session.jsonl")
    journal.submit({"kind": "intent"})
    with pytest.raises(JournalError, match="journal_writer:OSError"):
        journal.close()
    with pytest.raises(JournalError, match="journal_writer:OSError"):
        journal.check()


def test_backpressure_is_not_a_silent_drop(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked_callback(seq: int, _: int) -> None:
        if seq == 1:
            started.set()
            if not release.wait(5):
                raise RuntimeError("test barrier timed out")

    journal = Journal(tmp_path / "session.jsonl", max_pending=1, batch=1,
                      on_durable=blocked_callback)
    try:
        assert journal.submit({"kind": "first"}) == 1
        assert started.wait(5)
        assert journal.submit({"kind": "second"}) == 2
        with pytest.raises(JournalError, match="journal_backpressure"):
            journal.submit({"kind": "overflow"})
    finally:
        release.set()
        journal.close()
    assert [json.loads(line)["seq"] for line in journal.path.read_text().splitlines()] == [1, 2]


def test_receipt_binds_exact_bytes_and_record_count(tmp_path: Path) -> None:
    path = tmp_path / "sealed.jsonl"
    journal = Journal(path)
    journal.submit({"kind": "intent"})
    assert not journal_receipt_path(path).exists()
    journal.close()
    receipt = verify_paper_journal(path)
    assert receipt["records"] == 1
    assert receipt["authority"] == "PAPER_INTENT_ONLY"
    path.write_bytes(path.read_bytes().replace(b"intent", b"edited"))
    with pytest.raises(JournalError, match="receipt_mismatch"):
        verify_paper_journal(path)


def test_receipt_missing_or_truncated_stream_refuses(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.jsonl"
    journal = Journal(path)
    journal.submit({"kind": "intent"})
    journal.close()
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    with pytest.raises(JournalError, match="incomplete_record"):
        verify_paper_journal(path)
    journal_receipt_path(path).unlink()
    with pytest.raises(JournalError, match="receipt_missing"):
        verify_paper_journal(path)


def test_admitted_nested_values_cannot_change_after_submit(tmp_path: Path) -> None:
    path = tmp_path / "immutable.jsonl"
    journal = Journal(path)
    nested = {"bid": "1.20"}
    journal.submit({"kind": "quote", "evidence": nested})
    nested["bid"] = "9.99"
    journal.close()
    row = json.loads(path.read_text())
    assert row["evidence"]["bid"] == "1.20"
    assert verify_paper_journal(path)["records"] == 1


def test_nonfinite_record_refuses_without_consuming_sequence(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "finite.jsonl")
    with pytest.raises(JournalError, match="invalid_record"):
        journal.submit({"kind": "bad", "value": float("nan")})
    assert journal.submit({"kind": "good"}) == 1
    journal.close()
    assert verify_paper_journal(journal.path)["records"] == 1


def test_interrupted_prefix_recovery_preserves_every_source_byte(tmp_path: Path) -> None:
    path = tmp_path / "interrupted.jsonl"
    journal = Journal(path)
    journal.submit({"kind": "intent", "call_id": "synthetic"})
    journal.close()
    original = path.read_bytes()
    # Remove clean-close evidence and model a write after the durable checkpoint.
    journal_receipt_path(path).unlink()
    tail = b'{"seq":2,"kind":"unfinished'
    path.write_bytes(original + tail)
    dest = tmp_path / "quarantine"
    report = recover_paper_session_journal(path, dest)
    assert report["status"] == "VERIFIED_PREFIX_QUARANTINED"
    assert report["unverified_tail_bytes"] == len(tail)
    assert not report["resume_authorized"]
    assert (dest / "verified-prefix.jsonl").read_bytes() == original
    assert (dest / "unverified-tail.bin").read_bytes() == tail
    assert path.read_bytes() == original + tail
    with pytest.raises(FileExistsError):
        recover_paper_session_journal(path, dest)


def test_corrupt_checkpoint_or_prefix_never_creates_recovery(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.jsonl"
    journal = Journal(path)
    journal.submit({"kind": "intent"})
    journal.close()
    journal_receipt_path(path).unlink()
    path.write_bytes(path.read_bytes().replace(b"intent", b"edited"))
    dest = tmp_path / "refused"
    with pytest.raises(JournalError, match="prefix_mismatch"):
        recover_paper_session_journal(path, dest)
    assert not dest.exists()
    journal_checkpoint_path(path).unlink()
    with pytest.raises(JournalError, match="checkpoint_unavailable"):
        recover_paper_session_journal(path, dest)


def test_sealed_copy_keeps_clean_close_distinct_from_recovery(tmp_path: Path) -> None:
    path = tmp_path / "closed.jsonl"
    journal = Journal(path)
    journal.submit({"kind": "intent"})
    journal.close()
    report = recover_paper_session_journal(path, tmp_path / "copy")
    assert report["status"] == "SEALED_COPY"
    assert report["unverified_tail_bytes"] == 0
    assert not report["resume_authorized"]


@pytest.mark.parametrize("bad", [
    b'{"seq":1,"seq":1,"kind":"x"}\n',
    b'{"seq":1,"kind":"x","nested":{"a":1,"a":2}}\n',
    b'{"seq":1,"kind":"x","value":NaN}\n',
    b'{"seq":1,"kind":"x","value":1e999}\n',
])
def test_ambiguous_record_refused_even_with_matching_unkeyed_receipt(tmp_path, bad):
    path = tmp_path / "ambiguous.jsonl"
    path.write_bytes(bad)
    journal_receipt_path(path).write_text(json.dumps({
        "schema": "scorpion.paper-intent-journal-receipt.v1", "authority": "PAPER_INTENT_ONLY",
        "records": 1, "bytes": len(bad), "sha256": hashlib.sha256(bad).hexdigest(),
    }))
    with pytest.raises(JournalError):
        read_verified_paper_records(path)


def test_lossless_capacity_fault_preserves_prefix_without_clean_receipt(tmp_path):
    path = tmp_path / "bounded.jsonl"
    journal = Journal(path, max_bytes=100, require_lossless=True)
    journal.submit({"kind": "first"})
    with pytest.raises(JournalError, match="total_byte_bound"):
        journal.submit({"kind": "oversize", "value": "x" * 100})
    with pytest.raises(JournalError, match="total_byte_bound"):
        journal.submit({"kind": "retry"})
    with pytest.raises(JournalError, match="total_byte_bound"):
        journal.close()
    assert not journal_receipt_path(path).exists()
    recovered = recover_paper_session_journal(path, tmp_path / "recovered")
    assert recovered["status"] == "VERIFIED_PREFIX_QUARANTINED"
    assert recovered["verified_prefix"]["records"] == 1


def test_pending_byte_budget_bounds_memory_even_before_record_count(tmp_path):
    journal = Journal(tmp_path / "bounded.jsonl", max_pending_bytes=80)
    with pytest.raises(JournalError, match="pending_byte_bound"):
        journal.submit({"kind": "oversized", "value": "x" * 100})
    journal.submit({"kind": "ok"})
    journal.close()
    assert verify_paper_journal(journal.path)["records"] == 1
