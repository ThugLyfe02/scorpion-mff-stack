"""Drill: replay an arbitrary day in isolation and prove it agrees with the whole log.

`verify` proves the book equals its log from the beginning. It does not prove that a *slice* of the
log is self-consistent, and that is the property an operator needs at 3pm on a Tuesday: if I replay
only what happened since the open, do I get the book that is actually on screen?

Two failure modes this catches, neither visible to a whole-log replay:

* a row whose meaning depends on rows before the slice (an expiry release with no visible entry, a
  fill against a position opened yesterday) - the slice either raises or produces a different book;
* an ordering dependency where batch application and incremental application disagree, which is the
  signature of a reducer that is not a pure fold.

The drill is deterministic given a seed, so a failing day can be replayed by name rather than by
luck, and it reports the day and the diverging field rather than a boolean.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .store import Store
from .verify import replay_applied


class DrillError(ValueError):
    """An invalid drill request."""


@dataclass(frozen=True, slots=True)
class DayResult:
    day: date
    rows: int
    consistent: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(), "rows": self.rows,
            "consistent": self.consistent, "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class DrillReport:
    results: tuple[DayResult, ...]
    seed: int

    @property
    def ok(self) -> bool:
        return all(r.consistent for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "seed": self.seed, "days": len(self.results),
            "consistent": sum(1 for r in self.results if r.consistent),
            "results": [r.to_dict() for r in self.results],
        }


def days_in_log(store: Store) -> list[date]:
    """Every calendar day the applied log touches, from the row timestamps."""
    days: set[date] = set()
    for row in store.load_applied():
        raw = str(row.get("ts_utc") or "")
        if not raw:
            continue
        try:
            days.add(datetime.fromisoformat(raw).date())
        except ValueError:
            continue
    return sorted(days)


def pick(store: Store, *, count: int = 5, seed: int = 0) -> list[date]:
    """Deterministically sample days. A seed makes a failure reproducible by name."""
    available = days_in_log(store)
    if not available:
        raise DrillError("empty_log", "no days to drill")
    rng = random.Random(seed)
    count = min(count, len(available))
    return sorted(rng.sample(available, count))


def _rows_for_day(rows: Sequence[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        raw = str(row.get("ts_utc") or "")
        try:
            if datetime.fromisoformat(raw).date() == day:
                out.append(row)
        except ValueError:
            continue
    return out


def drill_day(
    store: Store, day: date, *, rows: Sequence[dict[str, Any]] | None = None
) -> DayResult:
    """Replay the day's rows after the prefix, then compare with a batch replay of everything.

    The slice is applied INCREMENTALLY on top of the prefix, which is what a restart does. A batch
    replay of the whole log is the reference. Agreement means the reducer is a pure fold and the day
    can be reconstructed on its own.
    """
    all_rows = list(rows if rows is not None else store.load_applied())
    if not all_rows:
        return DayResult(day, 0, True, "empty log")

    todays = _rows_for_day(all_rows, day)
    if not todays:
        return DayResult(day, 0, True, "no rows for this day")

    first_seq = int(str(todays[0]["seq"]))
    prefix = [row for row in all_rows if int(str(row["seq"])) < first_seq]
    suffix = [row for row in all_rows if int(str(row["seq"])) >= first_seq]

    state = replay_applied(prefix)
    for row in suffix:
        state = _apply(state, row)
    reference = replay_applied(all_rows)

    if state != reference:
        differing = _first_difference(state, reference)
        return DayResult(day, len(todays), False, differing)
    return DayResult(day, len(todays), True, "")


def _apply(state: Any, row: dict[str, Any]) -> Any:
    from .verify import apply_applied

    # `replay_applied` pre-scans the log for the policy row and skips it; a per-row application has
    # to do the same or it raises on a kind that carries configuration rather than a decision.
    if str(row["kind"]) == "policy":
        return state
    return apply_applied(state, row)


def _first_difference(a: Any, b: Any) -> str:
    """Name the first field that differs, rather than reporting inequality."""
    if a.halted != b.halted:
        return f"halted {a.halted} vs {b.halted}"
    for key in sorted(set(a.positions) | set(b.positions)):
        left, right = a.positions.get(key), b.positions.get(key)
        if left is None or right is None:
            return f"position {key} present={left is not None} vs {right is not None}"
        for field in ("status", "quantity", "generation"):
            if getattr(left, field) != getattr(right, field):
                return f"{key}.{field} {getattr(left, field)} vs {getattr(right, field)}"
    for field in ("market_day", "realized_today", "equity", "peak_equity"):
        if getattr(a.risk, field) != getattr(b.risk, field):
            return f"risk.{field} {getattr(a.risk, field)} vs {getattr(b.risk, field)}"
    return "states differ with no named field"


def run(store: Store, *, count: int = 5, seed: int = 0) -> DrillReport:
    chosen = pick(store, count=count, seed=seed)
    rows = store.load_applied()
    return DrillReport(results=tuple(drill_day(store, day, rows=rows) for day in chosen), seed=seed)


def render(report: DrillReport) -> str:
    lines = [f"seed {report.seed}  {len(report.results)} days  "
             f"{'ALL CONSISTENT' if report.ok else 'DIVERGENCE'}"]
    for result in report.results:
        mark = "ok  " if result.consistent else "FAIL"
        suffix = f"  {result.detail}" if result.detail else ""
        lines.append(f"  [{mark}] {result.day.isoformat()}  {result.rows} rows{suffix}")
    return "\n".join(lines)


def fingerprint(store: Store) -> str:
    """A digest of the applied log, so two drills can be compared without diffing rows."""
    payload = json.dumps(
        [(str(r["seq"]), str(r["kind"]), str(r["contract_key"]), str(r["payload_json"]))
         for r in store.load_applied()],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
