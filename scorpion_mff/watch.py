"""Is the hone alive, and does a human need to know?

`watch --once` is meant to be run from Task Scheduler / cron every minute or two. It
reads the machine state the hot path already writes - the heartbeat file and the fill
log - decides whether something is wrong, and pages the operator only when it is.

The failure this exists for is silence. A listener that dies at 09:31, or a book that
fills up with unexitable positions, produces exactly zero activity: no fills, no
errors, nothing to notice. The log knows; nobody is reading it.

Checks, in the order they matter:

1. **No heartbeat file** - the listener has not run here at all -> DOWN.
2. **Heartbeat older than the limit** - it ran, then stopped -> STALE (wedged, killed,
   or the machine slept).
3. **Alerts today** - the engine raised something it refused to act on
   (`ambiguous_exit`, `expiry_unparsed`, `halted`) -> ALERTING.
4. Otherwise OK: nothing is sent, the exit code is 0, and the run costs one stat.

Exit codes: 0 healthy, 1 a problem was found and paged (or would have been, in a dry
run), 2 the watchdog itself could not do its job. Never 0 when nothing was checked.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .page import PageResult, TelegramPager

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HEARTBEAT = Path(os.environ.get("MFF_HEARTBEAT", ROOT / "live" / "heartbeat.json"))
DEFAULT_FILLS = Path(os.environ.get("MFF_FILLS", ROOT / "live" / "fills.jsonl"))
DEFAULT_KILL = Path(os.environ.get("MFF_NO_TRADE", ROOT / "live" / "NO_TRADE"))
DEFAULT_STALE_AFTER_S = 120.0

ALERT_EVENTS = frozenset({"alert"})


@dataclass
class Health:
    status: str                       # "ok" | "alerting" | "stale" | "down"
    heartbeat_age_s: float | None = None
    beats: int = 0
    heartbeat_note: str = ""
    fills_today: int = 0
    alerts_today: int = 0
    last_alert: str = ""
    kill_switch: bool = False
    problems: tuple[str, ...] = ()
    checked_at: float = 0.0

    @property
    def healthy(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {"status": self.status, "heartbeat_age_s": self.heartbeat_age_s,
                "beats": self.beats, "fills_today": self.fills_today,
                "alerts_today": self.alerts_today, "last_alert": self.last_alert,
                "kill_switch": self.kill_switch, "problems": list(self.problems),
                "checked_at": self.checked_at}

    def page_text(self) -> str:
        if self.heartbeat_age_s is None:
            age = "never recorded"
        else:
            age = f"{self.heartbeat_age_s / 60:.1f} min old"
        head = {
            "down": "SCORPION MFF — DOWN: no heartbeat",
            "stale": "SCORPION MFF — STALE: listener has gone quiet",
            "alerting": "SCORPION MFF — needs a human: engine raised an alert",
            "ok": "SCORPION MFF — healthy",
        }[self.status]
        lines = [
            head,
            "",
            f"heartbeat : {age}  ({self.beats} beats)",
            f"fills today: {self.fills_today}   alerts today: {self.alerts_today}",
        ]
        if self.last_alert:
            lines.append(f"last alert : {self.last_alert}")
        lines.append(f"kill switch: {'ARMED (blocking)' if self.kill_switch else 'not set'}")
        if self.problems:
            lines += ["", "why:"] + [f"  - {p}" for p in self.problems]
        lines += ["", "Read-only alert — nothing was traded. Check live/fills.jsonl."]
        return "\n".join(lines)


def _read_heartbeat(path: Path) -> tuple[float | None, int, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, 0, ""
    ts = raw.get("ts")
    return (float(ts) if isinstance(ts, (int, float)) else None,
            int(raw.get("beats") or 0), str(raw.get("note") or ""))


def _read_fills_today(path: Path, now: float) -> tuple[int, int, str]:
    """(fills, alerts, last alert reason) for the current UTC day."""
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    fills = alerts = 0
    last = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0, 0, ""
    for line in lines[-5000:]:                  # bounded: a watchdog may not hang
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = row.get("ts")
        if not isinstance(ts, (int, float)) or time.strftime("%Y-%m-%d", time.gmtime(ts)) != day:
            continue
        event = str(row.get("event") or "")
        if event in ALERT_EVENTS:
            alerts += 1
            last = f"{row.get('reason', '')} ({time.strftime('%H:%M', time.gmtime(ts))} UTC)"
        elif event in {"buy", "sell"}:
            fills += 1
    return fills, alerts, last


def assess(*, now: float | None = None, heartbeat_path: Path | str = DEFAULT_HEARTBEAT,
           fills_path: Path | str = DEFAULT_FILLS, kill_path: Path | str = DEFAULT_KILL,
           stale_after_s: float = DEFAULT_STALE_AFTER_S) -> Health:
    now = time.time() if now is None else now
    hb_path = Path(heartbeat_path)
    kill = Path(kill_path).exists()

    ts, beats, note = _read_heartbeat(hb_path)
    fills, alerts, last = _read_fills_today(Path(fills_path), now)

    health = Health(status="ok", heartbeat_age_s=None, beats=beats, heartbeat_note=note,
                    fills_today=fills, alerts_today=alerts, last_alert=last,
                    kill_switch=kill, checked_at=now)

    if ts is None:
        health.status = "down"
        health.problems = (f"no heartbeat at {hb_path} — the listener has not run here "
                           f"(or cannot write its heartbeat)",)
        return health

    age = max(0.0, now - ts)
    health.heartbeat_age_s = age
    if age > stale_after_s:
        health.status = "stale"
        health.problems = (f"heartbeat is {age / 60:.1f} min old, limit "
                           f"{stale_after_s / 60:.1f} min — the listener stopped, wedged, "
                           f"or the machine slept",)
        return health

    if alerts:
        health.status = "alerting"
        health.problems = (f"{alerts} engine alert(s) today, last: {last} — the engine "
                           f"refused to act on something, which means an exit or an entry "
                           f"is stuck",)
        return health

    if kill:
        health.problems = ("kill switch is ARMED (live/NO_TRADE present) — intentional "
                           "stand-down, not a fault",)
    return health


def run_watch(*, pager: TelegramPager | None = None, dry_run: bool = False,
              stale_after_s: float = DEFAULT_STALE_AFTER_S, now: float | None = None,
              heartbeat_path: Path | str = DEFAULT_HEARTBEAT,
              fills_path: Path | str = DEFAULT_FILLS,
              kill_path: Path | str = DEFAULT_KILL) -> tuple[Health, PageResult | None]:
    """Assess, page if needed, and return both. A healthy run pages nothing."""
    health = assess(now=now, heartbeat_path=heartbeat_path, fills_path=fills_path,
                    kill_path=kill_path, stale_after_s=stale_after_s)
    if health.healthy:
        return health, None
    pager = pager if pager is not None else TelegramPager.from_env()
    result = pager.send(health.page_text(), dry_run=dry_run)
    return health, result
