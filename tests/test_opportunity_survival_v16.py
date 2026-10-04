from datetime import UTC, datetime
from decimal import Decimal

from scorpion.domain import EventKind
from scorpion.eligibility import StrategyBucket
from scorpion.microstructure import MarketEventKind, OptionMarketEvent, OptionMicrostructureTape
from scorpion.microstructure_forensics import (
    MicroForensicLeg,
    MicroForensicStatus,
    MicrostructureForensicsReport,
)
from scorpion.opportunity_survival import (
    OpportunityExtractionPolicy,
    OpportunityObservation,
    OpportunitySurvivalPolicy,
    OpportunitySurvivalStatus,
    evaluate_opportunity_survival,
    extract_opportunity_observations,
)

BASE = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
BASE_NS = int(BASE.timestamp() * 1_000_000_000)
CONTRACT = "AAPL|CALL|200|2026-09-18"


def test_kaplan_meier_preserves_right_censoring_and_median_half_life():
    rows = tuple(
        OpportunityObservation(
            event_id=f"e-{index}",
            group="CORE",
            duration_ms=100.0 if index < 20 else 1000.0,
            opportunity_lost=index < 20,
        )
        for index in range(30)
    )
    report = evaluate_opportunity_survival(
        rows,
        policy=OpportunitySurvivalPolicy(
            minimum_samples=30,
            horizons_ms=(50.0, 100.0, 500.0, 1000.0),
            restricted_mean_horizon_ms=1000.0,
        ),
    )
    group = report.groups[0]
    assert group.status is OpportunitySurvivalStatus.QUALIFIED
    assert group.observed_losses == 20
    assert group.censored == 10
    assert group.median_survival_ms == 100.0
    survival = dict(group.survival_at_horizons)
    assert survival[50.0] == 1.0
    assert 0.32 < survival[100.0] < 0.34
    assert survival[1000.0] == survival[100.0]


def _entry_leg(*, limit: str = "1.10") -> MicroForensicLeg:
    return MicroForensicLeg(
        event_id="entry-1",
        message_id="message-1",
        event_kind=EventKind.ENTRY,
        contract_key=CONTRACT,
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        status=MicroForensicStatus.CERTIFIED_FILL,
        source_ts_utc=BASE,
        decision_ts_utc=BASE,
        decision_quote_event_ns=BASE_NS,
        decision_quote_recv_ns=BASE_NS,
        decision_quote_bid=Decimal("1.00"),
        decision_quote_ask=Decimal("1.05"),
        limit_price=Decimal(limit),
        order_arrival_ts_utc=BASE,
        requested_quantity=1,
        conservative_fill_quantity=1,
        possible_fill_quantity=1,
        fill_price=Decimal("1.05"),
        fill_certainty=None,
        fill_evidence_event_ns=BASE_NS,
        fill_evidence_publisher_id=30,
    )


def _quote(offset_ms: int, ask: str, sequence: int) -> OptionMarketEvent:
    timestamp = BASE_NS + offset_ms * 1_000_000
    return OptionMarketEvent(
        contract_key=CONTRACT,
        kind=MarketEventKind.QUOTE,
        ts_event_ns=timestamp,
        ts_recv_ns=timestamp,
        publisher_id=30,
        sequence=sequence,
        bid=Decimal("1.00"),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=10,
    )


def test_tick_extractor_measures_first_loss_of_immediate_marketability():
    tape = OptionMicrostructureTape(
        (
            _quote(0, "1.05", 1),
            _quote(100, "1.08", 2),
            _quote(250, "1.12", 3),
            _quote(400, "1.07", 4),
        )
    )
    report = MicrostructureForensicsReport(
        messages_seen=1,
        completeness=(),
        legs=(_entry_leg(),),
        completed_trades=(),
        policy_fingerprint="policy",
        certified_actionable_legs=1,
        uncertain_actionable_legs=0,
    )
    observations = extract_opportunity_observations(
        report,
        tape,
        policy=OpportunityExtractionPolicy(),
    )
    assert len(observations) == 1
    assert observations[0].opportunity_lost is True
    assert observations[0].duration_ms == 250.0


def test_tick_extractor_marks_zero_when_marketability_already_lost():
    tape = OptionMicrostructureTape((_quote(0, "1.20", 1),))
    report = MicrostructureForensicsReport(
        messages_seen=1,
        completeness=(),
        legs=(_entry_leg(),),
        completed_trades=(),
        policy_fingerprint="policy",
        certified_actionable_legs=1,
        uncertain_actionable_legs=0,
    )
    observations = extract_opportunity_observations(report, tape)
    assert observations[0].duration_ms == 0.0
    assert observations[0].opportunity_lost is True
