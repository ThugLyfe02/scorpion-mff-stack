from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .information_value import LearningAction, LearningAllocationDecision

_GENESIS = "0" * 64
_RESEARCH_ACTIONS = frozenset(
    {
        LearningAction.LIGHT_SHADOW.value,
        LearningAction.DEEP_SHADOW.value,
        LearningAction.HUMAN_REVIEW.value,
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS realized_learning_outcomes (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    outcome_id TEXT NOT NULL UNIQUE,
    allocation_hash TEXT NOT NULL,
    event_id TEXT NOT NULL,
    action TEXT NOT NULL,
    segment TEXT NOT NULL,
    cost_units INTEGER NOT NULL,
    expected_utility REAL NOT NULL,
    accepted_truth_revision_id TEXT NOT NULL,
    truth_snapshot_hash TEXT NOT NULL,
    label_usable INTEGER NOT NULL,
    curriculum_fingerprint TEXT NOT NULL,
    evaluation_id TEXT NOT NULL,
    attribution_method TEXT NOT NULL,
    oof_log_loss_gain REAL NOT NULL,
    oof_brier_gain REAL NOT NULL,
    false_action_rate_delta REAL NOT NULL,
    wrong_action_rate_delta REAL NOT NULL,
    realized_ts_utc TEXT NOT NULL,
    previous_record_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE,
    UNIQUE(allocation_hash,event_id,action,evaluation_id,attribution_method)
);
CREATE TABLE IF NOT EXISTS realized_learning_integrity_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    event_count INTEGER NOT NULL,
    head_record_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS realized_learning_outcomes_no_update
BEFORE UPDATE ON realized_learning_outcomes
BEGIN SELECT RAISE(ABORT,'realized_learning_outcomes_append_only'); END;
CREATE TRIGGER IF NOT EXISTS realized_learning_outcomes_no_delete
BEFORE DELETE ON realized_learning_outcomes
BEGIN SELECT RAISE(ABORT,'realized_learning_outcomes_append_only'); END;
CREATE INDEX IF NOT EXISTS idx_realized_learning_action_sequence
ON realized_learning_outcomes(action,sequence);
CREATE INDEX IF NOT EXISTS idx_realized_learning_segment_sequence
ON realized_learning_outcomes(action,segment,sequence);
"""


class YieldAttributionMethod(StrEnum):
    LABEL_ONLY = "LABEL_ONLY"
    PAIRED_OOF_ABLATION = "PAIRED_OOF_ABLATION"
    CURRICULUM_BUCKET_ABLATION = "CURRICULUM_BUCKET_ABLATION"


class YieldCalibrationStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    INSUFFICIENT = "INSUFFICIENT"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class RealizedLearningOutcome:
    allocation_hash: str
    event_id: str
    action: LearningAction
    segment: str
    cost_units: int
    expected_utility: float
    accepted_truth_revision_id: str
    truth_snapshot_hash: str
    label_usable: bool
    curriculum_fingerprint: str
    evaluation_id: str
    attribution_method: YieldAttributionMethod
    oof_log_loss_gain: float
    oof_brier_gain: float
    false_action_rate_delta: float
    wrong_action_rate_delta: float
    realized_ts_utc: datetime

    def __post_init__(self) -> None:
        for name in ("allocation_hash", "event_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if self.action.value not in _RESEARCH_ACTIONS:
            raise ValueError("realized yield only supports research learning actions")
        if self.cost_units <= 0:
            raise ValueError("cost_units must be positive")
        if not math.isfinite(self.expected_utility) or self.expected_utility < 0:
            raise ValueError("expected_utility must be finite and non-negative")
        for name in (
            "oof_log_loss_gain",
            "oof_brier_gain",
            "false_action_rate_delta",
            "wrong_action_rate_delta",
        ):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.realized_ts_utc.tzinfo is None or self.realized_ts_utc.utcoffset() is None:
            raise ValueError("realized_ts_utc must be timezone-aware")
        if self.label_usable and (
            not self.accepted_truth_revision_id.strip() or not self.truth_snapshot_hash.strip()
        ):
            raise ValueError("usable labels must bind accepted truth revision and snapshot")
        if (
            self.attribution_method is not YieldAttributionMethod.LABEL_ONLY
            and (not self.evaluation_id.strip() or not self.curriculum_fingerprint.strip())
        ):
            raise ValueError("OOF attribution requires evaluation and curriculum identities")


@dataclass(frozen=True, slots=True)
class LearningYieldLedgerVerification:
    outcomes: int
    head_record_hash: str
    chain_hash: str
    valid: bool
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RealizedYieldPolicy:
    minimum_outcomes: int = 20
    minimum_action_outcomes: int = 5
    minimum_segment_outcomes: int = 5
    action_prior_strength: float = 20.0
    segment_prior_strength: float = 30.0
    confidence_z: float = 1.2815515655446004
    variance_floor: float = 0.01
    expected_utility_floor: float = 0.10
    maximum_observation_ratio: float = 2.0
    minimum_multiplier: float = 0.50
    maximum_multiplier: float = 1.50
    label_weight: float = 0.55
    log_loss_weight: float = 0.30
    brier_weight: float = 0.15
    log_loss_gain_scale: float = 0.02
    brier_gain_scale: float = 0.02
    false_action_penalty_scale: float = 0.02
    wrong_action_penalty_scale: float = 0.02

    def __post_init__(self) -> None:
        if min(
            self.minimum_outcomes,
            self.minimum_action_outcomes,
            self.minimum_segment_outcomes,
        ) <= 0:
            raise ValueError("yield sample thresholds must be positive")
        for name in (
            "action_prior_strength",
            "segment_prior_strength",
            "confidence_z",
            "variance_floor",
            "expected_utility_floor",
            "maximum_observation_ratio",
            "log_loss_gain_scale",
            "brier_gain_scale",
            "false_action_penalty_scale",
            "wrong_action_penalty_scale",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 0 < self.minimum_multiplier <= self.maximum_multiplier:
            raise ValueError("yield multiplier bounds are invalid")
        if self.maximum_multiplier > self.maximum_observation_ratio:
            raise ValueError("maximum_multiplier cannot exceed maximum_observation_ratio")
        weights = (self.label_weight, self.log_loss_weight, self.brier_weight)
        if any(not math.isfinite(value) or value < 0 for value in weights):
            raise ValueError("yield component weights must be finite and non-negative")
        if sum(weights) <= 0:
            raise ValueError("at least one yield component weight must be positive")


@dataclass(frozen=True, slots=True)
class YieldEstimate:
    action: str
    segment: str
    samples: int
    sample_mean_ratio: float
    prior_mean_ratio: float
    posterior_mean_ratio: float
    posterior_std_error: float
    conservative_ratio: float
    multiplier: float


@dataclass(frozen=True, slots=True)
class RealizedLearningYieldCalibration:
    status: YieldCalibrationStatus
    outcomes: int
    global_mean_ratio: float
    action_estimates: tuple[YieldEstimate, ...]
    segment_estimates: tuple[YieldEstimate, ...]
    ledger_head_record_hash: str
    ledger_chain_hash: str
    calibration_hash: str
    failures: tuple[str, ...]

    def multiplier_for(self, action: str, segment: str = "") -> float:
        if self.status is not YieldCalibrationStatus.QUALIFIED:
            return 1.0
        for item in self.segment_estimates:
            if item.action == action and item.segment == segment:
                return item.multiplier
        for item in self.action_estimates:
            if item.action == action:
                return item.multiplier
        return 1.0


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _outcome_payload(
    outcome: RealizedLearningOutcome,
    previous_record_hash: str,
) -> dict[str, object]:
    return {
        "version": "realized-learning-outcome-v1",
        "allocation_hash": outcome.allocation_hash,
        "event_id": outcome.event_id,
        "action": outcome.action.value,
        "segment": outcome.segment,
        "cost_units": outcome.cost_units,
        "expected_utility": round(outcome.expected_utility, 12),
        "accepted_truth_revision_id": outcome.accepted_truth_revision_id,
        "truth_snapshot_hash": outcome.truth_snapshot_hash,
        "label_usable": outcome.label_usable,
        "curriculum_fingerprint": outcome.curriculum_fingerprint,
        "evaluation_id": outcome.evaluation_id,
        "attribution_method": outcome.attribution_method.value,
        "oof_log_loss_gain": round(outcome.oof_log_loss_gain, 12),
        "oof_brier_gain": round(outcome.oof_brier_gain, 12),
        "false_action_rate_delta": round(outcome.false_action_rate_delta, 12),
        "wrong_action_rate_delta": round(outcome.wrong_action_rate_delta, 12),
        "realized_ts_utc": outcome.realized_ts_utc.astimezone(UTC).isoformat(),
        "previous_record_hash": previous_record_hash,
    }


def record_realized_learning_outcome(
    path: str | Path,
    outcome: RealizedLearningOutcome,
) -> str:
    db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT record_hash FROM realized_learning_outcomes ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = str(previous["record_hash"]) if previous is not None else _GENESIS
        payload = _outcome_payload(outcome, previous_hash)
        outcome_id = _hash(
            {
                "version": "realized-learning-outcome-id-v1",
                "allocation_hash": outcome.allocation_hash,
                "event_id": outcome.event_id,
                "action": outcome.action.value,
                "evaluation_id": outcome.evaluation_id,
                "attribution_method": outcome.attribution_method.value,
                "truth_snapshot_hash": outcome.truth_snapshot_hash,
            }
        )
        record_hash = _hash(payload)
        db.execute(
            """
            INSERT INTO realized_learning_outcomes
            (outcome_id,allocation_hash,event_id,action,segment,cost_units,expected_utility,
             accepted_truth_revision_id,truth_snapshot_hash,label_usable,curriculum_fingerprint,
             evaluation_id,attribution_method,oof_log_loss_gain,oof_brier_gain,
             false_action_rate_delta,wrong_action_rate_delta,realized_ts_utc,
             previous_record_hash,record_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                outcome_id,
                outcome.allocation_hash,
                outcome.event_id,
                outcome.action.value,
                outcome.segment,
                outcome.cost_units,
                outcome.expected_utility,
                outcome.accepted_truth_revision_id,
                outcome.truth_snapshot_hash,
                int(outcome.label_usable),
                outcome.curriculum_fingerprint,
                outcome.evaluation_id,
                outcome.attribution_method.value,
                outcome.oof_log_loss_gain,
                outcome.oof_brier_gain,
                outcome.false_action_rate_delta,
                outcome.wrong_action_rate_delta,
                outcome.realized_ts_utc.astimezone(UTC).isoformat(),
                previous_hash,
                record_hash,
            ),
        )
        checkpoint = db.execute(
            "SELECT event_count,chain_hash FROM realized_learning_integrity_state "
            "WHERE singleton_id=1"
        ).fetchone()
        count = int(checkpoint["event_count"]) if checkpoint is not None else 0
        chain = str(checkpoint["chain_hash"]) if checkpoint is not None else _GENESIS
        chain = hashlib.sha256(f"{chain}|{record_hash}".encode()).hexdigest()
        db.execute(
            """
            INSERT INTO realized_learning_integrity_state
            (singleton_id,event_count,head_record_hash,chain_hash) VALUES (1,?,?,?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                event_count=excluded.event_count,
                head_record_hash=excluded.head_record_hash,
                chain_hash=excluded.chain_hash
            """,
            (count + 1, record_hash, chain),
        )
        db.execute("COMMIT")
        return outcome_id
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def record_learning_decision_outcome(
    path: str | Path,
    *,
    allocation_hash: str,
    decision: LearningAllocationDecision,
    segment: str,
    accepted_truth_revision_id: str,
    truth_snapshot_hash: str,
    label_usable: bool,
    curriculum_fingerprint: str,
    evaluation_id: str,
    attribution_method: YieldAttributionMethod,
    oof_log_loss_gain: float,
    oof_brier_gain: float,
    false_action_rate_delta: float,
    wrong_action_rate_delta: float,
    realized_ts_utc: datetime | None = None,
) -> str:
    return record_realized_learning_outcome(
        path,
        RealizedLearningOutcome(
            allocation_hash=allocation_hash,
            event_id=decision.event_id,
            action=decision.action,
            segment=segment,
            cost_units=decision.cost_units,
            expected_utility=decision.base_utility or decision.utility,
            accepted_truth_revision_id=accepted_truth_revision_id,
            truth_snapshot_hash=truth_snapshot_hash,
            label_usable=label_usable,
            curriculum_fingerprint=curriculum_fingerprint,
            evaluation_id=evaluation_id,
            attribution_method=attribution_method,
            oof_log_loss_gain=oof_log_loss_gain,
            oof_brier_gain=oof_brier_gain,
            false_action_rate_delta=false_action_rate_delta,
            wrong_action_rate_delta=wrong_action_rate_delta,
            realized_ts_utc=(realized_ts_utc or datetime.now(UTC)).astimezone(UTC),
        ),
    )


def _decode_outcome(row: sqlite3.Row) -> RealizedLearningOutcome:
    return RealizedLearningOutcome(
        allocation_hash=str(row["allocation_hash"]),
        event_id=str(row["event_id"]),
        action=LearningAction(str(row["action"])),
        segment=str(row["segment"]),
        cost_units=int(row["cost_units"]),
        expected_utility=float(row["expected_utility"]),
        accepted_truth_revision_id=str(row["accepted_truth_revision_id"]),
        truth_snapshot_hash=str(row["truth_snapshot_hash"]),
        label_usable=bool(row["label_usable"]),
        curriculum_fingerprint=str(row["curriculum_fingerprint"]),
        evaluation_id=str(row["evaluation_id"]),
        attribution_method=YieldAttributionMethod(str(row["attribution_method"])),
        oof_log_loss_gain=float(row["oof_log_loss_gain"]),
        oof_brier_gain=float(row["oof_brier_gain"]),
        false_action_rate_delta=float(row["false_action_rate_delta"]),
        wrong_action_rate_delta=float(row["wrong_action_rate_delta"]),
        realized_ts_utc=datetime.fromisoformat(str(row["realized_ts_utc"])).astimezone(UTC),
    )


def verify_realized_learning_yield_ledger(
    path: str | Path,
) -> LearningYieldLedgerVerification:
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        rows = db.execute(
            "SELECT * FROM realized_learning_outcomes ORDER BY sequence"
        ).fetchall()
        checkpoint = db.execute(
            "SELECT * FROM realized_learning_integrity_state WHERE singleton_id=1"
        ).fetchone()
    failures: list[str] = []
    previous = _GENESIS
    chain = _GENESIS
    for expected_sequence, row in enumerate(rows, start=1):
        if int(row["sequence"]) != expected_sequence:
            failures.append("realized_learning_sequence_gap")
        try:
            outcome = _decode_outcome(row)
        except (ValueError, TypeError):
            failures.append("realized_learning_outcome_decode_failure")
            continue
        if str(row["previous_record_hash"]) != previous:
            failures.append("realized_learning_parent_hash_mismatch")
        expected_hash = _hash(_outcome_payload(outcome, str(row["previous_record_hash"])))
        if str(row["record_hash"]) != expected_hash:
            failures.append("realized_learning_record_hash_mismatch")
        previous = str(row["record_hash"])
        chain = hashlib.sha256(f"{chain}|{row['event_hash']}".encode()).hexdigest()
    checkpoint_valid = checkpoint is not None and (
        int(checkpoint["event_count"]) == len(rows)
        and str(checkpoint["head_record_hash"]) == previous
        and str(checkpoint["chain_hash"]) == chain
    )
    if not checkpoint_valid:
        failures.append("realized_learning_integrity_checkpoint_mismatch")
    unique_failures = tuple(dict.fromkeys(failures))
    return LearningYieldLedgerVerification(
        outcomes=len(rows),
        head_record_hash=previous,
        chain_hash=chain,
        valid=not unique_failures,
        failures=unique_failures,
    )


def _component_score(outcome: RealizedLearningOutcome, policy: RealizedYieldPolicy) -> float:
    weight_total = policy.label_weight + policy.log_loss_weight + policy.brier_weight
    label = 1.0 if outcome.label_usable else 0.0
    log_component = max(
        -1.0,
        min(1.0, outcome.oof_log_loss_gain / policy.log_loss_gain_scale),
    )
    brier_component = max(
        -1.0,
        min(1.0, outcome.oof_brier_gain / policy.brier_gain_scale),
    )
    positive = (
        policy.label_weight * label
        + policy.log_loss_weight * max(0.0, log_component)
        + policy.brier_weight * max(0.0, brier_component)
    ) / weight_total
    regression = (
        max(0.0, -log_component) * policy.log_loss_weight
        + max(0.0, -brier_component) * policy.brier_weight
    ) / weight_total
    safety_penalty = min(
        1.0,
        max(0.0, outcome.false_action_rate_delta) / policy.false_action_penalty_scale
        + max(0.0, outcome.wrong_action_rate_delta) / policy.wrong_action_penalty_scale,
    )
    return min(1.0, max(0.0, positive - regression - safety_penalty))


def _yield_ratio(outcome: RealizedLearningOutcome, policy: RealizedYieldPolicy) -> float:
    denominator = max(outcome.expected_utility, policy.expected_utility_floor)
    return min(
        policy.maximum_observation_ratio,
        _component_score(outcome, policy) / denominator,
    )


def _estimate(
    *,
    action: str,
    segment: str,
    ratios: list[float],
    prior_mean: float,
    prior_strength: float,
    policy: RealizedYieldPolicy,
) -> YieldEstimate:
    samples = len(ratios)
    sample_mean = statistics.fmean(ratios)
    posterior_mean = (
        samples * sample_mean + prior_strength * prior_mean
    ) / (samples + prior_strength)
    variance = statistics.variance(ratios) if samples > 1 else policy.variance_floor
    variance = max(variance, policy.variance_floor)
    std_error = math.sqrt(variance / (samples + prior_strength))
    conservative = max(0.0, posterior_mean - policy.confidence_z * std_error)
    multiplier = min(
        policy.maximum_multiplier,
        max(policy.minimum_multiplier, conservative),
    )
    return YieldEstimate(
        action=action,
        segment=segment,
        samples=samples,
        sample_mean_ratio=sample_mean,
        prior_mean_ratio=prior_mean,
        posterior_mean_ratio=posterior_mean,
        posterior_std_error=std_error,
        conservative_ratio=conservative,
        multiplier=multiplier,
    )


def calibrate_realized_learning_yield(
    path: str | Path,
    *,
    policy: RealizedYieldPolicy | None = None,
) -> RealizedLearningYieldCalibration:
    policy = policy or RealizedYieldPolicy()
    verification = verify_realized_learning_yield_ledger(path)
    failures: list[str] = []
    if not verification.valid:
        failures.extend(verification.failures)
        status = YieldCalibrationStatus.BLOCKED
        return _calibration_result(
            status=status,
            outcomes=verification.outcomes,
            global_mean_ratio=1.0,
            action_estimates=(),
            segment_estimates=(),
            verification=verification,
            policy=policy,
            failures=tuple(failures),
        )
    with sqlite3.connect(str(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        outcomes = tuple(
            _decode_outcome(row)
            for row in db.execute(
                "SELECT * FROM realized_learning_outcomes ORDER BY sequence"
            )
        )
    if len(outcomes) < policy.minimum_outcomes:
        failures.append(
            f"insufficient_realized_learning_outcomes:{len(outcomes)}<{policy.minimum_outcomes}"
        )
        return _calibration_result(
            status=YieldCalibrationStatus.INSUFFICIENT,
            outcomes=len(outcomes),
            global_mean_ratio=1.0,
            action_estimates=(),
            segment_estimates=(),
            verification=verification,
            policy=policy,
            failures=tuple(failures),
        )
    ratios = [_yield_ratio(item, policy) for item in outcomes]
    global_mean = statistics.fmean(ratios)
    action_groups: dict[str, list[float]] = {}
    segment_groups: dict[tuple[str, str], list[float]] = {}
    for outcome, ratio in zip(outcomes, ratios, strict=True):
        action_groups.setdefault(outcome.action.value, []).append(ratio)
        if outcome.segment:
            segment_groups.setdefault((outcome.action.value, outcome.segment), []).append(ratio)

    action_estimates: list[YieldEstimate] = []
    action_prior: dict[str, float] = {}
    for action in sorted(action_groups):
        members = action_groups[action]
        if len(members) < policy.minimum_action_outcomes:
            continue
        estimate = _estimate(
            action=action,
            segment="",
            ratios=members,
            prior_mean=global_mean,
            prior_strength=policy.action_prior_strength,
            policy=policy,
        )
        action_estimates.append(estimate)
        action_prior[action] = estimate.posterior_mean_ratio

    if not action_estimates:
        failures.append("insufficient_action_specific_yield_evidence")
        return _calibration_result(
            status=YieldCalibrationStatus.INSUFFICIENT,
            outcomes=len(outcomes),
            global_mean_ratio=global_mean,
            action_estimates=(),
            segment_estimates=(),
            verification=verification,
            policy=policy,
            failures=tuple(failures),
        )

    segment_estimates: list[YieldEstimate] = []
    for key in sorted(segment_groups):
        action, segment = key
        members = segment_groups[key]
        if len(members) < policy.minimum_segment_outcomes:
            continue
        estimate = _estimate(
            action=action,
            segment=segment,
            ratios=members,
            prior_mean=action_prior.get(action, global_mean),
            prior_strength=policy.segment_prior_strength,
            policy=policy,
        )
        segment_estimates.append(estimate)
    return _calibration_result(
        status=YieldCalibrationStatus.QUALIFIED,
        outcomes=len(outcomes),
        global_mean_ratio=global_mean,
        action_estimates=tuple(action_estimates),
        segment_estimates=tuple(segment_estimates),
        verification=verification,
        policy=policy,
        failures=(),
    )


def _calibration_result(
    *,
    status: YieldCalibrationStatus,
    outcomes: int,
    global_mean_ratio: float,
    action_estimates: tuple[YieldEstimate, ...],
    segment_estimates: tuple[YieldEstimate, ...],
    verification: LearningYieldLedgerVerification,
    policy: RealizedYieldPolicy,
    failures: tuple[str, ...],
) -> RealizedLearningYieldCalibration:
    material = {
        "version": "realized-learning-yield-calibration-v1",
        "status": status.value,
        "outcomes": outcomes,
        "global_mean_ratio": round(global_mean_ratio, 12),
        "action_estimates": [asdict(item) for item in action_estimates],
        "segment_estimates": [asdict(item) for item in segment_estimates],
        "ledger_head_record_hash": verification.head_record_hash,
        "ledger_chain_hash": verification.chain_hash,
        "policy": asdict(policy),
        "failures": failures,
    }
    return RealizedLearningYieldCalibration(
        status=status,
        outcomes=outcomes,
        global_mean_ratio=global_mean_ratio,
        action_estimates=action_estimates,
        segment_estimates=segment_estimates,
        ledger_head_record_hash=verification.head_record_hash,
        ledger_chain_hash=verification.chain_hash,
        calibration_hash=_hash(material),
        failures=failures,
    )
