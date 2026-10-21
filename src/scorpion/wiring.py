"""The seam: turning measured variants into the posture `arm` actually reads.

`experts.py` blends rules and nothing consumes it. That is the same gap that made six
attribution variants measure zero before the policy was threaded through, and it fails silently: the
code
looks finished, the tests pass, and the running system reads a number a human typed months ago.

This module is one path:

    variant trade rows -> align -> blend -> guard -> posture -> live/weights.json -> read at arm

The file is deliberately self-describing. It carries the policy fingerprint it was computed
under, the number of trades it was computed from, and the drift state at the moment of writing,
because a weights file without its provenance cannot be distinguished from a stale one that parses.

Reading it back refuses on four conditions, and each has its own code:
    missing            no file, so there is no posture
    stale              older than the allowed age
    fingerprint        computed under a different policy than the one running
    schema             written by a version this code does not understand
    drifted            the outcomes had left the corpus and the last allocation was withdrawn

A refusal here is correct behaviour. Falling back to a default size on a stale file is how a desk
trades last month's posture without noticing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scorpion.drift import Detection, DriftError
from scorpion.drift import guard as drift_guard
from scorpion.experts import ExpertsError, from_variant_trades, posture

SCHEMA_VERSION = 1


class WiringError(ValueError):
    """A posture that cannot be built or trusted, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def build(
    trades_by_variant: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    budget: float,
    policy_fingerprint: str,
    as_of: str,
    detection: Detection | None = None,
    floor: float = 0.05,
    cap: float = 0.6,
) -> dict[str, Any]:
    """Blend the variants, guard the result, size it, and describe its own provenance."""
    if not policy_fingerprint:
        raise WiringError("no_fingerprint", "the policy that produced this must be named")
    if budget <= 0:
        raise WiringError("bad_budget", str(budget))
    try:
        blended = from_variant_trades(trades_by_variant)
    except ExpertsError as error:
        raise WiringError("blend_failed", f"{error.code}: {error.detail}") from error

    weights = {
        name: float(weight)
        for name, weight in zip(
            blended["names"], blended["final_weights"], strict=False
        )
    }
    sized = posture(weights, budget=budget, floor=floor, cap=cap)

    if detection is None:
        raise WiringError(
            "no_drift_state",
            "a posture without a drift verdict cannot be distinguished from one that ignored it",
        )
    gated = drift_guard(detection, sized["sizes"])

    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of,
        "policy_fingerprint": policy_fingerprint,
        "identity_count": int(blended["identity_count"]),
        "variant_count": int(blended["variant_count"]),
        "budget": round(budget, 2),
        "weights": {name: round(value, 6) for name, value in weights.items()},
        "sizes": gated["sizes"],
        "allocated": bool(gated["allocated"]),
        "drift": gated["detection"],
        "reason": gated["reason"],
        "concentration": sized["concentration"],
        "best_variant": blended["best_expert"],
        "regret": blended["regret"],
        "within_bound": blended["within_bound"],
    }


def write(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Write atomically: a half-written posture is worse than none, because it parses."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(target)
    return dict(payload)


def read(path: Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        raise WiringError("missing", str(target))
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise WiringError("unreadable", f"{target}: {error}") from error
    if not isinstance(payload, dict):
        raise WiringError("unreadable", "top level is not an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise WiringError(
            "schema",
            f"file is schema {payload.get('schema_version')}, this code is {SCHEMA_VERSION}",
        )
    return payload


def load_for_arm(
    path: Path,
    *,
    policy_fingerprint: str,
    age_seconds: float,
    max_age_seconds: float,
) -> dict[str, Any]:
    """The read `arm` performs. Every refusal names the reason it cannot be trusted."""
    payload = read(path)
    if payload.get("policy_fingerprint") != policy_fingerprint:
        raise WiringError(
            "fingerprint",
            f"posture was built under {payload.get('policy_fingerprint')!r}, "
            f"running {policy_fingerprint!r}",
        )
    if age_seconds < 0:
        raise WiringError("bad_age", str(age_seconds))
    if age_seconds > max_age_seconds:
        raise WiringError(
            "stale",
            f"posture is {age_seconds:.0f}s old, limit {max_age_seconds:.0f}s",
        )
    if not payload.get("allocated", False):
        raise WiringError(
            "drifted", str(payload.get("reason", "allocation withdrawn"))
        )
    if not payload.get("weights"):
        raise WiringError("empty", "no weights recorded")
    return payload


def sizes_for_arm(
    path: Path, *, policy_fingerprint: str, age_seconds: float, max_age_seconds: float,
) -> dict[str, float]:
    """Just the sizes, for a caller that has already decided to trust the file."""
    payload = load_for_arm(
        path,
        policy_fingerprint=policy_fingerprint,
        age_seconds=age_seconds,
        max_age_seconds=max_age_seconds,
    )
    sizes = payload.get("sizes", {})
    if not isinstance(sizes, dict):
        raise WiringError("empty", "sizes is not an object")
    return {str(name): float(value) for name, value in sizes.items()}


def compare(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """What changed between two postures - the column that shows the system is alive."""
    before = previous.get("weights", {})
    after = current.get("weights", {})
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise WiringError("unreadable", "weights must be objects")
    common = sorted(set(before) & set(after))
    drift = {name: round(float(after[name]) - float(before[name]), 4) for name in common}
    moved = sum(abs(value) for value in drift.values()) / 2.0
    return {
        "as_of": current.get("as_of"),
        "previous_as_of": previous.get("as_of"),
        "total_variation": round(moved, 4),
        "largest_move": max(drift, key=lambda k: abs(drift[k])) if drift else None,
        "drift": drift,
        "posture_changed": moved > 0.01,
        "allocated": bool(current.get("allocated", False)),
    }


def render(payload: Mapping[str, Any]) -> str:
    lines = [
        f"posture as of {payload.get('as_of')} under policy "
        f"{str(payload.get('policy_fingerprint'))[:12]}",
        f"  {payload.get('variant_count')} variants over {payload.get('identity_count')} "
        f"shared trades; best {payload.get('best_variant')}",
    ]
    for name, size in sorted(payload.get("sizes", {}).items()):
        lines.append(f"  {name:22s} {size:8.2f}")
    if not payload.get("allocated", False):
        lines.append(f"  allocation withdrawn: {payload.get('reason')}")
    return "\n".join(lines)


__all__ = [
    "SCHEMA_VERSION",
    "WiringError",
    "build",
    "compare",
    "load_for_arm",
    "read",
    "render",
    "sizes_for_arm",
    "write",
    "DriftError",
]
