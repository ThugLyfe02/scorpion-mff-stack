"""Frozen-cohort research diagnostics; no money-path dependencies or authority.

The endpoint is clipped negative implementation shortfall, not trade return.
All statistical units are exchange sessions, not individual correlated fills.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .distribution_robust import bounded_mixture_stress
from .execution_meta_labels import _bounded_bytes, _count, _fields, _hex, _stamp, _unique_json
from .information_value import (
    LearningBudgetAllocation,
    LearningValueAssessment,
    allocate_learning_budget,
)
from .paper_session_statistics import _number, load_verified_paper_cohort
from .provenance import stable_hash
from .sequential_evidence import BernoulliEPolicy, BernoulliEProcess
from .session_calendar import session_hours

Interval = tuple[float, float]


@dataclass(frozen=True, slots=True)
class AdaptivePlan:
    digest: str
    baseline_cohort: str
    baseline_report: str
    monitor_cohort: str
    baseline_cutoff: datetime
    frozen_at: datetime
    train_sessions: int
    miscoverage: float
    density_ratio: float
    novel_mass: float
    score_floor: float
    adverse_null: float
    adverse_alternative: float
    family_alpha: float
    budget_units: int
    economic_floor: float
    economic_clip: float


def load_adaptive_plan(path: Path, pin: str) -> AdaptivePlan:
    raw = _bounded_bytes(path, 65536)
    if hashlib.sha256(raw).hexdigest() != pin:
        raise ValueError("adaptive plan pin mismatch")
    data = _unique_json(raw)
    _fields(data, {
        "schema", "authority", "baseline_cohort_sha256", "baseline_report_sha256",
        "monitor_cohort_sha256", "baseline_cutoff", "frozen_at", "train_sessions",
        "miscoverage", "density_ratio", "novel_mass", "score_floor_bps",
        "adverse_null", "adverse_alternative", "family_alpha", "budget_units",
        "economic_floor_bps", "economic_clip_bps",
    })
    if (
        data["schema"] != "scorpion.paper-adaptive-plan.v1"
        or data["authority"] != "RESEARCH_ONLY"
    ):
        raise ValueError("adaptive plan authority")
    cutoff, freeze = _stamp(data["baseline_cutoff"]), _stamp(data["frozen_at"])
    train = _count(data["train_sessions"])
    budget = _count(data["budget_units"])
    p = _number(data["adverse_null"], minimum=0.001, maximum=0.95)
    q = _number(data["adverse_alternative"], minimum=0.002, maximum=0.999)
    economic_clip = _number(data["economic_clip_bps"], minimum=1, maximum=100_000)
    if cutoff > freeze or not 5 <= train <= 247 or budget > 100 or q <= p:
        raise ValueError("adaptive plan bounds")
    return AdaptivePlan(
        pin, _hex(data["baseline_cohort_sha256"], 32).hex(),
        _hex(data["baseline_report_sha256"], 32).hex(),
        _hex(data["monitor_cohort_sha256"], 32).hex(), cutoff, freeze, train,
        _number(data["miscoverage"], minimum=0.01, maximum=0.25),
        _number(data["density_ratio"], minimum=1, maximum=10),
        _number(data["novel_mass"], minimum=0, maximum=0.5),
        _number(data["score_floor_bps"], minimum=-100_000, maximum=100_000),
        p, q, _number(data["family_alpha"], minimum=1e-6, maximum=0.05),
        budget, _number(data["economic_floor_bps"], minimum=-economic_clip,
                        maximum=economic_clip), economic_clip,
    )


@dataclass(frozen=True, slots=True)
class UncertaintyEnvelope:
    center_bps: float | None
    interval_bps: Interval
    train_sessions: int
    known_train_sessions: int
    calibration_sessions: int
    known_calibration_sessions: int
    rank: int
    nominal_miscoverage: float
    status: str
    assumption: str = "FINITE_SAMPLE_COVERAGE_REQUIRES_EXCHANGEABLE_SESSION_SCORES"


def _validate(intervals: tuple[Interval, ...], clip: float) -> None:
    if not math.isfinite(clip) or clip <= 0 or len(intervals) > 252:
        raise ValueError("invalid score support")
    if any(not (-clip <= lo <= hi <= clip) for lo, hi in intervals):
        raise ValueError("invalid score interval")


def split_session_envelope(
    intervals: tuple[Interval, ...], *, train_sessions: int, clip: float, alpha: float,
) -> UncertaintyEnvelope:
    """A fixed train/calibration split with worst-case censored residuals.

    A missing calibration session occupies its original rank and contributes
    the largest residual allowed by its interval. No IID fill assumption.
    """
    _validate(intervals, clip)
    if not 5 <= train_sessions < len(intervals) or not 0 < alpha < 1:
        raise ValueError("invalid chronological split")
    train, cal = intervals[:train_sessions], intervals[train_sessions:]
    known = [lo for lo, hi in train if lo == hi]
    rank = math.ceil((len(cal) + 1) * (1 - alpha))
    known_cal = sum(lo == hi for lo, hi in cal)
    center = statistics.fmean(known) if known else None
    if center is None or len(known) < 5 or rank > len(cal):
        return UncertaintyEnvelope(
            center, (-clip, clip), len(train), len(known), len(cal), known_cal,
            rank, alpha, "ABSTAIN_INSUFFICIENT_RESOLUTION",
        )
    radius = sorted(max(abs(lo-center), abs(hi-center)) for lo, hi in cal)[rank-1]
    bounds = (max(-clip, center-radius), min(clip, center+radius))
    status = "ABSTAIN_FULL_SUPPORT" if bounds == (-clip, clip) else "DESCRIPTIVE_ENVELOPE"
    return UncertaintyEnvelope(
        center, bounds, len(train), len(known), len(cal), known_cal, rank, alpha, status,
    )


@dataclass(frozen=True, slots=True)
class RobustEnvelope:
    baseline_identification_bps: Interval
    stressed_identification_bps: Interval
    stressed_adverse_probability: Interval
    worst_weights: tuple[tuple[str, float], ...]
    best_weights: tuple[tuple[str, float], ...]
    density_ratio: float
    novel_mass: float
    estimand: str = "EMPIRICAL_SESSION_MIXTURE_WITH_BOUNDED_NOVEL_SUPPORT"


def robust_session_envelope(
    intervals: tuple[Interval, ...], *, clip: float, ratio: float,
    novel_mass: float, floor: float,
) -> RobustEnvelope:
    """Exact box-constrained empirical mixture plus arbitrary bounded contamination."""
    _validate(intervals, clip)
    if not intervals or not 0 <= novel_mass <= 1 or not -clip <= floor <= clip:
        raise ValueError("invalid robust stress scope")
    n = len(intervals)

    def solve(values: list[float]) -> tuple[float, tuple[tuple[str, float], ...]]:
        return bounded_mixture_stress(
            [(str(i), 1/n, value) for i, value in enumerate(values)],
            density_ratio_bound=ratio,
        )

    lo, worst = solve([a for a, _ in intervals])
    negative_hi, best = solve([-b for _, b in intervals])
    confirmed, _ = solve([float(b < floor) for _, b in intervals])
    possible_negative, _ = solve([-float(a < floor) for a, _ in intervals])
    keep = 1-novel_mass
    bounds = (
        max(-clip, keep*lo-novel_mass*clip),
        min(clip, -keep*negative_hi+novel_mass*clip),
    )
    adverse = (
        max(0.0, keep*confirmed + novel_mass*float(clip < floor)),
        min(1.0, -keep*possible_negative + novel_mass*float(-clip < floor)),
    )
    return RobustEnvelope(
        (statistics.fmean(a for a, _ in intervals), statistics.fmean(b for _, b in intervals)),
        bounds, adverse, worst, best, ratio, novel_mass,
    )


@dataclass(frozen=True, slots=True)
class DecayEvidence:
    sessions: int
    confirmed_adverse: int
    ambiguous: int
    confirmed_max_log_e: float
    possible_max_log_e: float
    confirmed_anytime_p: float
    alpha: float
    status: str
    first_alarm_index: int | None
    scope: str = "FIXED_ROSTER_CHANGE_START_MIXTURE_CONDITIONAL_BERNOULLI_NULL"


def _logmeanexp(values: list[float]) -> float:
    maximum = max(values)
    return maximum + math.log(math.fsum(math.exp(v-maximum) for v in values)/len(values))


def monitor_decay(
    intervals: tuple[Interval, ...], *, planned: int, clip: float,
    floor: float, null: float, alternative: float, alpha: float,
) -> DecayEvidence:
    """Uniform mixture over every predeclared possible change start.

    Dormant bets have value 1. The confirmed indicator is a pathwise lower
    bound on the true adverse indicator. The possible path is diagnostic only.
    No uncorrected max over windows and no resetting after a bad result.
    """
    _validate(intervals, clip)
    if not 1 <= planned <= 252 or len(intervals) > planned or not -clip <= floor <= clip:
        raise ValueError("invalid decay scope")
    policy = BernoulliEPolicy(null, alternative, alpha)
    lower = [BernoulliEProcess(policy) for _ in range(planned)]
    upper = [BernoulliEProcess(policy) for _ in range(planned)]
    max_lo = max_hi = 0.0
    alarm = None
    confirmed = ambiguous = 0
    threshold = -math.log(alpha)
    for i, (lo, hi) in enumerate(intervals):
        known_bad, possibly_bad = hi < floor, lo < floor
        confirmed += int(known_bad)
        ambiguous += int(possibly_bad and not known_bad)
        for j in range(i+1):
            lower[j].observe(known_bad)
            upper[j].observe(possibly_bad)
        max_lo = max(max_lo, _logmeanexp([p.log_e_value for p in lower]))
        max_hi = max(max_hi, _logmeanexp([p.log_e_value for p in upper]))
        if alarm is None and max_lo >= threshold:
            alarm = i+1
    status = (
        "CONFIRMED_ADVERSE_ALARM" if alarm is not None else
        "POSSIBLE_ADVERSE_WATCH" if max_hi >= threshold else
        "UNRESOLVED" if ambiguous or not intervals else "NO_ALARM_NOT_PROOF_OF_HEALTH"
    )
    return DecayEvidence(
        len(intervals), confirmed, ambiguous, max_lo, max_hi, math.exp(-max_lo),
        alpha, status, alarm,
    )


@dataclass(frozen=True, slots=True)
class CoverageDiagnostic:
    sessions: int
    certainly_covered: int
    possibly_covered: int
    certainly_missed: int


def assess_envelope(intervals: tuple[Interval, ...], predicted: Interval) -> CoverageDiagnostic:
    lo, hi = predicted
    certain = sum(lo <= a <= b <= hi for a, b in intervals)
    possible = sum(a <= hi and b >= lo for a, b in intervals)
    return CoverageDiagnostic(len(intervals), certain, possible, len(intervals)-possible)


def allocate_diagnostic_research(
    *, uncertainty: UncertaintyEnvelope, robust: RobustEnvelope, decay: DecayEvidence,
    economic: DecayEvidence, coverage: CoverageDiagnostic, clip: float, budget: int,
) -> tuple[tuple[LearningValueAssessment, ...], LearningBudgetAllocation]:
    """Transparent deficit-based information utility, not inferred economic alpha.

    One hypothesis family per information deficit prevents duplicating a task to
    collect extra budget. A plan only; no compute or training is dispatched.
    """
    width = (uncertainty.interval_bps[1]-uncertainty.interval_bps[0])/(2*clip)
    shift = min(1.0, max(0.0, (
        robust.baseline_identification_bps[0]-robust.stressed_identification_bps[0]
    )/(2*clip)))
    missing = (
        (coverage.possibly_covered-coverage.certainly_covered)/coverage.sessions
        if coverage.sessions else 1.0
    )

    def urgency(d: DecayEvidence) -> float:
        return max(
            min(1.0, d.confirmed_max_log_e/-math.log(d.alpha)),
            d.ambiguous/max(1, d.sessions), 0.5 if not d.sessions else 0.0,
        )

    deficits = (
        ("calibration_resolution", width, False),
        ("distribution_shift_stress", shift, False),
        ("execution_evidence_completeness", missing, True),
        ("execution_cost_degradation", urgency(decay), False),
        ("economic_outcome_degradation", urgency(economic), True),
    )
    rows = tuple(LearningValueAssessment(
        name, score, 1.0, 1.0,
        0.45*score if not review else 0.0,
        0.9*score if not review else 0.25*score,
        score if review else 0.35*score,
        False, ("deficit_proxy_not_measured_information_gain",),
    ) for name, score, review in deficits)
    return rows, allocate_learning_budget(rows, budget_units=budget)


@dataclass(frozen=True, slots=True)
class AdaptiveReport:
    plan_sha256: str
    baseline_report_sha256: str
    monitor_report_sha256: str
    as_of: datetime
    monitor_sessions: tuple[date, ...]
    uncertainty: UncertaintyEnvelope
    robustness: RobustEnvelope
    historical_research_recommendation: str
    holdout_coverage: CoverageDiagnostic
    execution_decay: DecayEvidence
    economic_decay: DecayEvidence
    economic_evidence: tuple[tuple[date, Interval, str], ...]
    research_priorities: tuple[LearningValueAssessment, ...]
    allocation: LearningBudgetAllocation
    report_sha256: str
    authority: str = "RESEARCH_ONLY"
    authorizes_training: bool = False
    authorizes_promotion: bool = False
    authorizes_orders: bool = False


def evaluate_adaptive_research(
    *, plan_path: Path, plan_pin: str, baseline_cohort: Path, baseline_inputs: Path,
    monitor_cohort: Path, monitor_inputs: Path, trust_path: Path, cost_policy: Path,
    as_of: datetime, economic_inputs: Path | None = None,
) -> AdaptiveReport:
    from .paper_economic_surveillance import economic_intervals

    plan = load_adaptive_plan(plan_path, plan_pin)
    if (
        as_of.tzinfo is None or as_of.utcoffset() is None
        or not plan.frozen_at <= as_of <= datetime.now(UTC)
    ):
        raise ValueError("invalid adaptive cutoff")
    baseline = load_verified_paper_cohort(
        cohort_path=baseline_cohort, expected_cohort_sha256=plan.baseline_cohort,
        inputs_path=baseline_inputs, trust_path=trust_path, cost_policy_path=cost_policy,
        as_of=plan.baseline_cutoff,
    )
    target = load_verified_paper_cohort(
        cohort_path=monitor_cohort, expected_cohort_sha256=plan.monitor_cohort,
        inputs_path=monitor_inputs, trust_path=trust_path, cost_policy_path=cost_policy,
        as_of=as_of,
    )
    first = session_hours(target.policy.sessions[0])
    if (
        first is None or plan.frozen_at > first[0]-timedelta(hours=24)
        or baseline.report.report_sha256 != plan.baseline_report
        or baseline.report.future_sessions
        or baseline.policy.sessions[-1] >= target.policy.sessions[0]
        or baseline.policy.clip_bps != target.policy.clip_bps
        or set(baseline.evidence_identities) & set(target.evidence_identities)
    ):
        raise ValueError("adaptive baseline or holdout identity conflict")
    clip = baseline.policy.clip_bps
    past = tuple(s.primary_bounds_bps for s in baseline.report.sessions)
    now = tuple(s.primary_bounds_bps for s in target.report.sessions)
    uncertainty = split_session_envelope(
        past, train_sessions=plan.train_sessions, clip=clip, alpha=plan.miscoverage,
    )
    robust = robust_session_envelope(
        past, clip=clip, ratio=plan.density_ratio, novel_mass=plan.novel_mass,
        floor=plan.score_floor,
    )
    decision = (
        "RESEARCH_CANDIDATE_ONLY" if (
            uncertainty.status == "DESCRIPTIVE_ENVELOPE"
            and uncertainty.interval_bps[0] > plan.score_floor
            and robust.stressed_identification_bps[0] > plan.score_floor
            and baseline.report.primary.status == "BOUNDED_RESEARCH_SUPPORT"
        ) else "ABSTAIN_FROM_RESEARCH_QUALIFICATION"
    )
    coverage = assess_envelope(now, uncertainty.interval_bps)
    decay = monitor_decay(
        now, planned=len(target.policy.sessions), clip=clip, floor=plan.score_floor,
        null=plan.adverse_null, alternative=plan.adverse_alternative, alpha=plan.family_alpha/2,
    )
    economic_rows = economic_intervals(
        target, cohort_inputs=monitor_inputs, trust_path=trust_path,
        index_path=economic_inputs, clip=plan.economic_clip,
        prior_costs=baseline.costs,
    )
    economic = monitor_decay(
        tuple(interval for _, interval, _ in economic_rows),
        planned=len(target.policy.sessions), clip=plan.economic_clip,
        floor=plan.economic_floor, null=plan.adverse_null,
        alternative=plan.adverse_alternative, alpha=plan.family_alpha/2,
    )
    priorities, allocation = allocate_diagnostic_research(
        uncertainty=uncertainty, robust=robust, decay=decay, economic=economic,
        coverage=coverage, clip=clip, budget=plan.budget_units,
    )
    fields = (
        plan.digest, baseline.report.report_sha256, target.report.report_sha256, as_of,
        tuple(s.session_date for s in target.report.sessions), uncertainty, robust, decision,
        coverage, decay, economic, economic_rows, priorities, allocation,
    )
    return AdaptiveReport(*fields, stable_hash(("paper-adaptive-research-v1", fields)))
