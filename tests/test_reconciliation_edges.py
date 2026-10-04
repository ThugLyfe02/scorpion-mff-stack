from decimal import Decimal

from scorpion.domain import BookState
from scorpion.reconciliation import ExternalPositionObservation, reconcile_positions


def test_reconciliation_flags_unsupported_external_short_position():
    report = reconcile_positions(
        BookState(),
        (ExternalPositionObservation("AAPL|CALL|200|2026-09-08", -1, Decimal("1.00")),),
    )
    assert report.critical is True
    assert any(
        finding.code == "unsupported_external_short_position" for finding in report.findings
    )


def test_reconciliation_flags_duplicate_external_observations():
    observation = ExternalPositionObservation(
        "AAPL|CALL|200|2026-09-08",
        1,
        Decimal("1.00"),
    )
    report = reconcile_positions(BookState(), (observation, observation))
    assert report.critical is True
    assert any(finding.code == "duplicate_external_observation" for finding in report.findings)
