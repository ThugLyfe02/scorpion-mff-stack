from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.backtest_overfit import (
    OverfitPolicy,
    OverfitStatus,
    evaluate_backtest_overfit,
)
from scorpion.domain import EventKind
from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.execution_meta_labels import (
    ExecutionMetaOutcome,
    build_execution_meta_labels,
    load_execution_meta_labels,
    persist_execution_meta_labels,
)
from scorpion.microstructure import MarketEventKind, OptionMarketEvent, OptionMicrostructureTape
from scorpion.microstructure_forensics import (
    MicroForensicLeg,
    MicroForensicStatus,
    MicrostructureForensicsReport,
)
from scorpion.mondrian_conformal import (
    MondrianPolicy,
    MondrianStatus,
    evaluate_mondrian_calibrator,
    fit_mondrian_calibrator,
)

CONTRACT = "AAPL|CALL|200|2026-09-08"
BASE = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)


def _trade(event_id: str, opened: datetime, return_fraction: str) -> CompletedTrade:
    premium = Decimal("100")
    value = Decimal(return_fraction)
    return CompletedTrade(
        entry_event_id=event_id,
        contract_key=CONTRACT,
        channel_id="channel",
        author_id="author",
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        opened_ts_utc=opened,
        closed_ts_utc=opened + timedelta(minutes=1),
        initial_quantity=1,
        add_count=0,
        trim_count=0,
        gross_premium_in=premium,
        gross_proceeds=premium * (Decimal("1") + value),
        pnl=premium * value,
        return_fraction=value,
        holding_seconds=60.0,
        depth_evidence_complete=True,
    )


def _entry_leg(
    event_id: str,
    *,
    status: MicroForensicStatus = MicroForensicStatus.CERTIFIED_FILL,
    conservative: int = 1,
    possible: int = 1,
) -> MicroForensicLeg:
    decision = BASE + timedelta(milliseconds=250)
    fill_ns = int((decision + timedelta(milliseconds=10)).timestamp() * 1_000_000_000)
    return MicroForensicLeg(
        event_id=event_id,
        message_id=event_id,
        event_kind=EventKind.ENTRY,
        contract_key=CONTRACT,
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        status=status,
        source_ts_utc=BASE,
        decision_ts_utc=decision,
        decision_quote_event_ns=fill_ns - 20_000_000,
        decision_quote_recv_ns=fill_ns - 15_000_000,
        decision_quote_bid=Decimal("1.00"),
        decision_quote_ask=Decimal("1.04"),
        limit_price=Decimal("1.04"),
        order_arrival_ts_utc=decision + timedelta(milliseconds=10),
        requested_quantity=1,
        conservative_fill_quantity=conservative,
        possible_fill_quantity=possible,
        fill_price=Decimal("1.04") if conservative else None,
        fill_certainty=None,
        fill_evidence_event_ns=fill_ns if conservative else None,
        fill_evidence_publisher_id=30 if conservative else None,
    )


def test_pbo_passes_when_same_strategy_remains_best_out_of_sample():
    periods = 32
    panel = {
        "stable": tuple(0.02 for _ in range(periods)),
        "weak": tuple(-0.01 for _ in range(periods)),
        "small": tuple(0.005 for _ in range(periods)),
    }
    report = evaluate_backtest_overfit(panel, policy=OverfitPolicy(slices=8))
    assert report.status is OverfitStatus.PASS
    assert report.probability_of_backtest_overfit == 0.0
    assert report.median_selected_oos_rank_percentile > 0.5


def test_pbo_rejects_slice_specialists_selected_in_sample():
    periods = 32
    slices = 8
    width = periods // slices
    panel: dict[str, tuple[float, ...]] = {}
    for specialist in range(slices):
        values = []
        for index in range(periods):
            current_slice = index // width
            values.append(1.0 if current_slice == specialist else -0.20)
        panel[f"specialist-{specialist}"] = tuple(values)
    report = evaluate_backtest_overfit(
        panel,
        policy=OverfitPolicy(
            slices=slices,
            maximum_probability_of_backtest_overfit=0.25,
        ),
    )
    assert report.status is OverfitStatus.FAIL
    assert report.probability_of_backtest_overfit > 0.50
    assert report.failures


