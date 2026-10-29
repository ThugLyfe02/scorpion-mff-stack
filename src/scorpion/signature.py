"""Path signatures: the shape of a move, as a finite feature vector.

Rough path theory (Lyons, 1998): the iterated integrals of a path form a *signature* that
characterises the path up to tree-like equivalence, truncated at level N to a finite feature
vector. The first level is net displacement; the second adds signed area (lead-lag); the third
adds curvature. Every feature used elsewhere in this branch - return, range, MFE, time in
trade - is a projection of the path that discards its shape. A signature keeps the shape.

The computation is exact for a piecewise-linear path. A segment with increment Δ contributes
the tensor exponential exp(Δ) = Σ Δ^{⊗k}/k!, and segments compose by the truncated
concatenation product, so the signature of the whole path is the product of the segment
signatures. No quadrature, no approximation, no random number - the level-1..3 values of a
hand-drawn square come out of a hand-checkable closed form, and the tests pin them.

The lead-lag transform turns a univariate series into the two-dimensional path whose signed
area is the lead-lag relationship between successive moves. That is the canonical
two-dimensional path for a price series, and it is the reason this module computes in two
dimensions by default.

The module also owns the honest gate: a signature is a *feature vector*, and fitting anything
on it is a statistical act. With the 107-trade corpus, a level-3 signature (14 non-constant
features) is refused with a named reason, because 14 features on 107 samples overfits
instantly. The gate is a function of the feature count, not of enthusiasm.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


class SignatureError(ValueError):
    """A signature question that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _words(dim: int, level: int) -> list[tuple[int, ...]]:
    """Every multi-index of length 0..level over an alphabet of size `dim`.

    The order is by length then lexicographic within a length, so index 0 is always the empty
    word (the level-0 constant) and the level-1 words are the coordinate displacements.
    """
    words: list[tuple[int, ...]] = [()]
    frontier: list[tuple[int, ...]] = [()]
    for _ in range(level):
        nxt = [word + (letter,) for word in frontier for letter in range(dim)]
        words.extend(nxt)
        frontier = nxt
    return words


def feature_count(dim: int, level: int) -> int:
    """Total signature features including the level-0 constant: Σ_{k=0..N} d^k."""
    if dim < 1:
        raise SignatureError("bad_dim", str(dim))
    if level < 0:
        raise SignatureError("bad_level", str(level))
    total = 0
    power = 1
    for _ in range(level + 1):
        total += power
        power *= dim
    return total


def nontrivial_feature_count(dim: int, level: int) -> int:
    """Features that carry information: the total minus the level-0 constant."""
    return feature_count(dim, level) - 1


def _tensor_exp(
    inc: Sequence[float],
    level: int,
    index: dict[tuple[int, ...], int],
) -> dict[tuple[int, ...], float]:
    """The truncated tensor exponential of one segment's increment.

    For a word w of length k with letters i1..ik, the value is (Δ_{i1} … Δ_{ik}) / k!.
    """
    out: dict[tuple[int, ...], float] = {}
    for word in index:
        if not word:
            out[word] = 1.0
            continue
        value = 1.0
        for letter in word:
            value *= inc[letter]
        out[word] = value / math.factorial(len(word))
    return out


def _concat(
    a: Sequence[float],
    b: dict[tuple[int, ...], float],
    index: dict[tuple[int, ...], int],
) -> list[float]:
    """The truncated concatenation product: (A B)_w = Σ_{u·v = w} A_u B_v."""
    result = [0.0] * len(a)
    for word, position in index.items():
        total = 0.0
        for split in range(len(word) + 1):
            prefix = word[:split]
            suffix = word[split:]
            total += a[index[prefix]] * b[suffix]
        result[position] = total
    return result


@dataclass(frozen=True, slots=True)
class Signature:
    """A truncated path signature, aligned with `_words(dim, level)`."""

    dim: int
    level: int
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.values) != feature_count(self.dim, self.level):
            raise SignatureError(
                "wrong_length",
                f"dim {self.dim} level {self.level} needs "
                f"{feature_count(self.dim, self.level)} values, got {len(self.values)}",
            )

    def get(self, word: Sequence[int]) -> float:
        return self.values[_words(self.dim, self.level).index(tuple(word))]

    def levy_area(self) -> float:
        """The Levy area of a two-dimensional path: (S_01 - S_10) / 2.

        For a piecewise-linear planar path this equals the signed area of the polygon formed
        by the path and the chord closing it - positive when travelled counter-clockwise.
        """
        if self.dim != 2:
            raise SignatureError("not_2d", f"dim {self.dim} has no signed area")
        return (self.get((0, 1)) - self.get((1, 0))) / 2.0


