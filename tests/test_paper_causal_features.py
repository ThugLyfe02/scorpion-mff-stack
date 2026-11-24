"""Forward evidence stays causal, immutable and explicitly unlabeled."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from scorpion.causal_features import load_paper_intent_features
from scorpion.counterfactual import compare_paper_intent_alternatives
from scorpion.session_service import Journal, JournalError

T = "2026-11-24T15:00:00+00:00"
T1 = "2026-11-24T15:00:01+00:00"
T2 = "2026-11-24T15:00:02+00:00"
RULES = "a" * 64
EVENT_NS = int(datetime.fromisoformat(T1).timestamp()) * 1_000_000_000


def decision() -> dict[str, object]:
    return {
        "kind": "decision", "action": "schedule_entry", "call_id": "fixture",
        "ticker": "XYZ", "rules_sha256": RULES, "source_ts_utc": T,
        "received_ts_utc": T, "ts_utc": T, "revision_id": "b" * 64,
        "content_sha256": "c" * 64,
    }


def intent(chase: bool = False) -> dict[str, object]:
    return {
        "kind": "intent", "call_id": "fixture", "ticker": "XYZ", "rules_sha256": RULES,
        "mode": "PAPER", "status": "PAPER_INTENT_ONLY", "chase": chase,
        "conditional_on": "initial_unfilled" if chase else None,
        "limit": "10.10" if chase else "10.00", "quantity": 9 if chase else 10,
        "reference_price": "10.00", "ts_utc": T2 if chase else T1,
        "quote_evidence": {
            "bid": "9.99", "ask": "10.10", "observed_ts_utc": T1,
            "ts_event_ns": EVENT_NS, "recv_mono_ns": 100_000_000,
            "decision_mono_ns": 1_100_000_000 if chase else 110_000_000,
            "source_age_ns": 10_000_000,
        },
    }


def seal(path: Path, *records: dict[str, object]) -> Path:
    journal = Journal(path)
    for row in records:
        journal.submit(row)
    journal.close()
    return path


def test_feature_rows_bind_verified_prior_decision_and_never_have_labels(tmp_path: Path) -> None:
    path = seal(tmp_path / "session.jsonl", decision(), intent(), intent(True))
    report = load_paper_intent_features(path)
    assert len(report.rows) == 2
    assert report.rows[0].decision_seq == 1
    assert report.rows[0].intent_seq == 2
    assert report.rows[0].features["quote_age_ns"] == 20_000_000
    assert report.rows[0].label_status == "UNLABELED"
    assert report.labeled_training_rows == 0
    assert report == load_paper_intent_features(path)


def test_counterfactual_is_requested_price_delta_not_profit(tmp_path: Path) -> None:
    path = seal(tmp_path / "session.jsonl", decision(), intent(), intent(True))
    report = compare_paper_intent_alternatives(path)
    assert len(report.pairs) == 1
    assert report.pairs[0].requested_limit_change_bps == Decimal("100")
    assert report.pairs[0].requested_quantity_change == -1
    assert report.pairs[0].modeled_pnl_delta is None


def test_missing_prior_decision_and_future_quote_refused(tmp_path: Path) -> None:
    row = intent()
    quote = dict(row["quote_evidence"])  # type: ignore[arg-type]
    quote["observed_ts_utc"] = "2026-11-25T15:00:00+00:00"
    row["quote_evidence"] = quote
    path = seal(tmp_path / "session.jsonl", intent(), decision(), row, intent(True))
    report = load_paper_intent_features(path)
    assert not report.rows
    assert dict(report.refusals) == {
        "missing_prior_decision": 1, "invalid_intent_evidence": 1,
        "missing_qualified_initial": 1,
    }


def test_tamper_never_enters_research_dataset(tmp_path: Path) -> None:
    path = seal(tmp_path / "session.jsonl", decision(), intent())
    path.write_bytes(path.read_bytes().replace(b"XYZ", b"ABC"))
    with pytest.raises(JournalError, match="receipt_mismatch"):
        load_paper_intent_features(path)
