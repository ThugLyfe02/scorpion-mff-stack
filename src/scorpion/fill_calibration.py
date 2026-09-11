from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .microstructure import FillCertainty, MicroFillEnvelope

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS fill_predictions (
        prediction_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL UNIQUE,
        event_id TEXT NOT NULL,
        contract_key TEXT NOT NULL,
        side TEXT NOT NULL,
        requested_quantity INTEGER NOT NULL,
        lower_bound_quantity INTEGER NOT NULL,
        upper_bound_quantity INTEGER NOT NULL,
        modeled_price TEXT,
        certainty TEXT NOT NULL,
        order_arrival_ts_utc TEXT NOT NULL,
        observation_deadline_ts_utc TEXT NOT NULL,
        created_ts_utc TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fill_observations (
        observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_id TEXT NOT NULL UNIQUE,
        actual_filled_quantity INTEGER NOT NULL,
        average_fill_price TEXT,
        observation_deadline_ts_utc TEXT NOT NULL,
        source TEXT NOT NULL,
        observed_ts_utc TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(prediction_id) REFERENCES fill_predictions(prediction_id)
    )
    """,
)


@dataclass(frozen=True, slots=True)
class FillPredictionRecord:
    prediction_id: str
    intent_id: str
    event_id: str
    contract_key: str
    side: str
    requested_quantity: int
    lower_bound_quantity: int
    upper_bound_quantity: int
    modeled_price: Decimal | None
    certainty: FillCertainty
    order_arrival_ts_utc: datetime
    observation_deadline_ts_utc: datetime


@dataclass(frozen=True, slots=True)
class FillCalibrationReport:
    samples: int
    lower_bound_violations: int
    upper_bound_violations: int
    interval_coverage_rate: float
    exact_quantity_rate: float
    mean_interval_width: float
    mean_absolute_quantity_error_to_midpoint: float
    aggressive_certified_samples: int
    aggressive_certified_exact_rate: float
    resting_uncertain_samples: int
    resting_uncertain_coverage_rate: float
    calibrated: bool


def _ensure_schema(db: sqlite3.Connection) -> None:
    for statement in _SCHEMA:
        db.execute(statement)


def _prediction_id(intent_id: str, event_id: str, fill: MicroFillEnvelope) -> str:
    material = "|".join(
        (
            "fill-calibration-v1",
            intent_id,
            event_id,
            fill.side,
            str(fill.requested_quantity),
            str(fill.lower_bound_quantity),
            str(fill.upper_bound_quantity),
            fill.certainty.value,
            str(fill.order_arrival_ns),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def record_fill_prediction(
    path: str | Path,
    *,
    intent_id: str,
    event_id: str,
    contract_key: str,
    fill: MicroFillEnvelope,
    order_arrival_ts_utc: datetime,
    observation_deadline_ts_utc: datetime,
) -> FillPredictionRecord:
    if not intent_id.strip() or not event_id.strip() or not contract_key.strip():
        raise ValueError("intent_id, event_id and contract_key are required")
    if observation_deadline_ts_utc < order_arrival_ts_utc:
        raise ValueError("observation deadline cannot precede order arrival")
    identifier = _prediction_id(intent_id, event_id, fill)
    created = datetime.now(UTC).isoformat()
    with sqlite3.connect(str(path)) as db:
        _ensure_schema(db)
        db.execute(
            """
            INSERT OR IGNORE INTO fill_predictions
            (prediction_id,intent_id,event_id,contract_key,side,requested_quantity,
             lower_bound_quantity,upper_bound_quantity,modeled_price,certainty,
             order_arrival_ts_utc,observation_deadline_ts_utc,created_ts_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                identifier,
                intent_id,
                event_id,
                contract_key,
                fill.side,
                fill.requested_quantity,
                fill.lower_bound_quantity,
                fill.upper_bound_quantity,
                str(fill.modeled_price) if fill.modeled_price is not None else None,
                fill.certainty.value,
                order_arrival_ts_utc.astimezone(UTC).isoformat(),
                observation_deadline_ts_utc.astimezone(UTC).isoformat(),
                created,
            ),
        )
    return FillPredictionRecord(
        identifier,
        intent_id,
        event_id,
        contract_key,
        fill.side,
        fill.requested_quantity,
        fill.lower_bound_quantity,
        fill.upper_bound_quantity,
        fill.modeled_price,
        fill.certainty,
        order_arrival_ts_utc.astimezone(UTC),
        observation_deadline_ts_utc.astimezone(UTC),
    )


