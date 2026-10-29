"""Kill switch. Fail-closed, checked before every buy.

Two independent stops, because they protect against different things:
  * an operator file (presence = stop). Deleting it is the fastest way a human
    can stand the robot down without touching a process.
  * an in-memory halt raised by a Discord STOP/stand-down post (engine.halted).

Anything that makes the file's state unknowable (permissions, a bad path) is
treated as STOPPED - an unreadable kill switch is not a green light.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path


class KillSwitch:
    def __init__(self, path: str | os.PathLike[str] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        # live/NO_TRADE mirrors data/NO_TRADE in scorpion-stock-finder: one
        # convention for "stop" across both desks.
        env = os.environ.get("MFF_NO_TRADE", "")
        self.path = Path(path or env or "live/NO_TRADE")
        self.clock = clock
        self._last_check: tuple[float, bool] | None = None

    def active(self, max_age_s: float = 0.0) -> bool:
        """True when trading is blocked.

        `max_age_s` caches the answer briefly: the live loop calls this on every
        signal and a stat() per message is wasted work, but a stale file check
        is a safety hole. Default 0.0 = always stat.
        """
        now = self.clock()
        if max_age_s > 0 and self._last_check is not None:
            checked_at, value = self._last_check
            if now - checked_at <= max_age_s:
                return value
        try:
            value = self.path.exists()
        except OSError:
            value = True                     # unknowable -> fail closed
        self._last_check = (now, value)
        return value

    def blocked(self) -> bool:
        """Engine hook: engine.blocked = switch.blocked."""
        return self.active(max_age_s=1.0)

    def stand_down(self, note: str = "operator") -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"stand-down {note} at {self.clock():.3f}\n", encoding="utf-8")
        self._last_check = None
        return self.path

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self._last_check = None

    def status(self) -> str:
        try:
            present = self.path.exists()
        except OSError as exc:
            return f"UNREADABLE ({exc}) -> treated as STOPPED"
        if present:
            try:
                detail = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                detail = "(unreadable)"
            return f"STOPPED ({detail})"
        return "ARMED (no file)"
