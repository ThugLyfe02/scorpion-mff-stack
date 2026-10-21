"""Operations: liveness, the metrics that explain a refusal, and the restart policy.

A control plane that works and a control plane that can be left alone are different objects. The
difference is this module: a way to ask "is it alive", a way to ask "why is it refusing", and a
policy that decides when to restart - and, more importantly, when NOT to.

Liveness is measured from the applied log rather than from a heartbeat file. A heartbeat is a
promise the process makes about itself; the log is evidence it did something. A process that
heartbeats while failing to apply anything is exactly the failure a heartbeat cannot see, so the
newest applied row is the signal and its age is the metric.

The restart policy is deliberately conservative in one specific direction: **a halted store is
never auto-restarted.** A halt is a human decision recorded as an event, and a supervisor that
restarts through it converts a safety stop into a loop. `plan()` returns `refuse_halted` and the
operator clears it with `scorpion resume`, which writes its own event.

Three exit codes, matching cron's needs rather than a library's taste:

    0  healthy
    1  a problem an operator must look at (stale log, unknown sends, halted)
    2  the supervisor itself could not run
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_STALE_AFTER = timedelta(minutes=30)


# ── metrics ─────────────────────────────────────────────────────────────────
@dataclass
class Metrics:
    """Counters and gauges with a stable text rendering.

    Deliberately not a Prometheus client: an extra dependency in the money path buys nothing that
    a dict and a renderer do not, and the export format is stable enough to scrape later.
    """

    counters: dict[str, float] = field(default_factory=dict)
    gauges: dict[str, float] = field(default_factory=dict)
    timings: dict[str, list[float]] = field(default_factory=dict)

    def inc(self, name: str, amount: float = 1.0, **labels: object) -> None:
        key = _key(name, labels)
        self.counters[key] = self.counters.get(key, 0.0) + amount

    def set(self, name: str, value: float, **labels: object) -> None:
        self.gauges[_key(name, labels)] = float(value)

    def observe(self, name: str, value: float, **labels: object) -> None:
        self.timings.setdefault(_key(name, labels), []).append(float(value))

    def percentile(self, name: str, p: float, **labels: object) -> float | None:
        values = sorted(self.timings.get(_key(name, labels), []))
        if not values:
            return None
        position = p * (len(values) - 1)
        low = int(position)
        high = min(low + 1, len(values) - 1)
        return values[low] + (values[high] - values[low]) * (position - low)

    def to_dict(self) -> dict[str, Any]:
        return {
            "counters": dict(sorted(self.counters.items())),
            "gauges": dict(sorted(self.gauges.items())),
            "timings": {
                key: {"n": len(values), "max": round(max(values), 3),
                      "mean": round(sum(values) / len(values), 3)}
                for key, values in sorted(self.timings.items())
            },
        }

    def render(self) -> str:
        lines: list[str] = []
        for key, value in sorted(self.counters.items()):
            lines.append(f"{key} {value:g}")
        for key, value in sorted(self.gauges.items()):
            lines.append(f"{key} {value:g}")
        return "\n".join(lines)


def _key(name: str, labels: dict[str, object]) -> str:
    if not labels:
        return name
    suffix = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
    return f"{name}{{{suffix}}}"


def collect(store: Any) -> Metrics:
    """Everything the store can say about itself, in one call. Read-only."""
    metrics = Metrics()
    for kind, count in store.applied_counts().items():
        metrics.set("applied", count, kind=kind)
    outbox = store.outbox_snapshot()
    for status, count in (outbox.get("counts") or {}).items():
        metrics.set("outbox", count, status=status)
    metrics.set("outbox_dead_letter", int(outbox.get("dead_letter") or 0))
    halted, reason = store.halt_state()
    metrics.set("halted", 1 if halted else 0)
    if halted:
        metrics.inc("halt_events")
    unknown = store.load_unknown()
    metrics.set("unknown_sends", len(unknown))
    for row in unknown:
        metrics.inc("unknown_by_contract", 1, contract=str(row.get("contract_key") or "?"))
    metrics.set("applied_rows", sum((store.applied_counts() or {}).values()))
    return metrics


# ── liveness and supervision ────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Liveness:
    last_applied_utc: datetime | None
    age: timedelta | None
    status: str                     # ok | stale | down
    rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_applied_utc": (
                self.last_applied_utc.isoformat() if self.last_applied_utc else None
            ),
            "age_seconds": int(self.age.total_seconds()) if self.age else None,
            "status": self.status,
            "rows": self.rows,
        }


def liveness(store: Any, *, now: datetime | None = None,
             stale_after: timedelta = DEFAULT_STALE_AFTER) -> Liveness:
    """Is the log advancing? An empty log is `down` - nothing has ever been applied."""
    moment = now or datetime.now(UTC)
    rows = store.load_applied()
    if not rows:
        return Liveness(None, None, "down", 0)
    last = datetime.fromisoformat(str(rows[-1]["ts_utc"]))
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    age = moment - last
    return Liveness(last, age, "ok" if age <= stale_after else "stale", len(rows))


@dataclass(frozen=True, slots=True)
class Assessment:
    liveness: Liveness
    halted: bool
    halt_reason: str
    unknown_sends: int
    problems: tuple[str, ...] = ()
    exit_code: int = 0

    @property
    def healthy(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "liveness": self.liveness.to_dict(),
            "halted": self.halted, "halt_reason": self.halt_reason,
            "unknown_sends": self.unknown_sends,
            "problems": list(self.problems), "exit_code": self.exit_code,
        }


def assess(store: Any, *, now: datetime | None = None,
           stale_after: timedelta = DEFAULT_STALE_AFTER) -> Assessment:
    """One call an operator (or cron) can read, with an exit code that means something."""
    live = liveness(store, now=now, stale_after=stale_after)
    halted, reason = store.halt_state()
    unknown = len(store.load_unknown())

    problems: list[str] = []
    if live.status == "down":
        problems.append("log_empty: nothing has ever been applied")
    elif live.status == "stale":
        age_s = int((live.age or timedelta()).total_seconds())
        problems.append(f"log_stale: last applied {age_s}s ago")
    if halted:
        problems.append(f"halted: {reason or 'no reason recorded'}")
    if unknown:
        problems.append(f"unknown_sends: {unknown} awaiting reconciliation")

    return Assessment(
        liveness=live, halted=halted, halt_reason=reason, unknown_sends=unknown,
        problems=tuple(problems), exit_code=1 if problems else 0,
    )


@dataclass(frozen=True, slots=True)
class RestartPlan:
    action: str                     # none | restart | refuse_halted | refuse_unknown | refuse_empty
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "detail": self.detail}


def plan(store: Any, *, now: datetime | None = None,
         stale_after: timedelta = DEFAULT_STALE_AFTER) -> RestartPlan:
    """Whether a supervisor should restart, and the two refusals that matter.

    Order matters. A halted desk is refused before staleness is considered: restarting a halted
    store is the one action that can undo a deliberate stop, so it is checked first and reported
    as its own outcome rather than folded into "unhealthy".
    """
    state = assess(store, now=now, stale_after=stale_after)
    if state.halted:
        return RestartPlan("refuse_halted", state.halt_reason or "halt recorded without a reason")
    if state.unknown_sends:
        return RestartPlan(
            "refuse_unknown",
            f"{state.unknown_sends} sends of unknown fate; reconcile before resuming",
        )
    if state.liveness.status == "down":
        return RestartPlan("refuse_empty", "no applied events; nothing to resume from")
    if state.liveness.status == "stale":
        idle = int((state.liveness.age or timedelta()).total_seconds())
        return RestartPlan("restart", f"log idle {idle}s")
    return RestartPlan("none", "healthy")


def report(store: Any, *, now: datetime | None = None,
           stale_after: timedelta = DEFAULT_STALE_AFTER) -> dict[str, Any]:
    metrics = collect(store)
    state = assess(store, now=now, stale_after=stale_after)
    return {
        "assessment": state.to_dict(),
        "restart": plan(store, now=now, stale_after=stale_after).to_dict(),
        "metrics": metrics.to_dict(),
    }


def render(payload: dict[str, Any]) -> str:
    a = payload["assessment"]
    lines = [
        f"liveness   {a['liveness']['status']:5s}  rows {a['liveness']['rows']}"
        f"  age {a['liveness']['age_seconds']}s",
        f"halted     {a['halted']}  {a['halt_reason']}",
        f"unknown    {a['unknown_sends']}",
        f"restart    {payload['restart']['action']}  {payload['restart']['detail']}",
        f"exit       {a['exit_code']}",
    ]
    if a["problems"]:
        lines.append("problems   " + "; ".join(a["problems"]))
    return "\n".join(lines)


def to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def summarise(metrics: Metrics, keys: Iterable[str]) -> dict[str, float]:
    return {k: metrics.gauges[k] for k in keys if k in metrics.gauges}
