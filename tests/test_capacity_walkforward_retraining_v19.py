from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.bayesian_changepoint import BayesianChangePointReport, ChangePointStatus
from scorpion.domain import EventKind
from scorpion.drift_retraining import (
    DriftRetrainingObservation,
    DriftRetrainingPolicy,
    RetrainingPlanStatus,
    evaluate_drift_retraining,
    list_challenger_retraining_plans,
    maybe_create_drift_retraining_plan,
)
from scorpion.eligibility import StrategyBucket
from scorpion.execution_forensics import CompletedTrade
from scorpion.liquidity_capacity import (
    CapacityStatus,
    LiquidityCapacityPolicy,
    evaluate_liquidity_capacity,
)
from scorpion.microstructure import MarketEventKind, OptionMarketEvent, OptionMicrostructureTape
from scorpion.microstructure_forensics import (
    MicroForensicLeg,
    MicroForensicStatus,
    MicrostructureForensicsReport,
)
from scorpion.online_drift import DriftSignal
from scorpion.sizing_lab import SizingConstraints
from scorpion.walk_forward import WalkForwardPolicy, evaluate_walk_forward

BASE = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
CONTRACT = "AAPL|CALL|200|2026-10-16"


def _trade(index: int, return_fraction: str, *, long_lived: bool = False) -> CompletedTrade:
    opened = BASE + timedelta(days=index)
    closed = opened + (timedelta(days=12) if long_lived else timedelta(minutes=5))
    premium = Decimal("1000")
    ret = Decimal(return_fraction)
    return CompletedTrade(
        entry_event_id=f"entry-{index}",
        contract_key=f"T{index}|CALL|100|2026-10-16",
        channel_id="channel",
        author_id="author",
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        opened_ts_utc=opened,
        closed_ts_utc=closed,
        initial_quantity=10,
        add_count=0,
        trim_count=0,
        gross_premium_in=premium,
        gross_proceeds=premium * (Decimal("1") + ret),
        pnl=premium * ret,
        return_fraction=ret,
        holding_seconds=(closed - opened).total_seconds(),
        depth_evidence_complete=True,
    )


def _capacity_leg(event_id: str, kind: EventKind, ts: datetime) -> MicroForensicLeg:
    ns = int(ts.timestamp() * 1_000_000_000)
    return MicroForensicLeg(
        event_id=event_id,
        message_id=event_id,
        event_kind=kind,
        contract_key=CONTRACT,
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        status=MicroForensicStatus.CERTIFIED_FILL,
        source_ts_utc=ts,
        decision_ts_utc=ts,
        decision_quote_event_ns=ns,
        decision_quote_recv_ns=ns,
        decision_quote_bid=Decimal("1.00"),
        decision_quote_ask=Decimal("1.20"),
        limit_price=Decimal("1.20") if kind is EventKind.ENTRY else None,
        order_arrival_ts_utc=ts,
        requested_quantity=10,
        conservative_fill_quantity=10,
        possible_fill_quantity=10,
        fill_price=Decimal("1.20") if kind is EventKind.ENTRY else Decimal("1.23"),
        fill_certainty=None,
        fill_evidence_event_ns=ns,
        fill_evidence_publisher_id=30,
    )


def test_capacity_rejects_clip_when_depth_fits_but_impact_erases_edge():
    trades: list[CompletedTrade] = []
    legs: list[MicroForensicLeg] = []
    events: list[OptionMarketEvent] = []
    for index in range(20):
        opened = BASE + timedelta(days=index)
        closed = opened + timedelta(minutes=1)
        entry = _capacity_leg(f"entry-{index}", EventKind.ENTRY, opened)
        exit_leg = _capacity_leg(f"exit-{index}", EventKind.EXIT, closed)
        legs.extend((entry, exit_leg))
        for sequence, ts in enumerate((opened, closed), start=index * 2):
            ns = int(ts.timestamp() * 1_000_000_000)
            events.append(
                OptionMarketEvent(
                    contract_key=CONTRACT,
                    kind=MarketEventKind.QUOTE,
                    ts_event_ns=ns,
                    ts_recv_ns=ns,
                    publisher_id=30,
                    sequence=sequence,
                    bid=Decimal("1.00"),
                    ask=Decimal("1.20"),
                    bid_size=100,
                    ask_size=100,
                )
            )
        trades.append(
            CompletedTrade(
                entry_event_id=f"entry-{index}",
                contract_key=CONTRACT,
                channel_id="channel",
                author_id="author",
                bucket=StrategyBucket.CORE_SINGLE_NAME,
                opened_ts_utc=opened,
                closed_ts_utc=closed,
                initial_quantity=10,
                add_count=0,
                trim_count=0,
                gross_premium_in=Decimal("1200"),
                gross_proceeds=Decimal("1236"),
                pnl=Decimal("36"),
                return_fraction=Decimal("0.03"),
                holding_seconds=60.0,
                depth_evidence_complete=True,
            )
        )
    report = MicrostructureForensicsReport(
        messages_seen=40,
        completeness=(),
        legs=tuple(legs),
        completed_trades=tuple(trades),
        policy_fingerprint="policy",
        certified_actionable_legs=40,
        uncertain_actionable_legs=0,
    )
    capacity = evaluate_liquidity_capacity(
        report,
        OptionMicrostructureTape(tuple(events)),
        policy=LiquidityCapacityPolicy(
            clip_multipliers=(1.0,),
            displayed_depth_haircuts=(1.0,),
            minimum_supported_trades=20,
            minimum_lifecycle_coverage=1.0,
            impact_spread_multiplier=1.0,
            maximum_participation_rate=1.0,
            maximum_mean_impact_fraction=1.0,
        ),
    )
    scenario = capacity.scenarios[0]
    assert scenario.supported_trades == 20
    assert scenario.mean_return > 0
    assert scenario.stressed_mean_return < 0
    assert scenario.status is CapacityStatus.FAIL
    assert capacity.robust is False