def _probabilities(expected: EventKind, labels: tuple[EventKind, ...]) -> dict[EventKind, float]:
    residual = Decimal("0.01") / Decimal(len(labels) - 1)
    return {
        label: float(Decimal("0.99") if label is expected else residual)
        for label in labels
    }


def test_mondrian_conformal_requires_each_action_class_to_be_safe():
    labels = (
        EventKind.ENTRY,
        EventKind.ADD,
        EventKind.TRIM,
        EventKind.EXIT,
        EventKind.IGNORE,
    )
    policy = MondrianPolicy(
        minimum_calibration_samples_per_required_class=5,
        minimum_evaluation_samples_per_required_class=5,
        minimum_required_class_coverage=0.95,
    )
    calibration_truth = tuple(label for label in labels for _ in range(10))
    calibration_probs = tuple(_probabilities(label, labels) for label in calibration_truth)
    calibrator = fit_mondrian_calibrator(
        calibration_probs,
        calibration_truth,
        labels=labels,
        alpha=0.01,
    )
    evaluation_truth = tuple(label for label in labels for _ in range(10))
    evaluation_probs = tuple(_probabilities(label, labels) for label in evaluation_truth)
    good = evaluate_mondrian_calibrator(
        calibrator,
        evaluation_probs,
        evaluation_truth,
        policy=policy,
    )
    assert good.status is MondrianStatus.QUALIFIED
    assert good.qualified is True

    corrupted = list(evaluation_probs)
    exit_indices = [
        index for index, truth in enumerate(evaluation_truth) if truth is EventKind.EXIT
    ]
    for index in exit_indices:
        corrupted[index] = _probabilities(EventKind.IGNORE, labels)
    bad = evaluate_mondrian_calibrator(
        calibrator,
        tuple(corrupted),
        evaluation_truth,
        policy=policy,
    )
    assert bad.status is MondrianStatus.FAILED
    assert any("class_EXIT_coverage_below_threshold" in item for item in bad.failures)


def test_execution_meta_labels_use_certified_lifecycle_and_persist_by_manifest(tmp_path):
    profitable = _trade("entry-profitable", BASE, "0.10")
    certified = _entry_leg("entry-profitable")
    uncertain = _entry_leg(
        "entry-uncertain",
        status=MicroForensicStatus.RESTING_FILL_UNCERTAIN,
        conservative=0,
        possible=1,
    )
    fill_ns = certified.fill_evidence_event_ns
    assert fill_ns is not None
    tape = OptionMicrostructureTape(
        (
            OptionMarketEvent(
                contract_key=CONTRACT,
                kind=MarketEventKind.QUOTE,
                ts_event_ns=fill_ns + 500_000_000,
                ts_recv_ns=fill_ns + 500_000_000,
                publisher_id=30,
                sequence=1,
                bid=Decimal("1.10"),
                ask=Decimal("1.12"),
                bid_size=10,
                ask_size=10,
            ),
        )
    )
    report = MicrostructureForensicsReport(
        messages_seen=2,
        completeness=(),
        legs=(certified, uncertain),
        completed_trades=(profitable,),
        policy_fingerprint="policy",
        certified_actionable_legs=1,
        uncertain_actionable_legs=1,
    )
    labels = build_execution_meta_labels(report, tape)
    by_event = {label.event_id: label for label in labels}
    assert by_event["entry-profitable"].outcome is ExecutionMetaOutcome.CERTIFIED_PROFITABLE
    assert by_event["entry-profitable"].positive_shadow_target is True
    assert by_event["entry-uncertain"].outcome is ExecutionMetaOutcome.EXECUTION_UNCERTAIN

    path = tmp_path / "meta-labels.db"
    inserted = persist_execution_meta_labels(
        path,
        labels,
        research_manifest_hash="manifest-a",
    )
    assert inserted == 2
    assert persist_execution_meta_labels(
        path,
        labels,
        research_manifest_hash="manifest-a",
    ) == 0
    loaded = load_execution_meta_labels(path, research_manifest_hash="manifest-a")
    assert loaded == labels
