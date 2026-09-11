from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.fill_calibration import (
    evaluate_fill_calibration,
    record_fill_observation,
    record_fill_prediction,
)
from scorpion.history_archive import ArchivedDiscordMessage
from scorpion.microstructure import FillCertainty, MicroFillEnvelope
from scorpion.microstructure_live import LiveShadowRecorder, ShadowExecutionIntent, ShadowIntentSide
from scorpion.microstructure_windows import build_acquisition_plan

GUILD = "912747256736800838"
CHANNEL = "968352649437126676"
AUTHOR = "author"
CONTRACT = "AAPL|CALL|200|2026-09-08"
BASE = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)


def _message(message_id: str, offset_s: int, content: str) -> ArchivedDiscordMessage:
    return ArchivedDiscordMessage(
        message_id=message_id,
        guild_id=GUILD,
        channel_id=CHANNEL,
        author_id=AUTHOR,
        source_ts_utc=BASE + timedelta(seconds=offset_s),
        content=content,
    )


def _fill(lower: int, upper: int, certainty: FillCertainty) -> MicroFillEnvelope:
    return MicroFillEnvelope(
        side="BUY",
        requested_quantity=3,
        lower_bound_quantity=lower,
        upper_bound_quantity=upper,
        modeled_price=Decimal("1.00"),
        certainty=certainty,
        order_arrival_ns=1,
        evidence_event_ns=2,
        evidence_publisher_id=30,
        depth_known=True,
        reason="test",
    )


def test_acquisition_plan_merges_overlapping_windows_and_is_deterministic():
    messages = (
        _message("1", 0, "AAPL 200C TODAY @ 1.00"),
        _message("2", 5, "added @ 0.95"),
    )
    first = build_acquisition_plan(
        messages,
        allowed_author_ids=frozenset({AUTHOR}),
        pre_context=timedelta(seconds=2),
        post_context=timedelta(seconds=10),
    )
    second = build_acquisition_plan(
        messages,
        allowed_author_ids=frozenset({AUTHOR}),
        pre_context=timedelta(seconds=2),
        post_context=timedelta(seconds=10),
    )
    assert first.actionable_events == 2
    assert first.associated_events == 2
    assert len(first.windows) == 1
    assert first.windows[0].contract_key == CONTRACT
    assert first.plan_sha256 == second.plan_sha256
    assert first.total_window_seconds == 17.0


def test_fill_calibration_accepts_certified_exact_and_resting_interval(tmp_path):
    path = tmp_path / "fills.db"
    deadline = BASE + timedelta(seconds=5)
    exact = record_fill_prediction(
        path,
        intent_id="exact",
        event_id="event-exact",
        contract_key=CONTRACT,
        fill=_fill(3, 3, FillCertainty.AGGRESSIVE_DISPLAYED_COMPLETE),
        order_arrival_ts_utc=BASE,
        observation_deadline_ts_utc=deadline,
    )
    record_fill_observation(
        path,
        exact.prediction_id,
        actual_filled_quantity=3,
        observation_deadline_ts_utc=deadline,
        source="paper",
        average_fill_price=Decimal("1.00"),
    )
    uncertain = record_fill_prediction(
        path,
        intent_id="uncertain",
        event_id="event-uncertain",
        contract_key=CONTRACT,
        fill=_fill(0, 3, FillCertainty.RESTING_QUEUE_UNKNOWN),
        order_arrival_ts_utc=BASE,
        observation_deadline_ts_utc=deadline,
    )
    record_fill_observation(
        path,
        uncertain.prediction_id,
        actual_filled_quantity=2,
        observation_deadline_ts_utc=deadline,
        source="paper",
    )
    report = evaluate_fill_calibration(path)
    assert report.samples == 2
    assert report.lower_bound_violations == 0
    assert report.upper_bound_violations == 0
    assert report.interval_coverage_rate == 1.0
    assert report.aggressive_certified_exact_rate == 1.0
    assert report.resting_uncertain_coverage_rate == 1.0
    assert report.calibrated is True


def test_fill_calibration_detects_overconfident_lower_bound(tmp_path):
    path = tmp_path / "violation.db"
    deadline = BASE + timedelta(seconds=5)
    prediction = record_fill_prediction(
        path,
        intent_id="bad-lower",
        event_id="event-bad",
        contract_key=CONTRACT,
        fill=_fill(2, 2, FillCertainty.AGGRESSIVE_DISPLAYED_COMPLETE),
        order_arrival_ts_utc=BASE,
        observation_deadline_ts_utc=deadline,
    )
    record_fill_observation(
        path,
        prediction.prediction_id,
        actual_filled_quantity=1,
        observation_deadline_ts_utc=deadline,
        source="paper",
    )
    report = evaluate_fill_calibration(path)
    assert report.lower_bound_violations == 1
    assert report.calibrated is False


def test_live_shadow_can_freeze_prediction_before_later_observation(tmp_path):
    recorder = LiveShadowRecorder()
    intent = ShadowExecutionIntent(
        intent_id="shadow",
        contract_key=CONTRACT,
        side=ShadowIntentSide.BUY_LIMIT,
        quantity=2,
        order_arrival_ts_utc=BASE,
        limit_price=Decimal("1.05"),
        observation_window=timedelta(seconds=5),
    )
    result, prediction = recorder.evaluate_and_record(
        intent,
        event_id="event-shadow",
        calibration_db=tmp_path / "shadow.db",
    )
    assert result.fill.lower_bound_quantity == 0
    assert prediction.intent_id == "shadow"
    record_fill_observation(
        tmp_path / "shadow.db",
        prediction.prediction_id,
        actual_filled_quantity=0,
        observation_deadline_ts_utc=intent.deadline_ts_utc,
        source="paper",
    )
    assert evaluate_fill_calibration(tmp_path / "shadow.db").calibrated is True
