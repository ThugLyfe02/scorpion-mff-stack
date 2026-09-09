from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.contract_terms import ContractTerms, ContractTermsRegistry
from scorpion.domain import EventKind
from scorpion.eligibility import StrategyBucket
from scorpion.execution_attribution import build_execution_attribution_report
from scorpion.execution_trust import FillModelTrust, assess_fill_model_trust
from scorpion.fill_calibration import FillCalibrationReport
from scorpion.microstructure import MarketEventKind, OptionMarketEvent, OptionMicrostructureTape
from scorpion.microstructure_forensics import (
    MicroForensicLeg,
    MicroForensicStatus,
    MicrostructureForensicsReport,
)


def _terms(
    contract_key: str,
    *,
    start: int,
    end: int | None = None,
    multiplier: str = "100",
    adjusted: bool = False,
) -> ContractTerms:
    return ContractTerms(
        contract_key=contract_key,
        valid_from_ns=start,
        valid_to_ns=end,
        multiplier=Decimal(multiplier),
        deliverable="standard" if not adjusted else "adjusted deliverable",
        adjusted=adjusted,
        source="vendor-definition",
    )


def test_contract_terms_are_point_in_time_and_ambiguous_ranges_fail_closed():
    key = "AAPL|CALL|200|2026-09-18"
    registry = ContractTermsRegistry(
        (
            _terms(key, start=0, end=1000),
            _terms(key, start=1000, multiplier="50", adjusted=True),
        )
    )
    first = registry.resolve(key, 500)
    second = registry.resolve(key, 1500)
    assert first is not None and first.standard_equity_option is True
    assert second is not None and second.adjusted is True
    assert second.multiplier == Decimal("50")

    standard = registry.coverage(((key, 500),))
    assert standard.complete is True
    assert standard.standard_only is True

    adjusted = registry.coverage(((key, 1500),))
    assert adjusted.complete is True
    assert adjusted.standard_only is False
    assert adjusted.adjusted == (key,)
    assert adjusted.nonstandard_multiplier == (key,)

    overlapping = ContractTermsRegistry(
        (
            _terms(key, start=0),
            _terms(key, start=500),
        )
    )
    ambiguous = overlapping.coverage(((key, 1000),))
    assert ambiguous.complete is False
    assert ambiguous.ambiguous == (key,)


def test_fill_model_trust_requires_statistical_evidence_not_just_zero_observed_failures():
    thin = FillCalibrationReport(
        samples=10,
        lower_bound_violations=0,
        upper_bound_violations=0,
        interval_coverage_rate=1.0,
        exact_quantity_rate=1.0,
        mean_interval_width=0.0,
        mean_absolute_quantity_error_to_midpoint=0.0,
        aggressive_certified_samples=10,
        aggressive_certified_exact_rate=1.0,
        resting_uncertain_samples=0,
        resting_uncertain_coverage_rate=0.0,
        calibrated=True,
    )
    assert assess_fill_model_trust(thin).status is FillModelTrust.UNCALIBRATED

    mature = FillCalibrationReport(
        samples=500,
        lower_bound_violations=0,
        upper_bound_violations=0,
        interval_coverage_rate=1.0,
        exact_quantity_rate=0.98,
        mean_interval_width=0.04,
        mean_absolute_quantity_error_to_midpoint=0.01,
        aggressive_certified_samples=300,
        aggressive_certified_exact_rate=0.99,
        resting_uncertain_samples=200,
        resting_uncertain_coverage_rate=1.0,
        calibrated=True,
    )
    trusted = assess_fill_model_trust(mature)
    assert trusted.status is FillModelTrust.TRUSTED
    assert trusted.allows_authoritative_research is True
    assert trusted.interval_coverage_lower_bound > 0.98

    violated = FillCalibrationReport(
        samples=500,
        lower_bound_violations=20,
        upper_bound_violations=20,
        interval_coverage_rate=0.92,
        exact_quantity_rate=0.80,
        mean_interval_width=0.5,
        mean_absolute_quantity_error_to_midpoint=0.4,
        aggressive_certified_samples=300,
        aggressive_certified_exact_rate=0.80,
        resting_uncertain_samples=200,
        resting_uncertain_coverage_rate=0.90,
        calibrated=False,
    )
    assessment = assess_fill_model_trust(violated)
    assert assessment.status is FillModelTrust.UNTRUSTED
    assert assessment.allows_authoritative_research is False


def test_execution_attribution_separates_latency_spread_and_post_fill_markout():
    base = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    fill_ns = int((base + timedelta(milliseconds=260)).timestamp() * 1_000_000_000)
    key = "AAPL|CALL|200|2026-09-08"
    tape = OptionMicrostructureTape(
        (
            OptionMarketEvent(
                contract_key=key,
                kind=MarketEventKind.QUOTE,
                ts_event_ns=fill_ns + 100_000_000,
                ts_recv_ns=fill_ns + 100_000_000,
                publisher_id=30,
                sequence=1,
                bid=Decimal("1.07"),
                ask=Decimal("1.09"),
                bid_size=10,
                ask_size=10,
            ),
            OptionMarketEvent(
                contract_key=key,
                kind=MarketEventKind.QUOTE,
                ts_event_ns=fill_ns + 500_000_000,
                ts_recv_ns=fill_ns + 500_000_000,
                publisher_id=30,
                sequence=2,
                bid=Decimal("1.10"),
                ask=Decimal("1.12"),
                bid_size=10,
                ask_size=10,
            ),
        )
    )
    leg = MicroForensicLeg(
        event_id="event-1",
        message_id="message-1",
        event_kind=EventKind.ENTRY,
        contract_key=key,
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        status=MicroForensicStatus.CERTIFIED_FILL,
        source_ts_utc=base,
        decision_ts_utc=base + timedelta(milliseconds=250),
        decision_quote_event_ns=fill_ns - 20_000_000,
        decision_quote_recv_ns=fill_ns - 15_000_000,
        decision_quote_bid=Decimal("1.00"),
        decision_quote_ask=Decimal("1.04"),
        limit_price=Decimal("1.04"),
        order_arrival_ts_utc=base + timedelta(milliseconds=260),
        requested_quantity=1,
        conservative_fill_quantity=1,
        possible_fill_quantity=1,
        fill_price=Decimal("1.04"),
        fill_certainty=None,
        fill_evidence_event_ns=fill_ns,
        fill_evidence_publisher_id=30,
    )
    report = MicrostructureForensicsReport(
        messages_seen=1,
        completeness=(),
        legs=(leg,),
        completed_trades=(),
        policy_fingerprint="policy",
        certified_actionable_legs=1,
        uncertain_actionable_legs=0,
    )
    summary, legs = build_execution_attribution_report(
        report,
        tape,
        horizons_ms=(100, 500),
    )
    assert summary.attributed_legs == 1
    assert summary.mean_source_to_decision_ms == 250.0
    assert summary.mean_decision_to_arrival_ms == 10.0
    assert len(legs) == 1
    assert legs[0].decision_spread_bps is not None
    assert summary.markouts[0].samples == 1
    assert summary.markouts[0].mean_signed_markout_bps > 0
    assert summary.markouts[1].mean_signed_markout_bps > summary.markouts[0].mean_signed_markout_bps
