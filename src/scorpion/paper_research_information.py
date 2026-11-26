"""Bounded prior-sensitive value of additional adjudicated research observations.

This is a declared beta-binomial decision model, not a guarantee about dependent
market sessions, realized information gain or economic trading returns.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_PRIORS = ((1, 1), (2, 2), (1, 3), (3, 1))


@dataclass(frozen=True, slots=True)
class ExperimentInformationValue:
    endpoint: str
    prospective_observations: int
    prior_completion_scenarios: int
    minimum_expected_value: float
    maximum_expected_value: float
    conservative_normalized_value: float
    assumption: str = "BETA_BINOMIAL_CONDITIONALLY_IID_DECISION_MODEL_NOT_EMPIRICAL_GAIN"


def beta_experiment_value(a: int, b: int, n: int, threshold: float) -> float:
    """Exact finite-outcome predictive value of information.

    A hypothetical research retain/retire decision has utility threshold-p
    or zero, where p is an adverse-event probability. The experiment observes
    n conditionally independent Bernoulli events. Enumerate all beta-binomial
    outcomes and optimize the research decision after each outcome.
    """
    if not 1 <= a <= 256 or not 1 <= b <= 256 or not 1 <= n <= 20:
        raise ValueError("beta experiment count bound")
    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError("invalid research decision threshold")
    log_beta = math.lgamma(a)+math.lgamma(b)-math.lgamma(a+b)
    log_weights = [
        math.log(math.comb(n, k)) + math.lgamma(a+k) + math.lgamma(b+n-k)
        - math.lgamma(a+b+n) - log_beta for k in range(n+1)
    ]
    shift = max(log_weights)
    weights = [math.exp(value-shift) for value in log_weights]
    total = math.fsum(weights)
    post = math.fsum(
        weight*max(0.0, threshold-(a+k)/(a+b+n))/total
        for k, weight in enumerate(weights)
    )
    pre = max(0.0, threshold-a/(a+b))
    return max(0.0, min(threshold*(1-threshold), post-pre))


def experiment_value_envelope(
    *, endpoint: str, sessions: int, confirmed_adverse: int, ambiguous: int,
    observations: int, threshold: float,
) -> ExperimentInformationValue:
    """Enumerate every count completion of missing indicators and fixed priors.

    The worst scenario is the conservative input to scheduling; the best is
    a sensitivity diagnostic. No future observation is generated or bought.
    """
    if (
        not endpoint or not 0 <= sessions <= 252
        or not 0 <= confirmed_adverse <= sessions
        or not 0 <= ambiguous <= sessions-confirmed_adverse
    ):
        raise ValueError("invalid research evidence counts")
    values = [
        beta_experiment_value(
            a+confirmed_adverse+missing_bad,
            b+sessions-confirmed_adverse-missing_bad, observations, threshold,
        )
        for a, b in _PRIORS for missing_bad in range(ambiguous+1)
    ]
    low, high = min(values), max(values)
    return ExperimentInformationValue(
        endpoint, observations, len(values), low, high,
        min(1.0, low/(threshold*(1-threshold))),
    )
