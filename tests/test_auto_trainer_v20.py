from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from scorpion.auto_trainer import (
    AutoTrainerPolicy,
    CallableValidationMetric,
    MetricDirection,
    TrainingRunStatus,
    load_training_run,
    run_auto_training,
)
from scorpion.drift_retraining import ChallengerRetrainingPlan, RetrainingPlanStatus
from scorpion.fail_safe_control import (
    NoTradeSafetyLatch,
    SafetyMode,
    quarantine_and_trip_no_trade,
)
from scorpion.governance import PromotionDecision, PromotionStatus
from scorpion.release_guard import ReleaseRegistry, ReleaseState
from scorpion.shadow_lifecycle import ShadowModelRegistry, ShadowReleaseState
from scorpion.trainable_model import (
    CallableTrainableModel,
    DeterminismLevel,
    FeatureDType,
    FeatureField,
    FeatureSchema,
    FeatureVector,
    ModelArtifact,
    ModelPrediction,
    TaskKind,
    TrainingExample,
    dataset_fingerprint,
)

NOW = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _MeanFitted:
    model_id: str
    mean: float

    def predict_one(self, features: FeatureVector) -> ModelPrediction:
        del features
        return ModelPrediction(self.mean)

    def export_artifact(self) -> ModelArtifact:
        return ModelArtifact(
            model_id=self.model_id,
            model_version="mean-v1",
            loader_key="tests.mean",
            media_type="application/x-scorpion-test-model",
            payload=f"{self.mean:.12f}".encode(),
        )


def _schema() -> FeatureSchema:
    return FeatureSchema("features-v1", (FeatureField("x", FeatureDType.FLOAT),))


def _examples(schema: FeatureSchema, *, count: int = 140) -> tuple[TrainingExample, ...]:
    return tuple(
        TrainingExample(
            sample_id=f"sample-{index:03d}",
            observed_ts_utc=NOW + timedelta(minutes=index),
            features=schema.normalize({"x": float(index)}),
            target=1.0,
        )
        for index in range(count)
    )


def _plan(dataset_hash: str) -> ChallengerRetrainingPlan:
    return ChallengerRetrainingPlan(
        plan_id="plan-v1",
        parent_release_id="parent-v1",
        dataset_fingerprint=dataset_hash,
        status=RetrainingPlanStatus.READY_RESEARCH_CHALLENGER,
        confirmations=("page_hinkley", "bayesian_changepoint"),
        samples_since_drift=120,
        post_drift_training_samples=80,
        validation_samples=30,
        pre_drift_anchor_samples=20,
        purge_embargo_seconds=0.0,
        severe_drift=False,
        reasons=("corroborated_drift_retraining_ready",),
        created_ts_utc=NOW,
    )


def _mae(rows) -> float:
    return sum(
        abs(float(item.target) - float(item.prediction.value)) for item in rows
    ) / len(rows)


def _ready_promotion() -> PromotionDecision:
    return PromotionDecision(
        PromotionStatus.READY_FOR_OPERATOR_REVIEW,
        (),
        "all independent evidence gates passed",
    )


def _mean_model(schema: FeatureSchema, model_id: str = "mean-model") -> CallableTrainableModel:
    def fit(rows, seed):
        del seed
        mean = sum(float(item.target) for item in rows) / len(rows)
        return _MeanFitted(model_id, mean)

    return CallableTrainableModel(
        model_id=model_id,
        trainer_version="trainer-v1",
        task=TaskKind.REGRESSION,
        feature_schema=schema,
        fit_callable=fit,
        determinism=DeterminismLevel.EXACT,
    )


