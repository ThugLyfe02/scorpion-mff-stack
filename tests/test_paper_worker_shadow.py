from datetime import UTC, datetime
from decimal import Decimal

import pytest

from scorpion.paper_worker_calibration import WorkerCalibrationReport, WorkerCalibrationRow
from scorpion.paper_worker_shadow import evaluate_shadow_correction


def _report(day: int, *, reviewed_day: int | None = None, blocked: bool = False):
    identity = str(day)
    row = WorkerCalibrationRow(
        2, "INITIAL", "DURABLE_DISPATCH", 6, 4, 10, Decimal("0.2"), "paired",
    )
    return WorkerCalibrationReport(
        "source" + identity, "audit" + identity, "outcome" + identity,
        "model" + identity, "head" + identity, f"2026-11-{day:02}",
        datetime(2026, 11, reviewed_day or day, 22, tzinfo=UTC),
        (row,), 1, 1, 1, 1, Decimal("0.2"), Decimal("0.2"),
        ("blocked",) if blocked else (), "report" + identity,
    )


def test_shadow_only_uses_embargoed_past_and_shrinks_residual():
    reports = tuple(_report(day) for day in (2, 3, 4, 5, 6, 9))
    study = evaluate_shadow_correction(reports)
    target = study.folds[-2]
    assert target.status == "PAIRED_DESCRIPTIVE_ONLY"
    assert target.training_report_sha256 == tuple(f"report{day}" for day in (2, 3, 4, 5, 6))
    assert target.correction == Decimal("0.1")
    assert target.baseline_mae == Decimal("0.2")
    assert target.challenger_mae == Decimal("0.1")
    assert not study.authorizes_live_orders
    assert not study.authorizes_promotion
    assert not study.authorizes_training


def test_late_review_or_blocked_session_cannot_train_shadow():
    reports = tuple(_report(day) for day in (2, 3, 4, 5))
    late = _report(6, reviewed_day=9)
    study = evaluate_shadow_correction((*reports, late, _report(9)))
    assert study.folds[-2].status == "INSUFFICIENT_PRIOR_SESSIONS"
    blocked = _report(6, blocked=True)
    study = evaluate_shadow_correction((*reports, blocked, _report(9)))
    assert study.folds[-2].status == "INSUFFICIENT_PRIOR_SESSIONS"
    assert study.folds[-4].status == "UNQUALIFIED_OR_UNOBSERVED"


def test_duplicate_evidence_refused():
    report = _report(2)
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_shadow_correction((report, report))
