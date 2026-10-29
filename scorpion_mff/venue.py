"""The venue's positions feed: evidence, stamped, and refused when it is neither.

`reconcile.py` compares two books. This module is the *other* book's source: a snapshot
the venue actually published, with the time it was published. That time is the whole
point - a positions file from twenty minutes ago is not evidence about now, and deriving
the venue side from our own fill log would compare the book with itself and always pass.

Two adapters, one contract:

| Feed | Where it reads | Designed for |
|---|---|---|
| `FileVenueFeed` | an operator export or a fixture | recorded evidence, tests |
| `HttpVenueFeed` | `GET` a template you supply (`MFF_VENUE_URL`) | the endpoint |

Neither invents a vendor schema. `HttpVenueFeed` reads the list of positions out of a
dotted path you name (`MFF_VENUE_POSITIONS_PATH`, default `results`) and validates every
row through `reconcile.snapshot_from_payload`, which accepts `contract_key`/`key` and
`contracts`/`qty` and refuses duplicates, negatives, booleans and missing keys. An
unconfigured feed raises; it never returns an empty book, because "no positions" and "no
feed" are opposite facts and the flat one would silently stop every exit.

Accepted payload shapes (file or HTTP body):

```json
[{"contract_key": "QQQ|CALL|720|0DTE", "contracts": 3}]

{"as_of": 1757340000.0,
 "positions": [{"contract_key": "QQQ|CALL|720|0DTE", "contracts": 3}]}
```

`as_of` (or `taken_at`) is the venue's own timestamp, epoch seconds or ISO-8601. When it
is absent, a file's modification time is used - the honest statement "this is when the
bytes were written" - and an HTTP response is stamped with the read time.

The failure taxonomy matters more than the parsing:

* **Unavailable** (`VenueError`): the feed could not be read, answered 5xx, or has no
  positions path. That is *no evidence*. It pages, and it must never halt buys - halting
  on a broken adapter would turn an ops bug into a stopped desk.
* **Malformed** (`ReconcileError`): the feed answered with something that is not a book.
  Also no halt, for the same reason, but it is a different page ("fix the adapter").
* **Stale** (`VenueError`): a snapshot older than the limit. Refused, because an old book
  silently compared against a current one produces drift that no longer exists or misses
  drift that does.
* **Drift** (a clean `ReconcileReport`): both books are readable and they disagree. That
  is the only case that halts new buys, and `Engine.reconcile_positions` owns it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .reconcile import ReconcileError, VenuePosition, snapshot_from_payload

SCHEMA = "mff.venue-snapshot.v1"
DEFAULT_MAX_AGE_S = 120.0

_POSITION_FIELDS = ("positions", "results", "holdings")
_TIME_FIELDS = ("taken_at", "as_of", "timestamp", "time")


class VenueError(RuntimeError):
    """The feed could not produce a usable snapshot. Never a drift halt by itself."""


def _timestamp(value: Any) -> float | None:
    """Epoch seconds from an epoch number or an ISO-8601 string. None when unreadable.

    A vendor that sends `"as_of": "2026-09-08T14:00:00Z"` and one that sends
    `1757340000` mean the same thing; a vendor that sends `"soon"` means nothing, and
    `None` (rather than a guess) is what lets the staleness check refuse.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None
    return None


def _dig(payload: Any, dotted: str) -> Any:
    """Read 'results.0.quantity' out of nested dicts/lists. None when absent.

    Same dotted convention as `HttpQuoteProvider` (`broker.py`), kept local so this
    module does not depend on a private helper of another one.
    """
    current = payload
    for part in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
        if current is None:
            return None
    return current