def test_auto_trainer_never_exposes_validation_rows_to_arbitrary_fit(tmp_path):
    schema = _schema()
    examples = _examples(schema)
    seen_fit_ids: list[tuple[str, ...]] = []

    def fit(rows, seed):
        assert seed == 17
        seen_fit_ids.append(tuple(item.sample_id for item in rows))
        mean = sum(float(item.target) for item in rows) / len(rows)
        return _MeanFitted("mean-model", mean)

    model = CallableTrainableModel(
        model_id="mean-model",
        trainer_version="trainer-v1",
        task=TaskKind.REGRESSION,
        feature_schema=schema,
        fit_callable=fit,
        determinism=DeterminismLevel.EXACT,
    )
    outcome = run_auto_training(
        tmp_path / "adaptive.db",
        model=model,
        examples=examples,
        plan=_plan(dataset_fingerprint(examples, schema)),
        metrics=(
            CallableValidationMetric("mae", MetricDirection.MINIMIZE, 0.01, _mae),
        ),
        code_revision="abc123",
        policy_fingerprint="policy-sha",
        policy=AutoTrainerPolicy(
            minimum_training_samples=80,
            minimum_validation_samples=30,
        ),
        now=NOW,
    )
    assert outcome.report.status is TrainingRunStatus.SHADOW_READY
    assert outcome.report.exact_reproducibility_verified is True
    assert outcome.artifact is not None
    validation_ids = set(outcome.split.validation_ids)
    assert validation_ids
    assert len(seen_fit_ids) == 2
    assert all(validation_ids.isdisjoint(fit_ids) for fit_ids in seen_fit_ids)
    persisted = load_training_run(tmp_path / "adaptive.db", outcome.report.run_id)
    assert persisted["artifact_sha256"] == outcome.artifact.sha256


def test_auto_trainer_blocks_failed_validation_even_when_fit_succeeds(tmp_path):
    schema = _schema()
    examples = _examples(schema)

    def fit(rows, seed):
        del rows, seed
        return _MeanFitted("bad-model", 0.0)

    model = CallableTrainableModel(
        "bad-model",
        "trainer-v1",
        TaskKind.REGRESSION,
        schema,
        fit,
        DeterminismLevel.EXACT,
    )
    outcome = run_auto_training(
        tmp_path / "bad.db",
        model=model,
        examples=examples,
        plan=_plan(dataset_fingerprint(examples, schema)),
        metrics=(
            CallableValidationMetric("mae", MetricDirection.MINIMIZE, 0.10, _mae),
        ),
        code_revision="abc123",
        policy_fingerprint="policy-sha",
        now=NOW,
    )
    assert outcome.report.status is TrainingRunStatus.VALIDATION_FAILED
    assert outcome.report.shadow_ready is False
    assert any(item.startswith("validation_metric_failed:mae") for item in outcome.report.failures)


def test_retraining_plan_is_bound_to_exact_dataset_fingerprint(tmp_path):
    schema = _schema()
    examples = _examples(schema)
    model = _mean_model(schema)
    with pytest.raises(ValueError, match="dataset fingerprint"):
        run_auto_training(
            tmp_path / "mismatch.db",
            model=model,
            examples=examples,
            plan=_plan("0" * 64),
            metrics=(
                CallableValidationMetric("mae", MetricDirection.MINIMIZE, 0.01, _mae),
            ),
            code_revision="abc123",
            policy_fingerprint="policy-sha",
            now=NOW,
        )


def test_validation_evaluator_failure_is_durable_and_fail_closed(tmp_path):
    schema = _schema()
    examples = _examples(schema)
    model = _mean_model(schema, "metric-failure-model")

    def broken_metric(rows) -> float:
        del rows
        raise RuntimeError("fault-injected metric failure")

    outcome = run_auto_training(
        tmp_path / "metric-failure.db",
        model=model,
        examples=examples,
        plan=_plan(dataset_fingerprint(examples, schema)),
        metrics=(
            CallableValidationMetric(
                "broken_metric",
                MetricDirection.MINIMIZE,
                0.01,
                broken_metric,
            ),
        ),
        code_revision="abc123",
        policy_fingerprint="policy-sha",
        now=NOW,
    )
    assert outcome.report.status is TrainingRunStatus.VALIDATION_FAILED
    assert outcome.artifact is not None
    assert "validation_evaluation_failed:RuntimeError" in outcome.report.failures
    persisted = load_training_run(tmp_path / "metric-failure.db", outcome.report.run_id)
    assert persisted["status"] == TrainingRunStatus.VALIDATION_FAILED.value