def signature(path: Sequence[Sequence[float]], *, level: int = 3) -> Signature:
    """The level-N signature of a piecewise-linear path given as its vertices."""
    if level < 1:
        raise SignatureError("bad_level", str(level))
    if not path:
        raise SignatureError("empty_path", "no vertices")
    points = [tuple(float(v) for v in vertex) for vertex in path]
    dim = len(points[0])
    if dim < 1:
        raise SignatureError("bad_dim", "a vertex must have at least one coordinate")
    if any(len(vertex) != dim for vertex in points):
        raise SignatureError("ragged_path", "all vertices must share one dimension")

    words = _words(dim, level)
    index = {word: position for position, word in enumerate(words)}
    values = [0.0] * len(words)
    values[0] = 1.0
    for left, right in zip(points, points[1:], strict=False):
        increment = [right[k] - left[k] for k in range(dim)]
        values = _concat(values, _tensor_exp(increment, level, index), index)
    return Signature(dim=dim, level=level, values=tuple(values))


def lead_lag(series: Sequence[float]) -> list[tuple[float, float]]:
    """The lead-lag path of a univariate series.

    For a series x_0..x_n the path is (x_0, x_0), (x_1, x_0), (x_1, x_1), (x_2, x_1), ... -
    the lead coordinate moves first, then the lag coordinate catches up, so successive moves
    are encoded as (Δ, 0) then (0, Δ). Its signed area is the lead-lag relationship between
    successive moves, which is invisible to any single-coordinate feature.
    """
    if len(series) < 2:
        raise SignatureError("thin_series", f"{len(series)} points")
    values = [float(v) for v in series]
    points: list[tuple[float, float]] = [(values[0], values[0])]
    for i in range(1, len(values)):
        previous, current = values[i - 1], values[i]
        points.append((current, previous))
        points.append((current, current))
    return points


def _word_label(word: tuple[int, ...]) -> str:
    return "".join(str(letter) for letter in word)


@dataclass(frozen=True, slots=True)
class FitGate:
    """Whether a fit on this signature would be a statistical act or a statistical error."""

    nontrivial_features: int
    minimum_samples: int
    provided_samples: int
    refused: bool
    reason: str


def minimum_samples(features: int) -> int:
    """A floor of 30 samples, plus ten per feature, before any fit is permitted."""
    if features < 0:
        raise SignatureError("bad_features", str(features))
    return max(30, 10 * features)


def gate_fit(provided_samples: int, *, dim: int, level: int) -> FitGate:
    features = nontrivial_feature_count(dim, level)
    minimum = minimum_samples(features)
    refused = provided_samples < minimum
    reason = (
        f"{features} non-constant signature features need >= {minimum} independent samples "
        f"before any fit; the corpus has {provided_samples}"
        if refused
        else f"{features} non-constant features on {provided_samples} samples: fit permitted"
    )
    return FitGate(
        nontrivial_features=features,
        minimum_samples=minimum,
        provided_samples=provided_samples,
        refused=refused,
        reason=reason,
    )


def report(
    path: Sequence[Sequence[float]],
    *,
    level: int = 3,
    samples: int = 107,
) -> dict[str, Any]:
    sig = signature(path, level=level)
    words = _words(sig.dim, sig.level)
    payload: dict[str, Any] = {
        "dim": sig.dim,
        "level": sig.level,
        "features": feature_count(sig.dim, sig.level),
        "nontrivial_features": nontrivial_feature_count(sig.dim, sig.level),
        "level1": {
            _word_label((letter,)): round(sig.values[words.index((letter,))], 6)
            for letter in range(sig.dim)
        },
        "values": [
            {"word": _word_label(word), "value": round(value, 6)}
            for word, value in zip(words, sig.values, strict=False)
            if word
        ],
    }
    if sig.dim == 2:
        payload["levy_area"] = round(sig.levy_area(), 6)
    gate = gate_fit(samples, dim=sig.dim, level=sig.level)
    payload["fit_gate"] = {
        "nontrivial_features": gate.nontrivial_features,
        "minimum_samples": gate.minimum_samples,
        "provided_samples": gate.provided_samples,
        "refused": gate.refused,
        "reason": gate.reason,
    }
    payload["caveats"] = [
        "level 1 is net displacement; only level >= 2 sees the shape of how a move arrived",
        "a signature identifies a path only up to tree-like equivalence: a spike and a smooth "
        "ramp with the same iterated integrals are indistinguishable at the chosen truncation",
        "the lead-lag transform must be applied to a 1-D series before any lead-lag reading "
        "is meaningful, and its signed area is the only level-2 term with that interpretation",
        "the fit gate is a floor, not a proof: a permitted fit still needs the deflation and "
        "probability-of-backtest-overfit machinery before a number leaves this module",
    ]
    return payload


def render(payload: dict[str, Any]) -> str:
    gate = payload["fit_gate"]
    lines = [
        f"signature dim={payload['dim']} level={payload['level']} "
        f"features={payload['features']} (non-constant {payload['nontrivial_features']})",
        "  level 1 (net displacement): "
        + ", ".join(f"{word}: {value:g}" for word, value in payload["level1"].items()),
    ]
    if "levy_area" in payload:
        lines.append(f"  levy area: {payload['levy_area']:g}")
    lines.append(
        f"  fit gate: {'REFUSED' if gate['refused'] else 'permitted'} - {gate['reason']}"
    )
    for caveat in payload["caveats"]:
        lines.append(f"  ! {caveat}")
    return "\n".join(lines)