def record_fill_observation(
    path: str | Path,
    prediction_id: str,
    *,
    actual_filled_quantity: int,
    observation_deadline_ts_utc: datetime,
    source: str,
    average_fill_price: Decimal | None = None,
    note: str = "",
) -> None:
    if actual_filled_quantity < 0:
        raise ValueError("actual_filled_quantity cannot be negative")
    if average_fill_price is not None and average_fill_price <= 0:
        raise ValueError("average_fill_price must be positive when present")
    if not source.strip():
        raise ValueError("observation source is required")
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        _ensure_schema(db)
        prediction = db.execute(
            """
            SELECT requested_quantity,observation_deadline_ts_utc
            FROM fill_predictions WHERE prediction_id=?
            """,
            (prediction_id,),
        ).fetchone()
        if prediction is None:
            raise KeyError(prediction_id)
        if actual_filled_quantity > int(prediction["requested_quantity"]):
            raise ValueError("actual fill cannot exceed requested quantity")
        expected_deadline = datetime.fromisoformat(
            str(prediction["observation_deadline_ts_utc"])
        ).astimezone(UTC)
        supplied_deadline = observation_deadline_ts_utc.astimezone(UTC)
        if supplied_deadline != expected_deadline:
            raise ValueError("observation horizon must exactly match prediction horizon")
        db.execute(
            """
            INSERT OR REPLACE INTO fill_observations
            (prediction_id,actual_filled_quantity,average_fill_price,
             observation_deadline_ts_utc,source,observed_ts_utc,note)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                prediction_id,
                actual_filled_quantity,
                str(average_fill_price) if average_fill_price is not None else None,
                supplied_deadline.isoformat(),
                source.strip(),
                datetime.now(UTC).isoformat(),
                note[:1000],
            ),
        )


def evaluate_fill_calibration(path: str | Path) -> FillCalibrationReport:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        _ensure_schema(db)
        rows = db.execute(
            """
            SELECT p.*,o.actual_filled_quantity
            FROM fill_predictions p
            JOIN fill_observations o ON o.prediction_id=p.prediction_id
            ORDER BY p.created_ts_utc,p.prediction_id
            """
        ).fetchall()
    samples = len(rows)
    if samples == 0:
        return FillCalibrationReport(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0, 0.0, False)

    lower_violations = 0
    upper_violations = 0
    exact = 0
    widths: list[int] = []
    midpoint_errors: list[float] = []
    aggressive_total = 0
    aggressive_exact = 0
    resting_total = 0
    resting_covered = 0
    aggressive = {
        FillCertainty.AGGRESSIVE_DISPLAYED_COMPLETE.value,
        FillCertainty.AGGRESSIVE_DISPLAYED_PARTIAL.value,
    }

    for row in rows:
        lower = int(row["lower_bound_quantity"])
        upper = int(row["upper_bound_quantity"])
        actual = int(row["actual_filled_quantity"])
        if actual < lower:
            lower_violations += 1
        if actual > upper:
            upper_violations += 1
        if lower == upper == actual:
            exact += 1
        widths.append(upper - lower)
        midpoint_errors.append(abs(actual - ((lower + upper) / 2)))
        certainty = str(row["certainty"])
        if certainty in aggressive:
            aggressive_total += 1
            if lower == upper == actual:
                aggressive_exact += 1
        if certainty == FillCertainty.RESTING_QUEUE_UNKNOWN.value:
            resting_total += 1
            if lower <= actual <= upper:
                resting_covered += 1

    covered = samples - lower_violations - upper_violations
    return FillCalibrationReport(
        samples=samples,
        lower_bound_violations=lower_violations,
        upper_bound_violations=upper_violations,
        interval_coverage_rate=max(0.0, covered / samples),
        exact_quantity_rate=exact / samples,
        mean_interval_width=sum(widths) / samples,
        mean_absolute_quantity_error_to_midpoint=sum(midpoint_errors) / samples,
        aggressive_certified_samples=aggressive_total,
        aggressive_certified_exact_rate=(
            aggressive_exact / aggressive_total if aggressive_total else 0.0
        ),
        resting_uncertain_samples=resting_total,
        resting_uncertain_coverage_rate=(
            resting_covered / resting_total if resting_total else 0.0
        ),
        calibrated=lower_violations == 0 and upper_violations == 0,
    )
