from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from .active_learning import ReviewCandidate, ReviewPriority, prioritize


@dataclass(frozen=True, slots=True)
class ReviewWorkItem:
    candidate: ReviewCandidate
    rule_id: str
    contract_key: str | None
    channel_id: str
    age_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class AllocatedReview:
    event_id: str
    base_priority: ReviewPriority
    information_score: float
    diversity_penalty: float
    final_score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewAllocation:
    selected: tuple[AllocatedReview, ...]
    deferred_event_ids: tuple[str, ...]
    budget: int


def allocate_review_budget(
    items: Sequence[ReviewWorkItem],
    *,
    budget: int,
    aging_half_life_seconds: float = 300.0,
    duplicate_rule_penalty: float = 8.0,
    duplicate_contract_penalty: float = 10.0,
    duplicate_channel_penalty: float = 2.0,
) -> ReviewAllocation:
    """Greedy diversity-aware allocation of scarce human review capacity.

    Independent uncertainty ranking tends to spend a review budget on near-duplicate examples.
    This allocator keeps the original active-learning priority, adds a bounded aging bonus, and
    subtracts penalties for repeatedly selecting the same parser rule, contract, or channel.
    The result is a simple submodular-style approximation that favors marginal information gain.
    """
    if budget < 0:
        raise ValueError("budget cannot be negative")
    if aging_half_life_seconds <= 0:
        raise ValueError("aging_half_life_seconds must be positive")
    if min(duplicate_rule_penalty, duplicate_contract_penalty, duplicate_channel_penalty) < 0:
        raise ValueError("diversity penalties cannot be negative")
    if budget == 0 or not items:
        return ReviewAllocation((), tuple(item.candidate.event_id for item in items), budget)

    remaining = list(items)
    selected: list[AllocatedReview] = []
    rule_counts: Counter[str] = Counter()
    contract_counts: Counter[str] = Counter()
    channel_counts: Counter[str] = Counter()

    while remaining and len(selected) < budget:
        best_index = 0
        best_allocation: AllocatedReview | None = None
        for index, item in enumerate(remaining):
            base = prioritize(item.candidate)
            age = max(0.0, item.age_seconds)
            aging_bonus = min(15.0, 15.0 * age / (age + aging_half_life_seconds))
            penalty = rule_counts[item.rule_id] * duplicate_rule_penalty
            if item.contract_key:
                penalty += contract_counts[item.contract_key] * duplicate_contract_penalty
            penalty += channel_counts[item.channel_id] * duplicate_channel_penalty
            information = min(100.0, base.score + aging_bonus)
            final = max(0.0, information - penalty)
            reasons = list(base.reasons)
            if aging_bonus >= 5.0:
                reasons.append("aging_priority")
            if penalty > 0:
                reasons.append("diversity_penalty")
            allocation = AllocatedReview(
                event_id=item.candidate.event_id,
                base_priority=base,
                information_score=information,
                diversity_penalty=penalty,
                final_score=final,
                reasons=tuple(dict.fromkeys(reasons)),
            )
            if best_allocation is None or (final, item.candidate.event_id) > (
                best_allocation.final_score,
                best_allocation.event_id,
            ):
                best_index = index
                best_allocation = allocation
        if best_allocation is None:
            break
        chosen = remaining.pop(best_index)
        selected.append(best_allocation)
        rule_counts[chosen.rule_id] += 1
        if chosen.contract_key:
            contract_counts[chosen.contract_key] += 1
        channel_counts[chosen.channel_id] += 1

    selected_ids = {item.event_id for item in selected}
    deferred = tuple(
        item.candidate.event_id for item in items if item.candidate.event_id not in selected_ids
    )
    return ReviewAllocation(tuple(selected), deferred, budget)
