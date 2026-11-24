"""Stage 3: evidence-bound, fixed-cohort session statistics (research only).

The orchestration reuses Stage 2, the existing block bootstrap, confidence sequence,
regime classification, shrinkage and mixture solver. It does not estimate live P&L
or submit, train, rank, promote or authorize a trading policy.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .confidence_sequence import (
    BoundedMeanConfidenceSequence,
    anytime_bounded_mean_confidence_sequence,
)
from .config import MARKET_TZ
from .cost_adjusted_policy_value import SessionMeanDiagnostics, session_mean_diagnostics
from .counterfactual import compare_paper_no_chase
from .distribution_robust import bounded_mixture_stress
from .execution_attribution import (
    PaperExecutionCostReport,
    _load_cost_policy,
    build_paper_execution_cost_report,
)
from .execution_meta_labels import (
    PaperOutcomeAdmission,
    _bounded_bytes,
    _count,
    _fields,
    _hex,
    _parse_trust,
    _stamp,
    _text,
    _unique_json,
)
from .hierarchical_shrinkage import HierarchicalPolicy, evaluate_numeric_group_shrinkage
from .provenance import stable_hash
from .regime_stability import RegimeStabilityPolicy, classify_execution_regimes
from .session_calendar import CalendarNotCovered, session_hours
from .session_service import JournalError

_MAX_SESSIONS = 252
_MAX_ROUTES = 100_000
_FAMILY_ENDPOINTS = 2
_AXES = ("spread", "quote_age", "decision_latency", "session")
# Fixed v1 equity bands, deliberately distinct from options defaults.
_REGIMES = RegimeStabilityPolicy(
    tight_spread_bps=10, medium_spread_bps=50,
    fresh_quote_age_ms=100, normal_quote_age_ms=1000,
    fast_decision_latency_ms=120_000, normal_decision_latency_ms=300_000,
)


class PaperCohortError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PaperCohortPolicy:
    family_id: str
    registered_ts_utc: datetime
    start_date: date
    end_date: date
    sessions: tuple[date, ...]
    trust_sha256: str
    cost_policy_sha256: str
    family_alpha: float
    clip_bps: float
    minimum_sessions: int
    minimum_routes: int
    minimum_complete_fraction: float
    minimum_effective_sessions: float
    maximum_session_share: float
    maximum_ticker_share: float
    minimum_primary_score_bps: float
    minimum_chase_score_bps: float
    minimum_observed_chases: int
    maximum_tail_loss_bps: float
    minimum_group_sessions: int
    density_ratio_bound: float
    block_days: int
    bootstrap_trials: int
    random_seed: int


@dataclass(frozen=True, slots=True)
class PaperSessionStatistic:
    session_date: date
    status: str
    expected_routes: int | None
    observed_primary_routes: int
    known_chase_score_routes: int
    observed_terminal_chase_routes: int
    primary_bounds_bps: tuple[float, float]
    chase_bounds_bps: tuple[float, float]
    primary_raw_mean_bps: float | None
    chase_raw_mean_bps: float | None
    cost_report_sha256: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperEndpointStatistics:
    endpoint: str
    observed_routes: int
    known_expected_routes: int
    clipped_routes: int
    raw_route_mean_bps: float | None
    raw_worst_route_bps: float | None
    raw_lower_tail_mean_bps: float | None
    complete_session_diagnostics: SessionMeanDiagnostics
    bootstrap_block_sensitivity: tuple[tuple[int, float | None, float | None], ...]
    anytime_conditional_mean: BoundedMeanConfidenceSequence
    identification_interval_bps: tuple[float, float] | None
    threshold_bps: float
    status: str
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperRegimeGroup:
    axis: str
    bucket: str
    expected_routes: int
    observed_routes: int
    observed_sessions: int
    raw_session_mean_bps: float | None
    raw_route_mean_bps: float | None
    pooled_session_mean_bps: float | None
    eligible_for_descriptive_stress: bool


@dataclass(frozen=True, slots=True)
class PaperRegimeAxis:
    axis: str
    groups: tuple[PaperRegimeGroup, ...]
    dominant_route_share: float | None
    worst_case_observed_mixture_bps: float | None
    worst_case_weights: tuple[tuple[str, float], ...]
    status: str


@dataclass(frozen=True, slots=True)
class PaperSessionCohortReport:
    cohort_sha256: str
    family_id: str
    trust_sha256: str
    cost_policy_sha256: str
    as_of_ts_utc: datetime
    planned_sessions: int
    elapsed_sessions: int
    future_sessions: int
    covered_sessions: int
    complete_sessions: int
    known_expected_routes: int
    observed_primary_routes: int
    observed_terminal_chase_routes: int
    missing_or_invalid_sessions: int
    observed_route_coverage: float | None
    kish_effective_session_count: float | None
    maximum_observed_session_share: float | None
    maximum_observed_ticker_share: float | None
    maximum_absolute_score_ticker_share: float | None
    sessions: tuple[PaperSessionStatistic, ...]
    primary: PaperEndpointStatistics
    chase: PaperEndpointStatistics
    primary_regimes: tuple[PaperRegimeAxis, ...]
    chase_regimes: tuple[PaperRegimeAxis, ...]
    regime_robustness: str
    report_sha256: str
    authority: str = "RESEARCH_ONLY"
    estimand: str = "EQUAL_SESSION_CLIPPED_ENTRY_COST_SCORE_NOT_RETURN_OR_CAUSAL_ALPHA"
    familywise_alpha_scope: str = "TWO_PINNED_ENDPOINTS_WITHIN_THIS_COHORT_ONLY"
    authorizes_training: bool = False
    authorizes_promotion: bool = False
    certifies_live_fills: bool = False


@dataclass(frozen=True, slots=True)
class _RouteScore:
    call_id: str
    ticker: str
    regimes: tuple[tuple[str, str], ...]
    primary: float | None
    chase: float | None
    terminal_chase_observed: bool


@dataclass(frozen=True, slots=True)
class _Day:
    date: date
    status: str
    expected: int | None
    scores: tuple[_RouteScore, ...]
    report_hash: str | None
    reasons: tuple[str, ...]


def _number(value: object, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PaperCohortError("invalid_cohort_number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise PaperCohortError("cohort_number_outside_bounds")
    return result


def _day(value: object) -> date:
    raw = _text(value)
    result = date.fromisoformat(raw)
    if result.isoformat() != raw:
        raise PaperCohortError("noncanonical_session_date")
    return result


def _calendar_days(start: date, end: date) -> tuple[date, ...]:
    if start > end or (end - start).days > 370:
        raise PaperCohortError("cohort_calendar_bound")
    result: list[date] = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        if session_hours(day) is not None:
            result.append(day)
    if not 1 <= len(result) <= _MAX_SESSIONS:
        raise PaperCohortError("cohort_session_count_bound")
    return tuple(result)


def _load_cohort(raw: bytes, expected_hash: str) -> PaperCohortPolicy:
    _hex(expected_hash, 32)
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise PaperCohortError("cohort_pin_mismatch")
    data = _unique_json(raw)
    _fields(data, {
        "schema", "authority", "analysis_version", "family_id", "registered_ts_utc",
        "start_date", "end_date",
        "sessions", "trust_sha256", "cost_policy_sha256", "family_alpha", "clip_bps",
        "minimum_sessions", "minimum_routes", "minimum_complete_fraction",
        "minimum_effective_sessions", "maximum_session_share", "maximum_ticker_share",
        "minimum_primary_score_bps", "minimum_chase_score_bps", "minimum_observed_chases",
        "maximum_tail_loss_bps", "minimum_group_sessions", "density_ratio_bound",
        "block_days", "bootstrap_trials", "random_seed",
    })
    if (
        data["schema"] != "scorpion.paper-session-cohort.v1"
        or data["authority"] != "RESEARCH_ONLY"
        or data["analysis_version"] != "paper-session-statistics-v1"
    ):
        raise PaperCohortError("cohort_scope_mismatch")
    start, end = _day(data["start_date"]), _day(data["end_date"])
    sessions = _calendar_days(start, end)
    declared = data["sessions"]
    if not isinstance(declared, list) or tuple(_day(v) for v in declared) != sessions:
        raise PaperCohortError("cohort_calendar_membership_mismatch")
    registered = _stamp(data["registered_ts_utc"])
    first_hours = session_hours(sessions[0])
    if first_hours is None or registered >= first_hours[0]:
        raise PaperCohortError("cohort_registration_not_prospective")
    clip = _number(data["clip_bps"], minimum=1, maximum=100_000)
    minimum_sessions = _count(data["minimum_sessions"])
    block_days = _count(data["block_days"])
    trials = _count(data["bootstrap_trials"])
    minimum_group_sessions = _count(data["minimum_group_sessions"])
    if (
        minimum_sessions < 2 or minimum_sessions > len(sessions)
        or not 1 <= block_days <= min(30, len(sessions))
        or not 1000 <= trials <= 20_000
        or not 2 <= minimum_group_sessions <= len(sessions)
    ):
        raise PaperCohortError("cohort_sample_policy_bounds")
    return PaperCohortPolicy(
        _text(data["family_id"]), registered, start, end, sessions,
        _hex(data["trust_sha256"], 32).hex(), _hex(data["cost_policy_sha256"], 32).hex(),
        _number(data["family_alpha"], minimum=1e-6, maximum=0.05), clip,
        minimum_sessions, _count(data["minimum_routes"]),
        _number(data["minimum_complete_fraction"], minimum=0.8, maximum=1),
        _number(data["minimum_effective_sessions"], minimum=2, maximum=len(sessions)),
        _number(data["maximum_session_share"], minimum=0.01, maximum=1),
        _number(data["maximum_ticker_share"], minimum=0.01, maximum=1),
        _number(data["minimum_primary_score_bps"], minimum=-clip, maximum=clip),
        _number(data["minimum_chase_score_bps"], minimum=-clip, maximum=clip),
        _count(data["minimum_observed_chases"]),
        _number(data["maximum_tail_loss_bps"], minimum=0, maximum=1e9),
        minimum_group_sessions,
        _number(data["density_ratio_bound"], minimum=1, maximum=10),
        block_days, trials, _count(data["random_seed"]),
    )


def _read_signature(path: Path) -> str:
    raw = _bounded_bytes(path, 256)
    return raw.decode("ascii").strip()


def _input_rows(path: Path, elapsed: tuple[date, ...]) -> dict[date, dict[str, Path]]:
    data = _unique_json(_bounded_bytes(path, 1024 * 1024))
    _fields(data, {"schema", "sessions"})
    if data["schema"] != "scorpion.paper-session-inputs.v1":
        raise PaperCohortError("session_input_schema")
    if not isinstance(data["sessions"], list) or len(data["sessions"]) > _MAX_SESSIONS:
        raise PaperCohortError("session_input_bound")
    fields = {
        "journal", "outcome_export", "source_artifact", "collector_signature",
        "reviewer_signature", "cost_export", "market_artifact", "cost_collector_signature",
        "cost_reviewer_signature",
    }
    result: dict[date, dict[str, Path]] = {}
    for row in data["sessions"]:
        if not isinstance(row, dict):
            raise PaperCohortError("session_input_row")
        _fields(row, fields | {"session_date"})
        day = _day(row["session_date"])
        if day not in elapsed or day in result:
            raise PaperCohortError("duplicate_or_outside_elapsed_session")
        result[day] = {key: path.parent / _text(row[key]) for key in fields}
    return result


def _score(value: object) -> float:
    try:
        result = float(str(value))
    except ValueError as exc:
        raise PaperCohortError("invalid_route_score") from exc
    if not math.isfinite(result):
        raise PaperCohortError("nonfinite_route_score")
    return result


def _as_day(day: date, cost: PaperExecutionCostReport) -> _Day:
    coverage = cost.session_coverage
    if coverage is None or not coverage.valid or coverage.session_date != day:
        return _Day(
            day, "COVERAGE_UNVERIFIED", None, (), cost.report_sha256,
            ("missing_or_invalid_signed_session_coverage",)
            + (coverage.reasons if coverage else ()),
        )
    # Incomplete outcomes are retained within the known denominator, but any
    # scope/feature conflict invalidates the entire session's denominator.
    benign = {"incomplete_route_cost_coverage", "no_complete_routes"}
    if set(cost.blockers) - benign:
        return _Day(day, "COVERAGE_CONFLICT", None, (), cost.report_sha256, cost.blockers)
    if coverage.expected_initial_routes != len(cost.routes):
        raise PaperCohortError("cohort_route_count_mismatch")
    comparisons = {
        item.call_id: item for item in compare_paper_no_chase(cost).comparisons
    }
    scores: list[_RouteScore] = []
    for route in cost.routes:
        if (
            route.initial_ts_utc is None or route.source_ts_utc is None
            or route.initial_spread_bps is None or route.initial_quote_age_ms is None
            or route.initial_signal_age_ms is None or not route.revision_id
        ):
            raise PaperCohortError("missing_point_in_time_regime_evidence")
        regimes = classify_execution_regimes(
            spread_bps=_score(route.initial_spread_bps),
            quote_age_ms=_score(route.initial_quote_age_ms),
            decision_latency_ms=_score(route.initial_signal_age_ms),
            decision_ts=route.initial_ts_utc, policy=_REGIMES,
        )
        primary = -_score(route.net_shortfall_bps) if route.net_shortfall_bps is not None else None
        pair = comparisons[route.call_id]
        # Under attested route closure, an absent chase is no intervention; zero
        # is structural, not an imputation of an unknown order or unknown profit.
        chase = (
            -_score(pair.observed_minus_no_chase_bps)
            if pair.observed_minus_no_chase_bps is not None else
            0.0 if route.complete and not route.chase_order_observed else None
        )
        scores.append(_RouteScore(
            route.call_id, route.ticker, regimes, primary, chase,
            route.chase_order_observed and pair.observed_minus_no_chase_bps is not None,
        ))
    status = "COVERED_EMPTY" if not scores else (
        "COVERED_COMPLETE" if all(s.primary is not None and s.chase is not None for s in scores)
        else "COVERED_CENSORED"
    )
    return _Day(day, status, len(scores), tuple(scores), cost.report_sha256, cost.blockers)


def _observed_values(day: _Day, endpoint: str) -> list[float]:
    return [
        value for row in day.scores
        if (value := row.primary if endpoint == "primary" else row.chase) is not None
    ]


def _bounds(day: _Day, endpoint: str, clip: float) -> tuple[float, float]:
    if day.expected is None:
        return -clip, clip
    if day.expected == 0:
        return 0.0, 0.0
    values = _observed_values(day, endpoint)
    total = math.fsum(max(-clip, min(clip, value)) for value in values)
    missing = day.expected - len(values)
    # Roundoff at the exact support boundary must not invalidate the CS input.
    return (
        max(-clip, min(clip, (total - missing * clip) / day.expected)),
        max(-clip, min(clip, (total + missing * clip) / day.expected)),
    )


def _raw_day_mean(day: _Day, endpoint: str) -> float | None:
    if day.expected is None:
        return None
    if day.expected == 0:
        return 0.0
    values = _observed_values(day, endpoint)
    return statistics.fmean(values) if len(values) == day.expected else None


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _tail(values: Sequence[float]) -> float | None:
    return _mean(sorted(values)[:max(1, math.ceil(len(values) * 0.10))])


def _concentration(
    days: Sequence[_Day],
) -> tuple[float | None, float | None, float | None, float | None]:
    counts = [len(_observed_values(day, "primary")) for day in days]
    total = sum(counts)
    if not total:
        return None, None, None, None
    ticker_counts: Counter[str] = Counter()
    abs_scores: dict[str, float] = defaultdict(float)
    for day in days:
        for row in day.scores:
            if row.primary is not None:
                ticker_counts[row.ticker] += 1
                abs_scores[row.ticker] += abs(row.primary)
    abs_total = math.fsum(abs_scores.values())
    return (
        total * total / sum(n * n for n in counts),
        max(counts) / total, max(ticker_counts.values()) / total,
        max(abs_scores.values()) / abs_total if abs_total else None,
    )


def _endpoint(
    days: Sequence[_Day], policy: PaperCohortPolicy, endpoint: str,
    common_blockers: tuple[str, ...], chase_count: int,
) -> PaperEndpointStatistics:
    values = [value for day in days for value in _observed_values(day, endpoint)]
    known = sum(day.expected or 0 for day in days)
    intervals = tuple(_bounds(day, endpoint, policy.clip_bps) for day in days)
    alpha = policy.family_alpha / _FAMILY_ENDPOINTS
    confidence = anytime_bounded_mean_confidence_sequence(
        intervals, lower=-policy.clip_bps, upper=policy.clip_bps, alpha=alpha,
    )
    raw_means = [
        value for day in days if (value := _raw_day_mean(day, endpoint)) is not None
    ]
    contiguous = len(raw_means) == len(days)
    diagnostics = session_mean_diagnostics(
        raw_means, block_days=policy.block_days, trials=policy.bootstrap_trials,
        alpha=alpha, random_seed=policy.random_seed, contiguous=contiguous,
    )
    sensitivity: list[tuple[int, float | None, float | None]] = []
    for block in sorted({1, policy.block_days, min(30, policy.block_days * 2)}):
        estimate = session_mean_diagnostics(
            raw_means, block_days=block, trials=policy.bootstrap_trials,
            alpha=alpha, random_seed=policy.random_seed, contiguous=contiguous,
        )
        sensitivity.append((block, estimate.bootstrap_lower, estimate.bootstrap_upper))
    threshold = (
        policy.minimum_primary_score_bps if endpoint == "primary"
        else policy.minimum_chase_score_bps
    )
    blockers = list(common_blockers)
    if not known or len(values) / known < policy.minimum_complete_fraction:
        blockers.append("insufficient_known_route_coverage")
    if endpoint == "chase" and chase_count < policy.minimum_observed_chases:
        blockers.append("insufficient_observed_chase_interventions")
    clipped = sum(abs(value) > policy.clip_bps for value in values)
    if clipped:
        blockers.append("unclipped_tail_exceeds_preregistered_support")
    tail = _tail(values)
    if tail is not None and tail < -policy.maximum_tail_loss_bps:
        blockers.append("raw_route_tail_loss_exceeds_limit")
    if (
        diagnostics.leave_one_out_min is None
        or diagnostics.leave_one_out_min <= threshold
    ):
        blockers.append("leave_one_session_out_not_robust")
    if confidence.lower_bound <= threshold:
        blockers.append("anytime_lower_bound_not_above_threshold")
    # A bound can be informative with incomplete days, but it must not be called
    # a full cohort performance result or a release authorization.
    status = "BOUNDED_RESEARCH_SUPPORT" if not blockers else "NOT_QUALIFIED"
    identified = (
        (confidence.interval_mean_lower, confidence.interval_mean_upper)
        if confidence.interval_mean_lower is not None and confidence.interval_mean_upper is not None
        else None
    )
    return PaperEndpointStatistics(
        endpoint, len(values), known, clipped, _mean(values), min(values) if values else None,
        tail, diagnostics, tuple(sensitivity), confidence, identified, threshold, status,
        tuple(dict.fromkeys(blockers)),
    )


def _regimes(
    days: Sequence[_Day], policy: PaperCohortPolicy, endpoint: str,
) -> tuple[PaperRegimeAxis, ...]:
    results: list[PaperRegimeAxis] = []
    all_covered = all(day.expected is not None for day in days)
    for axis in _AXES:
        expected: Counter[str] = Counter()
        grouped: dict[str, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))
        for day in days:
            for row in day.scores:
                bucket = dict(row.regimes)[axis]
                expected[bucket] += 1
                value = row.primary if endpoint == "primary" else row.chase
                if value is not None:
                    grouped[bucket][day.date].append(value)
        day_means = {
            bucket: tuple(statistics.fmean(v) for _, v in sorted(by_day.items()))
            for bucket, by_day in sorted(grouped.items())
        }
        pooled = evaluate_numeric_group_shrinkage(
            day_means, policy=HierarchicalPolicy(
                minimum_total_samples=2 * policy.minimum_group_sessions,
                minimum_group_samples=policy.minimum_group_sessions,
            ),
        )
        estimates = {item.group: item.posterior_mean for item in pooled.estimates}
        groups: list[PaperRegimeGroup] = []
        mixture: list[tuple[str, float, float]] = []
        total = sum(expected.values())
        for bucket, n in sorted(expected.items()):
            by_day = grouped.get(bucket, {})
            values = [v for daily in by_day.values() for v in daily]
            route_mean = _mean(values)
            qualified = (
                bucket != "UNKNOWN" and len(by_day) >= policy.minimum_group_sessions
                and len(values) == n
            )
            groups.append(PaperRegimeGroup(
                axis, bucket, n, len(values), len(by_day), _mean(day_means.get(bucket, ())),
                route_mean, estimates.get(bucket), qualified,
            ))
            if qualified and route_mean is not None:
                mixture.append((bucket, n / total, route_mean))
        stress = None
        weights: tuple[tuple[str, float], ...] = ()
        if all_covered and len(mixture) >= 2 and len(mixture) == len(groups):
            stress, weights = bounded_mixture_stress(
                mixture, density_ratio_bound=policy.density_ratio_bound,
            )
        threshold = (
            policy.minimum_primary_score_bps if endpoint == "primary"
            else policy.minimum_chase_score_bps
        )
        status = "INSUFFICIENT" if stress is None else (
            "ADVERSE_DESCRIPTIVE_STRESS" if stress <= threshold else "DESCRIPTIVE_STRESS_OK"
        )
        results.append(PaperRegimeAxis(
            axis, tuple(groups), max(expected.values()) / total if total else None,
            stress, weights, status,
        ))
    return tuple(results)


def _evaluate_days(
    days: tuple[_Day, ...], policy: PaperCohortPolicy, policy_hash: str, as_of: datetime,
) -> PaperSessionCohortReport:
    if tuple(day.date for day in days) != policy.sessions[:len(days)]:
        raise PaperCohortError("session_prefix_mismatch")
    known = sum(day.expected or 0 for day in days)
    observed = sum(len(_observed_values(day, "primary")) for day in days)
    chases = sum(row.terminal_chase_observed for day in days for row in day.scores)
    covered = sum(day.expected is not None for day in days)
    complete = sum(day.status in ("COVERED_EMPTY", "COVERED_COMPLETE") for day in days)
    kish, day_share, ticker_share, abs_share = _concentration(days)
    common: list[str] = []
    if len(days) < policy.minimum_sessions:
        common.append("insufficient_elapsed_sessions")
    if known < policy.minimum_routes:
        common.append("insufficient_parent_routes")
    if not days or covered / len(days) < policy.minimum_complete_fraction:
        common.append("insufficient_verified_session_coverage")
    if not days or complete / len(days) < policy.minimum_complete_fraction:
        common.append("insufficient_complete_session_coverage")
    if kish is None or kish < policy.minimum_effective_sessions:
        common.append("insufficient_kish_session_diversity")
    if day_share is None or day_share > policy.maximum_session_share:
        common.append("session_concentration")
    if ticker_share is None or ticker_share > policy.maximum_ticker_share:
        common.append("ticker_concentration")
    if abs_share is not None and abs_share > policy.maximum_ticker_share:
        common.append("economic_score_ticker_concentration")
    primary = _endpoint(days, policy, "primary", tuple(common), chases)
    chase = _endpoint(days, policy, "chase", tuple(common), chases)
    primary_regimes = _regimes(days, policy, "primary")
    chase_regimes = _regimes(days, policy, "chase")
    regime_statuses = {axis.status for axis in primary_regimes + chase_regimes}
    robustness = (
        "ADVERSE_DESCRIPTIVE_STRESS" if "ADVERSE_DESCRIPTIVE_STRESS" in regime_statuses
        else "INSUFFICIENT" if "INSUFFICIENT" in regime_statuses else "DESCRIPTIVE_STRESS_OK"
    )
    session_rows = tuple(PaperSessionStatistic(
        day.date, day.status, day.expected,
        len(_observed_values(day, "primary")), len(_observed_values(day, "chase")),
        sum(row.terminal_chase_observed for row in day.scores),
        _bounds(day, "primary", policy.clip_bps), _bounds(day, "chase", policy.clip_bps),
        _raw_day_mean(day, "primary"), _raw_day_mean(day, "chase"), day.report_hash, day.reasons,
    ) for day in days)
    material = {
        "version": "paper-session-statistics-v1", "policy_sha256": policy_hash,
        "regime_policy": _REGIMES,
        "as_of": as_of, "sessions": session_rows, "primary": primary, "chase": chase,
        "primary_regimes": primary_regimes, "chase_regimes": chase_regimes,
        "concentration": (kish, day_share, ticker_share, abs_share),
    }
    return PaperSessionCohortReport(
        policy_hash, policy.family_id, policy.trust_sha256, policy.cost_policy_sha256, as_of,
        len(policy.sessions), len(days), len(policy.sessions) - len(days), covered, complete,
        known, observed, chases, len(days) - covered, observed / known if known else None,
        kish, day_share, ticker_share, abs_share, session_rows, primary, chase,
        primary_regimes, chase_regimes, robustness, stable_hash(material),
    )


def evaluate_paper_session_cohort(
    *, cohort_path: Path, expected_cohort_sha256: str, inputs_path: Path,
    trust_path: Path, cost_policy_path: Path, as_of: datetime,
) -> PaperSessionCohortReport:
    """Verify original signed inputs per elapsed exchange session, then analyze.

    No caller-supplied numeric report or claim of source completeness is trusted
    as an alternative to Stage 2 admission. A declared but invalid input is not
    converted to an empty day. Cross-session replays refuse the entire cohort.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise PaperCohortError("naive_cohort_cutoff")
    as_of = as_of.astimezone(UTC)
    policy = _load_cohort(_bounded_bytes(cohort_path, 65536), expected_cohort_sha256)
    trust = _parse_trust(_bounded_bytes(trust_path, 65536), policy.trust_sha256)
    costs = _load_cost_policy(
        _bounded_bytes(cost_policy_path, 65536), policy.cost_policy_sha256, policy.trust_sha256,
    )
    if max(trust.registered, costs.registered_ts_utc) > policy.registered_ts_utc:
        raise PaperCohortError("trust_or_cost_policy_registered_after_cohort")
    if as_of < policy.registered_ts_utc:
        raise PaperCohortError("cutoff_before_cohort_registration")
    elapsed = tuple(
        day for day in policy.sessions if (hours := session_hours(day)) is not None
        and hours[1] <= as_of
    )
    inputs = _input_rows(inputs_path, elapsed)
    seen_artifacts: set[str] = set()
    seen_calls: set[str] = set()
    seen_revisions: set[str] = set()
    seen_orders: set[str] = set()
    seen_fills: set[str] = set()
    days: list[_Day] = []
    route_count = 0

    def claim(value: str, seen: set[str], reason: str) -> None:
        if not value or value in seen:
            raise PaperCohortError(reason)
        seen.add(value)

    for day in elapsed:
        row = inputs.get(day)
        if row is None:
            days.append(_Day(day, "MISSING_SESSION", None, (), None, ("no_session_input",)))
            continue
        try:
            admission = PaperOutcomeAdmission(
                row["journal"], trust_path, policy.trust_sha256,
                row["outcome_export"], row["source_artifact"],
                _read_signature(row["collector_signature"]),
                _read_signature(row["reviewer_signature"]), as_of,
            )
            cost = build_paper_execution_cost_report(
                admission, cost_policy_path=cost_policy_path,
                expected_policy_sha256=policy.cost_policy_sha256,
                cost_export_path=row["cost_export"], market_artifact_path=row["market_artifact"],
                collector_signature=_read_signature(row["cost_collector_signature"]),
                reviewer_signature=_read_signature(row["cost_reviewer_signature"]),
            )
        except (OSError, ValueError, JournalError, ImportError, CalendarNotCovered) as exc:
            days.append(_Day(day, "INVALID_SESSION", None, (), None, (type(exc).__name__,)))
            continue
        if cost.source_id != trust.source_id or cost.account_ref != trust.account_ref:
            raise PaperCohortError("cohort_account_scope_conflict")
        for identity in (
            cost.journal_sha256, cost.outcome_export_sha256, cost.cost_export_sha256,
            cost.source_artifact_sha256, cost.market_artifact_sha256,
        ):
            claim(identity, seen_artifacts, "cross_session_artifact_reuse")
        for route in cost.routes:
            claim(route.call_id, seen_calls, "cross_session_call_reuse")
            claim(route.revision_id, seen_revisions, "cross_session_signal_revision_reuse")
        for order in cost.orders:
            if order.venue_order_id is not None:
                claim(order.venue_order_id, seen_orders, "cross_session_venue_order_reuse")
            for fill_id in order.venue_fill_ids:
                claim(fill_id, seen_fills, "cross_session_venue_fill_reuse")
        route_count += len(cost.routes)
        if route_count > _MAX_ROUTES:
            raise PaperCohortError("cohort_route_bound")
        days.append(_as_day(day, cost))
    return _evaluate_days(tuple(days), policy, expected_cohort_sha256, as_of)