@dataclass(frozen=True)
class VenueSnapshot:
    """A venue book and the instant it describes. `taken_at=None` means 'unknown'."""

    positions: tuple[VenuePosition, ...]
    taken_at: float | None
    source: str
    schema: str = SCHEMA

    @property
    def contracts(self) -> int:
        return sum(item.contracts for item in self.positions)

    def age_s(self, now: float | None = None) -> float | None:
        if self.taken_at is None:
            return None
        moment = time.time() if now is None else now
        return max(0.0, moment - self.taken_at)

    def stale(self, now: float | None = None,
              max_age_s: float | None = DEFAULT_MAX_AGE_S) -> bool:
        """True when the snapshot is provably too old. Unknown age is NOT stale.

        Unknown age is refused separately (in `check_age`), because "we cannot date this
        book" and "this book is old" are different operator problems.
        """
        if max_age_s is None:
            return False
        age = self.age_s(now)
        return age is not None and age > max_age_s

    def check_age(self, now: float | None = None,
                  max_age_s: float | None = DEFAULT_MAX_AGE_S) -> None:
        """Refuse an old or undatable snapshot. Called by the feeds, not by callers."""
        if max_age_s is None:
            return
        age = self.age_s(now)
        if age is None:
            raise VenueError(
                "venue snapshot has no timestamp and a maximum age is set: "
                "an undatable book is not evidence")
        if age > max_age_s:
            raise VenueError(
                f"venue snapshot is {age:.0f}s old (limit {max_age_s:.0f}s): "
                f"re-read the venue before comparing books")

    def payload(self) -> list[dict[str, Any]]:
        """The bare array `reconcile --snapshot` and `ReconcileReport` already speak."""
        return [item.to_dict() for item in self.positions]

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "source": self.source,
                "taken_at": self.taken_at, "contracts": self.contracts,
                "positions": self.payload()}


class VenueFeed(Protocol):
    """Anything that can produce a stamped, validated venue book."""

    @property
    def configured(self) -> bool: ...

    @property
    def source(self) -> str: ...

    def snapshot(self, now: float | None = None) -> VenueSnapshot: ...


def _positions_from_payload(payload: Any) -> tuple[VenuePosition, ...]:
    """Find the position list in either accepted shape, then validate it.

    A payload with no recognisable list is refused with the keys it did have, because
    "your path is wrong" and "the venue has no positions" must not be the same message.

    Duplicate contract keys are refused *here*, at the boundary, not left to the
    comparator: two answers to one question are malformed evidence, and a feed that
    hands them to a live tick would raise inside the hot path instead of paging.
    """
    if isinstance(payload, list):
        return _validated(payload)
    if isinstance(payload, dict):
        for name in _POSITION_FIELDS:
            if name in payload:
                rows = payload[name]
                if not isinstance(rows, list):
                    raise ReconcileError(
                        f"venue snapshot {name!r} must be a JSON array, "
                        f"got {type(rows).__name__}")
                return _validated(rows)
        raise ReconcileError(
            f"venue snapshot has no position list (looked for "
            f"{', '.join(_POSITION_FIELDS)}; found {', '.join(sorted(payload)) or 'nothing'})")
    raise ReconcileError(
        f"venue snapshot must be an array or an object, got {type(payload).__name__}")


def _validated(rows: list[Any]) -> tuple[VenuePosition, ...]:
    positions = snapshot_from_payload(rows)
    seen: set[str] = set()
    for item in positions:
        if item.contract_key in seen:
            raise ReconcileError(
                f"venue snapshot lists {item.contract_key!r} twice: "
                f"two answers to one question is not a book")
        seen.add(item.contract_key)
    return tuple(positions)


def _time_from_payload(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    for name in _TIME_FIELDS:
        if name in payload:
            stamp = _timestamp(payload[name])
            if stamp is not None:
                return stamp
    return None


@dataclass
class FileVenueFeed:
    """A venue book from a JSON file. Missing/unreadable is an error, never 'flat'.

    `max_age_s` is None by default so a recorded fixture from last week can still be
    replayed by hand; the live path arms it (`arm --venue-snapshot ... --venue-every`).
    When a maximum age is set and the payload carries no timestamp, the file's mtime is
    the timestamp - it is the only honest one available, and it is what makes a stale
    operator export refuse itself instead of silently halting the desk.
    """

    path: Path | None = None
    max_age_s: float | None = None
    label: str = ""
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)

    @property
    def configured(self) -> bool:
        return self.path is not None

    @property
    def source(self) -> str:
        return self.label or f"file:{self.path}"

    def snapshot(self, now: float | None = None) -> VenueSnapshot:
        if self.path is None:
            raise VenueError("no venue snapshot file configured")
        moment = self.clock() if now is None else now
        try:
            raw: object = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise VenueError(f"venue snapshot not found: {self.path}") from None
        except OSError as exc:
            raise VenueError(f"cannot read venue snapshot {self.path}: {exc}") from None
        except json.JSONDecodeError as exc:
            raise ReconcileError(f"venue snapshot {self.path} is not JSON: {exc}") from None
        positions = _positions_from_payload(raw)
        taken_at = _time_from_payload(raw)
        if taken_at is None:
            try:
                taken_at = self.path.stat().st_mtime
            except OSError:                       # pragma: no cover - stat after read
                taken_at = None
        snapshot = VenueSnapshot(positions=positions, taken_at=taken_at,
                                 source=self.source)
        snapshot.check_age(moment, self.max_age_s)
        return snapshot


