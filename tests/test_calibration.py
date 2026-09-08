from scorpion.accuracy import DecisionEvidence
from scorpion.calibration import (
    CalibrationObservation,
    CalibrationStatus,
    assess_rule_confidence,
    calibrate_rules,
    wilson_lower_bound,
)


def test_wilson_lower_bound_is_conservative():
    lower = wilson_lower_bound(95, 100)
    assert 0.0 < lower < 0.95


def test_rule_calibration_detects_overconfidence():
    observations = [
        CalibrationObservation("entry.complete", 0.998, correct=index < 90)
        for index in range(100)
    ]
    calibration = calibrate_rules(observations)["entry.complete"]
    assert calibration.empirical_accuracy == 0.90
    assert calibration.calibration_gap > 0.09
    assert calibration.brier_score > 0.0


def test_calibrated_decision_requires_evidence_before_trust():
    evidence = DecisionEvidence("entry.complete", 0.998)
    uncalibrated = assess_rule_confidence(evidence, None)
    assert uncalibrated.status is CalibrationStatus.UNCALIBRATED

    observations = [CalibrationObservation("entry.complete", 0.998, True) for _ in range(200)]
    calibration = calibrate_rules(observations)["entry.complete"]
    trusted = assess_rule_confidence(evidence, calibration)
    assert trusted.status is CalibrationStatus.TRUSTED
    assert trusted.effective_confidence <= evidence.confidence
