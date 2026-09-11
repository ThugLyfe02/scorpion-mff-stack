from scorpion.domain import EventKind
from scorpion.residual_hotspots import (
    ResidualHotspotPolicy,
    ResidualHotspotStatus,
    ResidualModelObservation,
    evaluate_residual_hotspots,
)


def _rows() -> tuple[ResidualModelObservation, ...]:
    rows: list[ResidualModelObservation] = []
    for index in range(240):
        session = "OPEN" if index < 60 else "LATE"
        regime = "WIDE" if index % 3 == 0 else "TIGHT"
        truth = EventKind.IGNORE
        hotspot = session == "OPEN"
        predictions = (
            (EventKind.ENTRY, EventKind.ENTRY, EventKind.IGNORE)
            if hotspot
            else (
                EventKind.ENTRY if index % 19 == 0 else EventKind.IGNORE,
                EventKind.IGNORE,
                EventKind.IGNORE,
            )
        )
        for model_id, predicted in zip(("a", "b", "c"), predictions, strict=True):
            rows.append(
                ResidualModelObservation(
                    event_id=f"e-{index}",
                    model_id=model_id,
                    truth=truth,
                    predicted=predicted,
                    slices={"session": session, "regime": regime},
                )
            )
    return tuple(rows)


def test_residual_hotspot_detects_shared_failure_slice_after_fdr():
    report = evaluate_residual_hotspots(
        _rows(),
        policy=ResidualHotspotPolicy(
            minimum_events=200,
            minimum_slice_events=30,
            minimum_complement_events=30,
            minimum_absolute_failure_lift=0.20,
            maximum_fdr_q=0.05,
            maximum_robust_hotspots=0,
        ),
    )
    assert report.status is ResidualHotspotStatus.FAILED
    keys = {item.slice_key for item in report.robust_hotspots}
    assert "session=OPEN" in keys
    hotspot = next(item for item in report.robust_hotspots if item.slice_key == "session=OPEN")
    assert hotspot.failure_rate == 1.0
    assert hotspot.q_value <= 0.05


def test_residual_hotspot_can_be_used_as_diagnostic_without_blocking():
    report = evaluate_residual_hotspots(
        _rows(),
        policy=ResidualHotspotPolicy(
            minimum_events=200,
            minimum_slice_events=30,
            minimum_complement_events=30,
            minimum_absolute_failure_lift=0.20,
            maximum_fdr_q=0.05,
            maximum_robust_hotspots=10,
        ),
    )
    assert report.status is ResidualHotspotStatus.QUALIFIED
    assert report.robust_hotspots


def test_high_cardinality_slice_dimension_is_excluded_from_search():
    rows = tuple(
        ResidualModelObservation(
            event_id=row.event_id,
            model_id=row.model_id,
            truth=row.truth,
            predicted=row.predicted,
            slices={**row.slices, "message": row.event_id},
        )
        for row in _rows()
    )
    report = evaluate_residual_hotspots(
        rows,
        policy=ResidualHotspotPolicy(
            minimum_events=200,
            minimum_slice_events=30,
            minimum_complement_events=30,
            maximum_values_per_dimension=20,
            maximum_robust_hotspots=10,
        ),
    )
    assert all(not item.slice_key.startswith("message=") for item in report.evidence)
