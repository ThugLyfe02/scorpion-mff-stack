from __future__ import annotations

from dataclasses import dataclass, field

from .config import DEFAULT_POLICY, Policy
from .decision_packet import SelectiveReviewPolicy
from .eligibility import StrategyEligibilityPolicy
from .provenance import stable_hash


@dataclass(frozen=True, slots=True)
class RuntimePolicyBundle:
    """Versionable policy bundle consumed by deterministic runtime/replay surfaces.

    Keeping the three policy families together prevents a replay from silently using today's
    reducer limits with yesterday's strategy-eligibility or selective-review thresholds.
    """

    base: Policy = DEFAULT_POLICY
    eligibility: StrategyEligibilityPolicy = field(default_factory=StrategyEligibilityPolicy)
    selective_review: SelectiveReviewPolicy = field(default_factory=SelectiveReviewPolicy)
    policy_version: str = "runtime-policy-v1"

    @property
    def fingerprint(self) -> str:
        return stable_hash(
            {
                "policy_version": self.policy_version,
                "base": self.base,
                "eligibility": self.eligibility,
                "selective_review": self.selective_review,
            }
        )


DEFAULT_RUNTIME_POLICY = RuntimePolicyBundle()
