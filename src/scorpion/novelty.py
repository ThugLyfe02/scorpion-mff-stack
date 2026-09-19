from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from .parser import normalize_text

_TOKEN_RE = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)?|[%@$+\-]")


@dataclass(frozen=True, slots=True)
class NoveltyMatch:
    novelty_score: float
    nearest_similarity: float
    nearest_index: int | None


def _features(text: str) -> frozenset[str]:
    normalized = normalize_text(text).lower()
    words = {f"w:{token}" for token in _TOKEN_RE.findall(normalized)}
    compact = f"  {normalized}  "
    trigrams = {f"c:{compact[index:index + 3]}" for index in range(max(0, len(compact) - 2))}
    return frozenset(words | trigrams)


def jaccard_similarity(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 1.0
    return len(left & right) / len(union)


class NoveltyIndex:
    def __init__(self, exemplars: Sequence[str]) -> None:
        self._features = tuple(_features(text) for text in exemplars)

    def score(self, text: str) -> NoveltyMatch:
        candidate = _features(text)
        if not self._features:
            return NoveltyMatch(1.0, 0.0, None)
        similarities = tuple(jaccard_similarity(candidate, known) for known in self._features)
        nearest_index = max(range(len(similarities)), key=similarities.__getitem__)
        nearest = similarities[nearest_index]
        return NoveltyMatch(1.0 - nearest, nearest, nearest_index)


def _token_counts(texts: Sequence[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for text in texts:
        counts.update(_TOKEN_RE.findall(normalize_text(text).lower()))
    return counts


def jensen_shannon_token_drift(baseline: Sequence[str], current: Sequence[str]) -> float:
    left = _token_counts(baseline)
    right = _token_counts(current)
    left_total = sum(left.values())
    right_total = sum(right.values())
    if left_total == 0 and right_total == 0:
        return 0.0
    vocabulary = set(left) | set(right)

    def probability(counts: Counter[str], total: int, token: str) -> float:
        return counts[token] / total if total else 0.0

    divergence = 0.0
    for token in vocabulary:
        p = probability(left, left_total, token)
        q = probability(right, right_total, token)
        midpoint = (p + q) / 2.0
        if p > 0.0:
            divergence += 0.5 * p * math.log2(p / midpoint)
        if q > 0.0:
            divergence += 0.5 * q * math.log2(q / midpoint)
    return min(1.0, max(0.0, divergence))
