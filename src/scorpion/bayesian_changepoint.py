from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class ChangePointStatus(StrEnum):
    STABLE = "STABLE"
    WATCH = "WATCH"
    DEGRADATION_CHANGEPOINT = "DEGRADATION_CHANGEPOINT"
    IMPROVEMENT_CHANGEPOINT = "IMPROVEMENT_CHANGEPOINT"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class BayesianChangePointPolicy:
    window_size: int = 200
    minimum_segment: int = 20
    prior_alpha: float = 1.0
    prior_beta: float = 1.0
    changepoint_prior_probability: float = 0.05
    watch_posterior_probability: float = 0.50
    changepoint_posterior_probability: float = 0.90
    minimum_rate_change: float = 0.05

    def __post_init__(self) -> None:
        if self.window_size < 2 * self.minimum_segment:
            raise ValueError("window_size must support two minimum segments")
        if self.minimum_segment <= 1:
            raise ValueError("minimum_segment must be >1")
        if self.prior_alpha <= 0 or self.prior_beta <= 0:
            raise ValueError("Beta prior parameters must be positive")
        for name in (
            "changepoint_prior_probability",
            "watch_posterior_probability",
            "changepoint_posterior_probability",
        ):
            value = getattr(self, name)
            if not 0 < value < 1:
                raise ValueError(f"{name} must be in (0,1)")
        if self.watch_posterior_probability > self.changepoint_posterior_probability:
            raise ValueError("watch threshold cannot exceed changepoint threshold")
        if not 0 <= self.minimum_rate_change <= 1:
            raise ValueError("minimum_rate_change must be in [0,1]")


@dataclass(frozen=True, slots=True)
class BayesianChangePointReport:
    samples: int
    failures: int
    overall_failure_rate: float
    posterior_changepoint_probability: float
    log_bayes_factor: float
    best_split_index: int | None
    pre_failure_rate: float
    post_failure_rate: float
    rate_change: float
    candidate_splits: int
    status: ChangePointStatus

    @property
    def degradation(self) -> bool:
        return self.status is ChangePointStatus.DEGRADATION_CHANGEPOINT


def _log_beta(alpha: float, beta: float) -> float:
    return math.lgamma(alpha) + math.lgamma(beta) - math.lgamma(alpha + beta)


def _log_marginal(successes: int, failures: int, *, alpha: float, beta: float) -> float:
    return _log_beta(alpha + successes, beta + failures) - _log_beta(alpha, beta)


def _logsumexp(values: Sequence[float]) -> float:
    if not values:
        return -math.inf
    maximum = max(values)
    return maximum + math.log(sum(math.exp(value - maximum) for value in values))


def evaluate_bayesian_changepoint(
    failure_events: Sequence[bool],
    *,
    policy: BayesianChangePointPolicy | None = None,
) -> BayesianChangePointReport:
    """Detect an abrupt shift in a binary failure stream with a Bayesian split mixture.

    The null model assumes one stationary Bernoulli rate over the bounded rolling window. The
    changepoint model places a uniform prior over every split that leaves ``minimum_segment``
    observations on both sides, then integrates each segment's Bernoulli rate with a Beta prior.
    This avoids selecting the most flattering split without paying for split uncertainty.
    """
    policy = policy or BayesianChangePointPolicy()
    rows = tuple(bool(item) for item in failure_events[-policy.window_size :])
    samples = len(rows)
    total_failures = sum(rows)
    overall_rate = total_failures / samples if samples else 0.0
    if samples < 2 * policy.minimum_segment:
        return BayesianChangePointReport(
            samples=samples,
            failures=total_failures,
            overall_failure_rate=overall_rate,
            posterior_changepoint_probability=0.0,
            log_bayes_factor=0.0,
            best_split_index=None,
            pre_failure_rate=0.0,
            post_failure_rate=0.0,
            rate_change=0.0,
            candidate_splits=0,
            status=ChangePointStatus.INSUFFICIENT,
        )

    prefix_failures = [0]
    for item in rows:
        prefix_failures.append(prefix_failures[-1] + int(item))
    alpha = policy.prior_alpha
    beta = policy.prior_beta
    null_log_likelihood = _log_marginal(
        samples - total_failures,
        total_failures,
        alpha=alpha,
        beta=beta,
    )

    split_logs: list[tuple[int, float, float, float]] = []
    for split in range(policy.minimum_segment, samples - policy.minimum_segment + 1):
        pre_n = split
        post_n = samples - split
        pre_failures = prefix_failures[split]
        post_failures = total_failures - pre_failures
        pre_successes = pre_n - pre_failures
        post_successes = post_n - post_failures
        log_likelihood = _log_marginal(
            pre_successes,
            pre_failures,
            alpha=alpha,
            beta=beta,
        ) + _log_marginal(
            post_successes,
            post_failures,
            alpha=alpha,
            beta=beta,
        )
        pre_rate = pre_failures / pre_n
        post_rate = post_failures / post_n
        split_logs.append((split, log_likelihood, pre_rate, post_rate))

    mixture_log_likelihood = _logsumexp([item[1] for item in split_logs]) - math.log(
        len(split_logs)
    )
    log_bayes_factor = mixture_log_likelihood - null_log_likelihood
    prior = policy.changepoint_prior_probability
    log_prior_odds = math.log(prior) - math.log1p(-prior)
    log_posterior_odds = log_prior_odds + log_bayes_factor
    if log_posterior_odds >= 0:
        posterior = 1.0 / (1.0 + math.exp(-log_posterior_odds))
    else:
        exp_value = math.exp(log_posterior_odds)
        posterior = exp_value / (1.0 + exp_value)

    best_split, _, pre_rate, post_rate = max(split_logs, key=lambda item: item[1])
    rate_change = post_rate - pre_rate
    meaningful = abs(rate_change) >= policy.minimum_rate_change
    if posterior >= policy.changepoint_posterior_probability and meaningful:
        status = (
            ChangePointStatus.DEGRADATION_CHANGEPOINT
            if rate_change > 0
            else ChangePointStatus.IMPROVEMENT_CHANGEPOINT
        )
    elif posterior >= policy.watch_posterior_probability:
        status = ChangePointStatus.WATCH
    else:
        status = ChangePointStatus.STABLE

    return BayesianChangePointReport(
        samples=samples,
        failures=total_failures,
        overall_failure_rate=overall_rate,
        posterior_changepoint_probability=posterior,
        log_bayes_factor=log_bayes_factor,
        best_split_index=best_split,
        pre_failure_rate=pre_rate,
        post_failure_rate=post_rate,
        rate_change=rate_change,
        candidate_splits=len(split_logs),
        status=status,
    )
