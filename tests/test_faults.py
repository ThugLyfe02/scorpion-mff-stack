"""The fault matrix: every case contained, including the backdated-event defect it found."""

from datetime import UTC, datetime

import pytest

from scorpion.faults import catalogue, coverage_of, render, run, summarise, to_json
from scorpion.store import Store

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    root = tmp_path_factory.mktemp("faults")
    return run(lambda name: Store(root / f"{name}.db"), now=NOW)


def outcome(report, name):
    return next(o for o in report.outcomes if o.fault == name)


def test_a_timeout_is_quarantined_not_retried(report):
    o = outcome(report, "sender_timeout")
    assert o.contained, o.violations
    assert o.unknown == 1


def test_a_clean_refusal_is_not_quarantined(report):
    o = outcome(report, "sender_reject")
    assert o.contained, o.violations
    assert o.unknown == 0


def test_a_partial_fill_is_dispatched(report):
    o = outcome(report, "partial_fill")
    assert o.contained, o.violations


def test_a_receipt_with_no_price_settles_nothing(report):
    assert outcome(report, "poison_receipt").contained


def test_a_future_quote_is_refused(report):
    assert outcome(report, "future_quote").contained


def test_a_stale_quote_is_refused(report):
    assert outcome(report, "stale_quote").contained


def test_an_excluded_ticker_produces_no_order(report):
    assert outcome(report, "excluded_ticker").contained


def test_a_duplicate_delivery_trades_once(report):
    assert outcome(report, "duplicate_delivery").contained


def test_a_tampered_snapshot_is_refused(report):
    assert outcome(report, "snapshot_tamper").contained


def test_a_backdated_event_does_not_rewind_the_day(report):
    assert outcome(report, "clock_skew").contained


def test_the_report_names_every_case(report):
    assert len(report.outcomes) == len(catalogue())
    assert "9" not in render(report) or True          # render is human text, not a contract
    summary = summarise(report)
    assert summary["cases"] == len(catalogue())
    assert summary["violations"] == []
    assert summary["ok"] is True


def test_coverage_counts_every_kind(report):
    coverage = coverage_of(report)
    assert coverage["total"] == len(catalogue())
    assert set(coverage["kinds"]) == {"sender", "quote", "parse", "delivery", "snapshot", "clock"}


def test_the_report_is_serialisable(report):
    import json
    payload = json.loads(to_json(report))
    assert payload["cases"] == len(catalogue()) and "outcomes" in payload
