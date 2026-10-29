from scorpion.active_learning import ReviewCandidate, ReviewTier, rank_review_candidates


def test_high_information_candidate_ranks_first():
    low = ReviewCandidate("low", 0.99, 0.99, 0.05, 0.01)
    high = ReviewCandidate(
        "high",
        0.55,
        0.50,
        0.90,
        0.80,
        actionable_disagreement=True,
        unseen_rule=True,
        contract_review=True,
    )
    ranked = rank_review_candidates([low, high])
    assert ranked[0].event_id == "high"
    assert ranked[0].tier is ReviewTier.URGENT
    assert "actionable_model_disagreement" in ranked[0].reasons