def test_non_finite_metric_threshold_is_rejected():
    with pytest.raises(ValueError, match="finite"):
        CallableValidationMetric("mae", MetricDirection.MINIMIZE, float("nan"), _mae)


def test_dataset_fingerprint_binds_feature_values_and_schema():
    schema = _schema()
    rows = _examples(schema, count=3)
    baseline = dataset_fingerprint(rows, schema)
    changed = list(rows)
    changed[0] = TrainingExample(
        sample_id=rows[0].sample_id,
        observed_ts_utc=rows[0].observed_ts_utc,
        features=schema.normalize({"x": 999.0}),
        target=rows[0].target,
    )
    assert dataset_fingerprint(tuple(changed), schema) != baseline
    with pytest.raises(ValueError):
        schema.normalize({"x": 1.0, "future_feature": 2.0})


def test_shadow_activation_is_automatic_but_isolated_from_live_release(tmp_path):
    schema = _schema()
    examples = _examples(schema)

    def fit(rows, seed):
        del rows, seed
        return _MeanFitted("shadow-model", 1.0)

    model = CallableTrainableModel(
        "shadow-model",
        "trainer-v1",
        TaskKind.REGRESSION,
        schema,
        fit,
        DeterminismLevel.EXACT,
    )
    outcome = run_auto_training(
        tmp_path / "shadow.db",
        model=model,
        examples=examples,
        plan=_plan(dataset_fingerprint(examples, schema)),
        metrics=(
            CallableValidationMetric("mae", MetricDirection.MINIMIZE, 0.01, _mae),
        ),
        code_revision="abc123",
        policy_fingerprint="policy-sha",
        now=NOW,
    )
    registry = ShadowModelRegistry(tmp_path / "shadow.db")
    record = registry.register_and_activate(
        component="entry-quality-model",
        training=outcome.report,
        promotion=_ready_promotion(),
        parent_release_id="live-parent-v1",
        now=NOW,
    )
    assert record.state is ShadowReleaseState.ACTIVE_SHADOW
    assert registry.active("entry-quality-model") == record


def test_automatic_protection_quarantines_and_fails_closed_without_live_rollback(tmp_path):
    path = tmp_path / "release.db"
    registry = ReleaseRegistry(path)
    promotion = _ready_promotion()
    first = registry.register(
        component="entry-model",
        artifact_hash="a" * 64,
        policy_fingerprint="p" * 64,
        research_manifest_hash="m" * 64,
        now=NOW,
    )
    registry.activate(first.release_id, operator="operator-a", promotion=promotion, now=NOW)
    second = registry.register(
        component="entry-model",
        artifact_hash="b" * 64,
        policy_fingerprint="p" * 64,
        research_manifest_hash="n" * 64,
        previous_release_id=first.release_id,
        now=NOW + timedelta(minutes=1),
    )
    registry.activate(
        second.release_id,
        operator="operator-a",
        promotion=promotion,
        now=NOW + timedelta(minutes=1),
    )
    latch = NoTradeSafetyLatch(path)
    protected = quarantine_and_trip_no_trade(
        registry,
        latch,
        component="entry-model",
        reason="shadow/live degradation alarm",
        now=NOW + timedelta(minutes=2),
    )
    assert protected.safety_state.mode is SafetyMode.NO_TRADE
    assert protected.rollback_plan is not None
    assert protected.rollback_plan.rollback_release_id == first.release_id
    assert registry.get(second.release_id).state is ReleaseState.QUARANTINED
    assert registry.get(first.release_id).state is ReleaseState.SUPERSEDED
    with pytest.raises(RuntimeError):
        latch.assert_execution_allowed("entry-model")
    cleared = latch.clear_no_trade(
        "entry-model",
        operator="operator-b",
        reason="manual evidence review complete",
        now=NOW + timedelta(minutes=3),
    )
    assert cleared.mode is SafetyMode.NORMAL