@dataclass
class HttpVenueFeed:
    """Positions from a URL template YOU supply. No vendor schema is invented here.

        MFF_VENUE_URL=https://.../positions?account={account}
        MFF_VENUE_POSITIONS_PATH=results
        MFF_VENUE_TIME_PATH=as_of

    A non-2xx answer, a transport failure, a missing positions path or an unreadable
    body is `VenueError`: no evidence. The bearer token is read from `MFF_RH_TOKEN` (the
    same credential the order adapter uses) and never appears in an error string.
    """

    template: str = field(default_factory=lambda: os.environ.get("MFF_VENUE_URL", ""))
    positions_path: str = field(
        default_factory=lambda: os.environ.get("MFF_VENUE_POSITIONS_PATH", ""))
    time_path: str = field(default_factory=lambda: os.environ.get("MFF_VENUE_TIME_PATH", ""))
    token: str = field(default_factory=lambda: os.environ.get("MFF_RH_TOKEN", ""))
    account: str = field(default_factory=lambda: os.environ.get("MFF_RH_ACCOUNT", ""))
    timeout_s: float = 5.0
    max_age_s: float | None = DEFAULT_MAX_AGE_S
    transport: Callable[[urllib.request.Request, float], tuple[int, dict[str, Any]]] | None = None
    clock: Callable[[], float] = time.time

    @property
    def configured(self) -> bool:
        return bool(self.template)

    @property
    def source(self) -> str:
        """Host only: a URL can carry a token in its query and a log must not."""
        try:
            return f"http:{urllib.parse.urlsplit(self.template).netloc or 'unparsed'}"
        except ValueError:                        # pragma: no cover - malformed template
            return "http:unparsed"

    def snapshot(self, now: float | None = None) -> VenueSnapshot:
        if not self.template:
            raise VenueError("no venue positions feed configured (set MFF_VENUE_URL)")
        moment = self.clock() if now is None else now
        url = self.template.replace("{account}", urllib.parse.quote(self.account))
        request = urllib.request.Request(url, headers={
            "Accept": "application/json",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        status, body = self._send(request)
        if status == 0:
            raise VenueError("venue positions feed unreachable (transport error)")
        if not (200 <= status < 300):
            raise VenueError(f"venue positions feed HTTP {status}")
        payload: Any = body
        if self.positions_path:
            payload = _dig(body, self.positions_path)
            if payload is None:
                raise VenueError(
                    f"venue positions path {self.positions_path!r} is not in the response")
        positions = _positions_from_payload(payload)
        taken_at = _time_from_payload(body)
        if taken_at is None and self.time_path:
            taken_at = _timestamp(_dig(body, self.time_path))
        if taken_at is None:
            # An HTTP answer describes now: the read instant is the only stamp available.
            taken_at = moment
        snapshot = VenueSnapshot(positions=positions, taken_at=taken_at, source=self.source)
        snapshot.check_age(moment, self.max_age_s)
        return snapshot

    def _send(self, request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
        if self.transport is not None:
            return self.transport(request, self.timeout_s)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read().decode("utf-8", "replace")
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            return exc.code, {}
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0, {}


def feed_from_env(*, max_age_s: float | None = None) -> VenueFeed | None:
    """The feed the environment configures, or None. A file wins over a URL.

    `arm` calls this so the live process reconciles on a schedule without a code change
    the day the positions endpoint is confirmed. Neither variable set means drift
    detection is OFF, and `arm` says so out loud rather than implying it is on.
    """
    snapshot_path = os.environ.get("MFF_VENUE_SNAPSHOT", "").strip()
    if snapshot_path:
        return FileVenueFeed(path=Path(snapshot_path),
                             max_age_s=DEFAULT_MAX_AGE_S if max_age_s is None else max_age_s)
    url = os.environ.get("MFF_VENUE_URL", "").strip()
    if url:
        return HttpVenueFeed(max_age_s=DEFAULT_MAX_AGE_S if max_age_s is None else max_age_s)
    return None