def test_purged_walk_forward_removes_overlapping_training_lifecycle():
    trades = tuple(
        _trade(index, "0.08", long_lived=index == 9)
        for index in range(50)
    )
    report = evaluate_walk_forward(
        trades,
        policy=WalkForwardPolicy(
            min_train_samples=10,
            test_size=5,
            min_folds=3,
            embargo=timedelta(0),
            bootstrap_resamples=200,
        ),
        sizing_constraints=SizingConstraints(
            min_samples=10,
            bootstrap_resamples=100,
            trials=100,
        ),
    )
    assert report.passed is True
    assert report.total_purged_train_samples > 0
    assert report.temporal_leakage_detected is False
    assert all(not fold.temporal_overlap_detected for fold in report.folds)


def test_walk_forward_blocks_positive_mean_without_conservative_oos_support():
    returns = ["0.02"] * 10 + ["0.30"] * 15 + ["-0.20"] * 15
    trades = tuple(_trade(index, value) for index, value in enumerate(returns))
    report = evaluate_walk_forward(
        trades,
        policy=WalkForwardPolicy(
            min_train_samples=10,
            test_size=10,
            min_folds=3,
            embargo=timedelta(0),
            minimum_positive_fold_ratio=0.0,
            maximum_worst_fold_loss=1.0,
            bootstrap_resamples=1000,
            bootstrap_block_size=5,
            bootstrap_seed=7,
        ),
        sizing_constraints=SizingConstraints(
            min_samples=10,
            bootstrap_resamples=100,
            trials=100,
        ),
    )
    assert report.pooled_oos_mean > 0
    assert report.pooled_oos_lower_bound <= 0
    assert report.passed is False
    assert any(item.startswith("pooled_oos_lower_bound") for item in report.failures)


def _changepoint(probability: float = 0.95) -> BayesianChangePointReport:
    return BayesianChangePointReport(
        samples=200,
        failures=50,
        overall_failure_rate=0.25,
        posterior_changepoint_probability=probability,
        log_bayes_factor=8.0,
        best_split_index=100,
        pre_failure_rate=0.05,
        post_failure_rate=0.45,
        rate_change=0.40,
        candidate_splits=161,
        status=ChangePointStatus.DEGRADATION_CHANGEPOINT,
    )


def _drift_observation(*, with_changepoint: bool = True) -> DriftRetrainingObservation:
    return DriftRetrainingObservation(
        observed_ts_utc=BASE + timedelta(days=30),
        drift_started_ts_utc=BASE + timedelta(days=25),
        dataset_samples=1500,
        samples_since_drift=200,
        page_hinkley=DriftSignal(100, 0.20, 8.0, True),
        bayesian_changepoint=_changepoint() if with_changepoint else None,
    )


def test_drift_retraining_requires_corroboration_not_single_detector_twitch():
    plan = evaluate_drift_retraining(
        parent_release_id="champion-v1",
        dataset_fingerprint="dataset-v2",
        observation=_drift_observation(with_changepoint=False),
    )
    assert plan.status is RetrainingPlanStatus.ACCUMULATING_EVIDENCE
    assert plan.ready is False
    assert "drift_confirmations:1<2" in plan.reasons


def test_corroborated_drift_reserves_holdout_and_pre_drift_anchor():
    plan = evaluate_drift_retraining(
        parent_release_id="champion-v1",
        dataset_fingerprint="dataset-v2",
        observation=_drift_observation(),
    )
    assert plan.ready is True
    assert plan.status is RetrainingPlanStatus.READY_RESEARCH_CHALLENGER
    assert plan.confirmations == ("page_hinkley", "bayesian_changepoint")
    assert plan.validation_samples == 50
    assert plan.post_drift_training_samples == 150
    assert plan.pre_drift_anchor_samples == 500
    assert plan.purge_embargo_seconds == 1800.0


def test_retraining_ledger_is_durable_and_cooldown_prevents_storms(tmp_path):
    path = tmp_path / "evolution.db"
    first, inserted = maybe_create_drift_retraining_plan(
        path,
        parent_release_id="champion-v1",
        dataset_fingerprint="dataset-v2",
        observation=_drift_observation(),
        policy=DriftRetrainingPolicy(cooldown=timedelta(hours=6)),
    )
    second, inserted_again = maybe_create_drift_retraining_plan(
        path,
        parent_release_id="champion-v1",
        dataset_fingerprint="dataset-v2",
        observation=_drift_observation(),
        policy=DriftRetrainingPolicy(cooldown=timedelta(hours=6)),
    )
    assert first.ready is True
    assert inserted is True
    assert second.status is RetrainingPlanStatus.COOLDOWN
    assert inserted_again is False
    rows = list_challenger_retraining_plans(path)
    assert len(rows) == 1
    assert rows[0]["plan_id"] == first.plan_id
