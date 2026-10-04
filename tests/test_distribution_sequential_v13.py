from scorpion.distribution_robust import (
    DistributionRobustStatus,
    DistributionShiftPolicy,
    evaluate_distribution_robustness,
)
from scorpion.regime_stability import (
    RegimeAxisReport,
    RegimeGroup,
    RegimeStabilityReport,
    RegimeStabilityStatus,
)
from scorpion.sequential_evidence import (
    BernoulliEPolicy,
    BernoulliEProcess,
    ExecutionEvidenceMonitor,
    ExecutionEvidenceMonitorPolicy,
    SequentialEvidenceStatus,
)


def _axis(axis: str, means: tuple[float, float], shares: tuple[float, float]) -> RegimeAxisReport:
    groups = tuple(
        RegimeGroup(
            axis=axis,
            bucket=f"g{index}",
            samples=50,
            mean_return=mean,
            win_rate=0.60 if mean > 0 else 0.40,
            sample_share=share,
            qualified=True,
        )
        for index, (mean, share) in enumerate(zip(means, shares, strict=True))
    )
    return RegimeAxisReport(
        axis=axis,
        groups=groups,
        qualified_groups=2,
        positive_qualified_groups=sum(group.mean_return > 0 for group in groups),
        positive_group_ratio=sum(group.mean_return > 0 for group in groups) / 2,
        dominant_group_share=max(shares),
        status=RegimeStabilityStatus.PASS,
    )


def test_distribution_shift_passes_when_edge_survives_hostile_regime_mix():
    regime = RegimeStabilityReport(
        total_samples=100,
        axes=(
            _axis("spread", (0.08, 0.03), (0.6, 0.4)),
            _axis("latency", (0.06, 0.02), (0.5, 0.5)),
        ),
        status=RegimeStabilityStatus.PASS,
        failures=(),
    )
    report = evaluate_distribution_robustness(
        regime,
        policy=DistributionShiftPolicy(density_ratio_bound=2.0),
    )
    assert report.status is DistributionRobustStatus.PASS
    assert report.passed is True
    assert all(axis.worst_case_mean_return > 0 for axis in report.axes)
    assert all(axis.stress_loss >= 0 for axis in report.axes)


def test_distribution_shift_rejects_edge_that_depends_on_favorable_mix():
    regime = RegimeStabilityReport(
        total_samples=100,
        axes=(
            _axis("spread", (0.12, -0.08), (0.8, 0.2)),
            _axis("latency", (0.10, -0.06), (0.8, 0.2)),
        ),
        status=RegimeStabilityStatus.PASS,
        failures=(),
    )
    report = evaluate_distribution_robustness(
        regime,
        policy=DistributionShiftPolicy(density_ratio_bound=3.0),
    )
    assert report.status is DistributionRobustStatus.FAIL
    assert report.passed is False
    assert any(axis.worst_case_mean_return < 0 for axis in report.axes)


def test_anytime_valid_e_process_stays_calm_under_low_violation_rate():
    process = BernoulliEProcess(
        BernoulliEPolicy(null_rate=0.05, alternative_rate=0.20, alpha=0.01)
    )
    for index in range(200):
        point = process.observe(index % 50 == 0)
    assert point.status is SequentialEvidenceStatus.HEALTHY
    assert point.empirical_rate <= 0.05
    assert point.anytime_p_value > 0.01


def test_anytime_valid_e_process_alarms_on_sustained_degradation():
    process = BernoulliEProcess(
        BernoulliEPolicy(null_rate=0.01, alternative_rate=0.20, alpha=0.01)
    )
    point = process.snapshot()
    for _ in range(40):
        point = process.observe(True)
        if point.status is SequentialEvidenceStatus.ALARM:
            break
    assert point.status is SequentialEvidenceStatus.ALARM
    assert point.anytime_p_value <= 0.01


def test_combined_execution_monitor_escalates_if_either_evidence_stream_alarms():
    monitor = ExecutionEvidenceMonitor(
        ExecutionEvidenceMonitorPolicy(
            fill_bound=BernoulliEPolicy(
                null_rate=0.01,
                alternative_rate=0.20,
                alpha=0.01,
            ),
            actionable_model_error=BernoulliEPolicy(
                null_rate=0.01,
                alternative_rate=0.20,
                alpha=0.01,
            ),
        )
    )
    snapshot = monitor.snapshot()
    for _ in range(40):
        snapshot = monitor.observe_actionable_model_error(
            wrong_actionable_singleton=True
        )
        if snapshot.status is SequentialEvidenceStatus.ALARM:
            break
    assert snapshot.status is SequentialEvidenceStatus.ALARM
    assert snapshot.actionable_model_error.status is SequentialEvidenceStatus.ALARM
