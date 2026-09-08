from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class ReviewTier(StrEnum):
    URGENT = "URGENT"
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    event_id: str
    parser_confidence: float
    association_confidence: float
    novelty_score: float
    ensemble_entropy: float
    actionable_disagreement: bool = False
    unseen_rule: bool = False
    contract_review: bool = False
    quote_review: bool = False


@dataclass(frozen=True, slots=True)
class ReviewPriority:
    event_id: str
    score: float
    tier: ReviewTier
    reasons: tuple[str, ...]


def prioritize(candidate: ReviewCandidate) -> ReviewPriority:
    values = (
        candidate.parser_confidence,
        candidate.association_confidence,
        candidate.novelty_score,
        candidate.ensemble_entropy,
    )
    if any(not 0.0 <= value <= 1.0 for value in values):
        raise ValueError("confidence/novelty/entropy values must be between 0 and 1")

    score = 0.0
    reasons: list[str] = []
    score += (1.0 - candidate.parser_confidence) * 20.0
    score += (1.0 - candidate.association_confidence) * 15.0
    score += candidate.novelty_score * 20.0
    score += candidate.ensemble_entropy * 15.0
    if candidate.actionable_disagreement:
        score += 20.0
        reasons.append("actionable_model_disagreement")
    if candidate.unseen_rule:
        score += 5.0
        reasons.append("uncalibrated_rule")
    if candidate.contract_review:
        score += 10.0
        reasons.append("contract_semantics")
    if candidate.quote_review:
        score += 10.0
        reasons.append("quote_semantics")
    if candidate.novelty_score >= 0.70:
        reasons.append("novel_wording")
    if candidate.parser_confidence < 0.75:
        reasons.append("low_parser_confidence")
    if candidate.association_confidence < 0.75:
        reasons.append("weak_association")
    if candidate.ensemble_entropy >= 0.30:
        reasons.append("ensemble_uncertainty")

    score = min(100.0, score)
    if score >= 70.0:
        tier = ReviewTier.URGENT
    elif score >= 45.0:
        tier = ReviewTier.HIGH
    elif score >= 20.0:
        tier = ReviewTier.NORMAL
    else:
        tier = ReviewTier.LOW
    return ReviewPriority(candidate.event_id, score, tier, tuple(reasons))


def rank_review_candidates(candidates: Sequence[ReviewCandidate]) -> tuple[ReviewPriority, ...]:
    priorities = [prioritize(candidate) for candidate in candidates]
    return tuple(sorted(priorities, key=lambda item: (-item.score, item.event_id)))
