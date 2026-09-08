from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .calibration import CalibrationObservation, RuleCalibration, calibrate_rules, wilson_lower_bound


@dataclass(frozen=True, slots=True)
class ModelReliability:
    model_key: str
    samples: int
    correct: int
    empirical_accuracy: float
    wilson_lower_95: float


def load_rule_calibration(path: str | Path) -> dict[str, RuleCalibration]:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """
            SELECT d.parser_rule,d.parser_confidence,d.kind,a.expected_kind
            FROM adjudications a
            JOIN decision_audit d ON d.event_id=a.event_id
            ORDER BY a.adjudicated_ts_utc,d.event_id
            """
        ).fetchall()
    finally:
        db.close()
    observations = [
        CalibrationObservation(
            rule_id=row["parser_rule"],
            stated_confidence=float(row["parser_confidence"]),
            correct=row["kind"] == row["expected_kind"],
            actionable=row["kind"] in {"ENTRY", "ADD", "TRIM", "EXIT"},
        )
        for row in rows
    ]
    return calibrate_rules(observations)


def load_model_reliability(path: str | Path) -> dict[str, ModelReliability]:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """
            SELECT s.model_name,s.model_version,s.predicted_kind,a.expected_kind
            FROM shadow_predictions s
            JOIN adjudications a ON a.event_id=s.event_id
            ORDER BY s.model_name,s.model_version,a.adjudicated_ts_utc
            """
        ).fetchall()
    finally:
        db.close()
    grouped: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        key = f"{row['model_name']}@{row['model_version']}"
        grouped[key].append(row["predicted_kind"] == row["expected_kind"])
    result: dict[str, ModelReliability] = {}
    for key, values in grouped.items():
        samples = len(values)
        correct = sum(values)
        result[key] = ModelReliability(
            model_key=key,
            samples=samples,
            correct=correct,
            empirical_accuracy=correct / samples,
            wilson_lower_95=wilson_lower_bound(correct, samples),
        )
    return result


def reliability_weights(
    reliability: dict[str, ModelReliability],
    *,
    minimum_samples: int = 20,
    provisional_weight: float = 0.25,
) -> dict[str, float]:
    if minimum_samples <= 0:
        raise ValueError("minimum_samples must be positive")
    if not 0.0 <= provisional_weight <= 1.0:
        raise ValueError("provisional_weight must be between 0 and 1")
    return {
        key: (
            item.wilson_lower_95
            if item.samples >= minimum_samples
            else min(provisional_weight, item.empirical_accuracy)
        )
        for key, item in reliability.items()
    }
