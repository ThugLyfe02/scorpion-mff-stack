from scorpion.novelty import NoveltyIndex, jensen_shannon_token_drift


def test_novelty_scores_nearby_wording_lower_than_unrelated_text():
    index = NoveltyIndex(["QQQ 719C TODAY @ 1.01", "Closing runners 19%"])
    nearby = index.score("QQQ 719C TODAY @ 1.02")
    unrelated = index.score("macroeconomic commentary with no option signal")
    assert nearby.novelty_score < unrelated.novelty_score
    assert nearby.nearest_index is not None


def test_token_drift_is_zero_for_identical_corpora():
    corpus = ["QQQ 719C TODAY @ 1.01", "Closing runners 19%"]
    assert jensen_shannon_token_drift(corpus, corpus) == 0.0


def test_token_drift_detects_style_shift():
    baseline = ["QQQ 719C TODAY @ 1.01"] * 5
    current = ["new slang totally different wording"] * 5
    assert jensen_shannon_token_drift(baseline, current) > 0.5
