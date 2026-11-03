"""Buying power and cash, checked before an entry reaches the venue.

`risk.py` caps the premium the desk is *allowed* to risk. It knows nothing about the
account that has to pay for it. A two-clip day can therefore be sized inside every risk
cap and still be refused by the broker - after the signal is gone, after the parser and
the engine have already spent the decision. This module is the missing precheck: the
venue account's cash and buying power, as evidence with a timestamp, compared against the
exact debit of the order the desk is about to send.

Two adapters, one contract:

| Feed | Where it reads | Designed for |
|---|---|---|
| `FileAccountFeed` | an operator export or a fixture | recorded evidence, tests |
| `HttpAccountFeed` | `GET` a template you supply (`MFF_ACCOUNT_URL`) | the endpoint |
| `StaticAccountFeed` | a snapshot in memory | tests, offline replay |

Neither invents a vendor schema. `HttpAccountFeed` reads each number out of a dotted
path you name (`MFF_ACCOUNT_CASH_PATH` etc.) and converts it from the declared unit
(`MFF_ACCOUNT_UNITS`, dollars by default, or integer cents). The file adapter accepts
dollar fields (`cash`, `buying_power`, `equity`) or explicit cent fields
(`cash_cents`, ...), and refuses a value that is not exactly representable in cents
rather than rounding it - a cent is the atom of this module.

The failure taxonomy is the point:

* **Unavailable** (`no_account`): no feed configured, an unreadable file, a 5xx, a
  transport failure. No evidence; the entry is refused.
* **Malformed** (`malformed_account`): the feed answered with something that is not an
  account, a negative or boolean value, a sub-cent number, a missing field. Refused,
  never repaired.
* **Undatable** (`undatable_account`): no timestamp and no honest fallback (a file's
  mtime is one; an HTTP read instant is another). Refused when a maximum age is armed.
* **Stale** (`stale_account`): older than `max_age_seconds`. Refused. A cash balance
  from twenty minutes ago is not evidence about the buying power available now.
* **Underfunded** (`insufficient_cash`, `insufficient_buying_power`): readable, fresh
  evidence that says the order does not fit. Both limits are checked, because a broker
  can enforce either.
* **Undeclared fees** (`fee_model_missing`): the fee model is a *declared* config, not a
  guess. Without it the guard refuses the entry rather than understating the debit.

Money is integer **cents** everywhere inside this module. Dollars are converted at the
boundary with `Decimal(str(value))`, and a value with more than two decimal places is
refused. The one rounding choice made here is in `FeeModel.fees_for`: the fee is the
larger of the per-contract formula and the declared minimum, computed exactly in cents.

Exits are never blocked. A sell that reduces risk must never wait for an account feed:
`check`/`precheck` return an admitted decision for `sell` before looking at any evidence,
and the docstrings say so at the point the bypass happens.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .types import OrderIntent

SCHEMA = "mff.account-snapshot.v1"
FEE_SCHEMA = "mff.fee-model.v1"
DEFAULT_MAX_AGE_SECONDS = 120.0
OPTION_MULTIPLIER = 100  # shares per option contract
_UNITS = ("usd", "cents")

# The two spellings the operator surfaces already use for one account.
_MONEY_FIELDS: dict[str, tuple[str, ...]] = {
    "cash": ("cash_cents", "cash", "cash_available", "cash_balance"),
    "buying_power": ("buying_power_cents", "buying_power", "bp", "options_buying_power"),
    "equity": ("equity_cents", "equity", "account_equity", "portfolio_value"),
}
_TIME_FIELDS = ("taken_at", "as_of", "timestamp", "time")


class AccountReason(StrEnum):
    """Every reason this module refuses to admit evidence, or refuses an order."""

    NO_ACCOUNT = "no_account"
    STALE_ACCOUNT = "stale_account"
    UNDATABLE_ACCOUNT = "undatable_account"
    NEGATIVE_ACCOUNT = "negative_account"
    MALFORMED_ACCOUNT = "malformed_account"


class BpReason(StrEnum):
    """The refusal reasons a `BpDecision` may carry. Kept small and closed."""

    NO_ACCOUNT = "no_account"
    STALE_ACCOUNT = "stale_account"
    MALFORMED_ACCOUNT = "malformed_account"
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"
    FEE_MODEL_MISSING = "fee_model_missing"


class AccountError(RuntimeError):
    """The account feed or a declared fee model cannot be used as evidence.

    `reason` is a named `AccountReason`; nothing in the message is a guess. The guard
    maps an `AccountError` onto its own closed set of `BpReason`s and keeps the detail.
    """

    def __init__(self, message: str, *, reason: AccountReason) -> None:
        super().__init__(message)
        self.reason = reason


# ── money ────────────────────────────────────────────────────────────────────
def usd_to_cents(value: object, *, field: str = "amount") -> int:
    """Exact integer cents from a dollar amount. Never rounds.

    `Decimal(str(value))` is used because `1.15` as a float is not `1.15` as a dollar
    amount; converting through its shortest decimal spelling keeps the operator's number.
    A boolean is not money, a NaN/infinite is not money, and a sub-cent value is refused
    instead of being rounded into a different price.
    """
    if isinstance(value, bool):
        raise AccountError(
            f"{field}: a boolean is not money", reason=AccountReason.MALFORMED_ACCOUNT
        )
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise AccountError(
            f"{field}: {value!r} is not a number", reason=AccountReason.MALFORMED_ACCOUNT
        ) from None
    if not amount.is_finite():
        raise AccountError(
            f"{field}: {value!r} is not a finite number", reason=AccountReason.MALFORMED_ACCOUNT
        )
    if amount < 0:
        raise AccountError(
            f"{field}: negative amount {value!r}", reason=AccountReason.NEGATIVE_ACCOUNT
        )
    scaled = amount * 100
    if scaled != scaled.to_integral_value():
        raise AccountError(
            f"{field}: {value!r} is not a whole cent; refusing to round money",
            reason=AccountReason.MALFORMED_ACCOUNT,
        )
    return int(scaled)


def cents_to_usd(cents: int) -> str:
    """`12345` -> `"$123.45"`. Display only; the wire format stays integer cents."""
    return f"${Decimal(cents) / 100:,.2f}"


def _cents(value: object, *, field: str) -> int:
    """Integer cents from a declared cent field. Rejects bools and floats."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise AccountError(
            f"{field}: integer cents required, got {type(value).__name__}",
            reason=AccountReason.MALFORMED_ACCOUNT,
        )
    if value < 0:
        raise AccountError(
            f"{field}: negative amount {value}", reason=AccountReason.NEGATIVE_ACCOUNT
        )
    return value


def _money(value: object, *, field: str, units: str) -> int:
    """Parse one money field in the feed's declared unit."""
    if units == "cents":
        return _cents(value, field=field)
    return usd_to_cents(value, field=field)


def _check_units(units: str) -> str:
    if units not in _UNITS:
        raise AccountError(
            f"units must be one of {', '.join(_UNITS)}, got {units!r}",
            reason=AccountReason.MALFORMED_ACCOUNT,
        )
    return units


def _check_max_age(max_age_seconds: float | None) -> float | None:
    if max_age_seconds is None:
        return None
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, (int, float))
        or not math.isfinite(float(max_age_seconds))
        or max_age_seconds < 0
    ):
        raise AccountError(
            f"max_age_seconds must be a finite non-negative number or None, "
            f"got {max_age_seconds!r}",
            reason=AccountReason.MALFORMED_ACCOUNT,
        )
    return float(max_age_seconds)


# ── payload helpers (the same conventions as `venue.py`/`broker.py`) ────────
def _timestamp(value: object) -> float | None:
    """Epoch seconds from an epoch number or an ISO-8601 string. None when unreadable.

    `None` (rather than a guess) is what lets the staleness check refuse an undatable
    balance instead of treating it as fresh.
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


def _dig(payload: object, dotted: str) -> object:
    """Read `results.0.cash` out of nested dicts/lists. None when absent."""
    current: object = payload
    for part in dotted.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
        if current is None:
            return None
    return current


def _time_from_payload(payload: object) -> float | None:
    if not isinstance(payload, Mapping):
        return None
    for name in _TIME_FIELDS:
        if name in payload:
            stamp = _timestamp(payload[name])
            if stamp is not None:
                return stamp
    return None


def _parse_account_payload(
    payload: object, *, units: str, where: str = "account snapshot"
) -> tuple[int, int, int]:
    """Read cash/buying_power/equity out of an object, refusing anything missing.

    A payload with no recognisable money field is malformed *evidence*, not a zero
    balance. The distinction is the whole module: zero cash refuses a buy, while no
    feed also refuses a buy - but only one of them means "the account is empty".
    """
    if not isinstance(payload, Mapping):
        raise AccountError(
            f"{where} must be a JSON object, got {type(payload).__name__}",
            reason=AccountReason.MALFORMED_ACCOUNT,
        )
    found_keys = ", ".join(sorted(str(key) for key in payload)) or "nothing"
    values: dict[str, int] = {}
    for name, keys in _MONEY_FIELDS.items():
        key = next((candidate for candidate in keys if candidate in payload), None)
        if key is None:
            raise AccountError(
                f"{where} has no {name} field (looked for {', '.join(keys)}; found {found_keys})",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )
        if key.endswith("_cents"):
            values[name] = _cents(payload[key], field=f"{where}.{key}")
        else:
            values[name] = _money(payload[key], field=f"{where}.{key}", units=units)
    return values["cash"], values["buying_power"], values["equity"]


def _parse_paths(
    body: object, paths: Mapping[str, str], *, units: str, where: str
) -> tuple[int, int, int]:
    """Read each account number from its configured dotted path."""
    values: dict[str, int] = {}
    for name, dotted in paths.items():
        if not dotted:
            raise AccountError(
                f"{where}: no {name} path configured", reason=AccountReason.MALFORMED_ACCOUNT
            )
        raw = _dig(body, dotted)
        if raw is None:
            raise AccountError(
                f"{where}: {name} path {dotted!r} is not in the response",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )
        values[name] = _money(raw, field=f"{where}.{dotted}", units=units)
    return values["cash"], values["buying_power"], values["equity"]


# ── the evidence ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AccountSnapshot:
    """The venue account at an instant, in integer cents. `taken_at=None` = unknown.

    The three numbers are cents, not dollars: `cash=25010` is $250.10. That is the only
    representation this module computes with; `cents_to_usd` exists for display and for
    the CLI, and a log line always says `_cents` so nobody reads a balance as dollars.
    """

    cash: int
    buying_power: int
    equity: int
    taken_at: float | None
    source: str = ""
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        for name in ("cash", "buying_power", "equity"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise AccountError(
                    f"{name}: integer cents required, got {type(value).__name__}",
                    reason=AccountReason.MALFORMED_ACCOUNT,
                )
            if value < 0:
                raise AccountError(
                    f"{name}: negative amount {value}", reason=AccountReason.NEGATIVE_ACCOUNT
                )
        if self.taken_at is not None and (
            isinstance(self.taken_at, bool)
            or not isinstance(self.taken_at, (int, float))
            or not math.isfinite(float(self.taken_at))
        ):
            raise AccountError(
                f"taken_at: must be a finite epoch second or None, got {self.taken_at!r}",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )
        if not isinstance(self.source, str):
            raise AccountError("source: must be a string", reason=AccountReason.MALFORMED_ACCOUNT)

    @property
    def available_cents(self) -> int:
        """What an entry can actually spend: the tighter of cash and buying power."""
        return min(self.cash, self.buying_power)

    def age_s(self, now: float | None = None) -> float | None:
        """Seconds since the snapshot was taken, or None when it cannot be dated."""
        if self.taken_at is None:
            return None
        moment = time.time() if now is None else now
        return max(0.0, moment - float(self.taken_at))

    def stale(
        self, now: float | None = None, max_age_seconds: float | None = DEFAULT_MAX_AGE_SECONDS
    ) -> bool:
        """True when the snapshot is provably too old. Unknown age is NOT stale.

        Undatable evidence is refused separately (`check_age`), because "we cannot date
        this balance" and "this balance is old" are different operator problems.
        """
        maximum = _check_max_age(max_age_seconds)
        if maximum is None:
            return False
        age = self.age_s(now)
        return age is not None and age > maximum

    def check_age(
        self, now: float | None = None, max_age_seconds: float | None = DEFAULT_MAX_AGE_SECONDS
    ) -> None:
        """Refuse an old or undatable snapshot. Called by the feeds, not by callers."""
        maximum = _check_max_age(max_age_seconds)
        if maximum is None:
            return
        age = self.age_s(now)
        if age is None:
            raise AccountError(
                "account snapshot has no timestamp and a maximum age is set: "
                "an undatable balance is not evidence",
                reason=AccountReason.UNDATABLE_ACCOUNT,
            )
        if age > maximum:
            raise AccountError(
                f"account snapshot is {age:.0f}s old (limit {maximum:.0f}s): "
                f"re-read the account before sizing an entry",
                reason=AccountReason.STALE_ACCOUNT,
            )

    def to_dict(self) -> dict[str, Any]:
        """Wire shape. Money stays integer cents, spelled as such."""
        return {
            "schema": self.schema,
            "source": self.source,
            "taken_at": self.taken_at,
            "cash_cents": self.cash,
            "buying_power_cents": self.buying_power,
            "equity_cents": self.equity,
            "available_cents": self.available_cents,
        }


class AccountFeed(Protocol):
    """Anything that can produce a stamped, validated account snapshot."""

    @property
    def configured(self) -> bool: ...

    @property
    def source(self) -> str: ...

    def snapshot(self, now: float | None = None) -> AccountSnapshot: ...


@dataclass
class FileAccountFeed:
    """A stamped account snapshot from a JSON file. Missing is unavailable, never zero.

    `max_age_seconds` is armed by default: a live path that reads an operator export
    must refuse last week's balance. It can be set to None for a recorded fixture.
    When the payload carries no timestamp, the file's mtime is used - the honest
    statement "this is when the bytes were written" - because that is what makes a
    stale export refuse itself.
    """

    path: Path | None = None
    max_age_seconds: float | None = DEFAULT_MAX_AGE_SECONDS
    units: str = "usd"
    label: str = ""
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
        _check_units(self.units)
        _check_max_age(self.max_age_seconds)

    @property
    def configured(self) -> bool:
        return self.path is not None

    @property
    def source(self) -> str:
        return self.label or f"file:{self.path}"

    def snapshot(self, now: float | None = None) -> AccountSnapshot:
        if self.path is None:
            raise AccountError(
                "no account snapshot file configured", reason=AccountReason.NO_ACCOUNT
            )
        moment = self.clock() if now is None else float(now)
        try:
            raw: object = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise AccountError(
                f"account snapshot not found: {self.path}", reason=AccountReason.NO_ACCOUNT
            ) from None
        except OSError as exc:
            raise AccountError(
                f"cannot read account snapshot {self.path}: {exc}", reason=AccountReason.NO_ACCOUNT
            ) from None
        except json.JSONDecodeError as exc:
            raise AccountError(
                f"account snapshot {self.path} is not JSON: {exc}",
                reason=AccountReason.MALFORMED_ACCOUNT,
            ) from None
        cash, buying_power, equity = _parse_account_payload(raw, units=self.units)
        taken_at = _time_from_payload(raw)
        if taken_at is None:
            try:
                taken_at = self.path.stat().st_mtime
            except OSError:  # pragma: no cover - stat after read
                taken_at = None
        snapshot = AccountSnapshot(
            cash=cash,
            buying_power=buying_power,
            equity=equity,
            taken_at=taken_at,
            source=self.source,
        )
        snapshot.check_age(moment, self.max_age_seconds)
        return snapshot


@dataclass
class HttpAccountFeed:
    """Account numbers from a URL template YOU supply. No vendor schema is invented.

        MFF_ACCOUNT_URL=https://.../account?account={account}
        MFF_ACCOUNT_CASH_PATH=results.0.cash
        MFF_ACCOUNT_BUYING_POWER_PATH=results.0.buying_power
        MFF_ACCOUNT_EQUITY_PATH=results.0.equity
        MFF_ACCOUNT_UNITS=usd            # or "cents"

    A non-2xx answer, a transport failure or a missing path is `no_account` or
    `malformed_account`: no evidence. The bearer token is read from `MFF_RH_TOKEN` (the
    same credential the order adapter uses) and never appears in an error string or in
    `source`. An HTTP answer carries the read instant as its stamp when the body has no
    timestamp, because that is the only honest time available for a live answer.
    """

    template: str = field(default_factory=lambda: os.environ.get("MFF_ACCOUNT_URL", ""))
    cash_path: str = field(default_factory=lambda: os.environ.get("MFF_ACCOUNT_CASH_PATH", "cash"))
    buying_power_path: str = field(
        default_factory=lambda: os.environ.get("MFF_ACCOUNT_BUYING_POWER_PATH", "buying_power")
    )
    equity_path: str = field(
        default_factory=lambda: os.environ.get("MFF_ACCOUNT_EQUITY_PATH", "equity")
    )
    time_path: str = field(default_factory=lambda: os.environ.get("MFF_ACCOUNT_TIME_PATH", ""))
    units: str = field(default_factory=lambda: os.environ.get("MFF_ACCOUNT_UNITS", "usd"))
    token: str = field(default_factory=lambda: os.environ.get("MFF_RH_TOKEN", ""))
    account: str = field(default_factory=lambda: os.environ.get("MFF_RH_ACCOUNT", ""))
    timeout_s: float = 5.0
    max_age_seconds: float | None = DEFAULT_MAX_AGE_SECONDS
    transport: Callable[[urllib.request.Request, float], tuple[int, object]] | None = None
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        _check_units(self.units)
        _check_max_age(self.max_age_seconds)
        if not math.isfinite(float(self.timeout_s)) or self.timeout_s <= 0:
            raise AccountError(
                f"timeout_s must be positive, got {self.timeout_s!r}",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )

    @property
    def configured(self) -> bool:
        return bool(self.template)

    @property
    def source(self) -> str:
        """Host only: a URL can carry a token in its query and a log must not."""
        try:
            return f"http:{urllib.parse.urlsplit(self.template).netloc or 'unparsed'}"
        except ValueError:  # pragma: no cover - malformed template
            return "http:unparsed"

    def snapshot(self, now: float | None = None) -> AccountSnapshot:
        if not self.template:
            raise AccountError(
                "no account feed configured (set MFF_ACCOUNT_URL)", reason=AccountReason.NO_ACCOUNT
            )
        moment = self.clock() if now is None else float(now)
        url = self.template.replace("{account}", urllib.parse.quote(self.account))
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        status, body = self._send(request)
        if status == 0:
            raise AccountError(
                "account feed unreachable (transport error)", reason=AccountReason.NO_ACCOUNT
            )
        if not (200 <= status < 300):
            raise AccountError(f"account feed HTTP {status}", reason=AccountReason.NO_ACCOUNT)
        cash, buying_power, equity = _parse_paths(
            body,
            {
                "cash": self.cash_path,
                "buying_power": self.buying_power_path,
                "equity": self.equity_path,
            },
            units=self.units,
            where="account feed",
        )
        taken_at = _time_from_payload(body)
        if taken_at is None and self.time_path:
            taken_at = _timestamp(_dig(body, self.time_path))
        if taken_at is None:
            taken_at = moment
        snapshot = AccountSnapshot(
            cash=cash,
            buying_power=buying_power,
            equity=equity,
            taken_at=taken_at,
            source=self.source,
        )
        snapshot.check_age(moment, self.max_age_seconds)
        return snapshot

    def _send(self, request: urllib.request.Request) -> tuple[int, object]:
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


@dataclass
class StaticAccountFeed:
    """A snapshot already in memory, for tests and replay. None is `no_account`."""

    account: AccountSnapshot | None = None
    label: str = "static"
    max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        _check_max_age(self.max_age_seconds)

    @property
    def configured(self) -> bool:
        return self.account is not None

    @property
    def source(self) -> str:
        return self.label

    def snapshot(self, now: float | None = None) -> AccountSnapshot:
        if self.account is None:
            raise AccountError("no static account snapshot", reason=AccountReason.NO_ACCOUNT)
        self.account.check_age(now, self.max_age_seconds)
        return self.account


def account_feed_from_env(*, max_age_seconds: float | None = None) -> AccountFeed | None:
    """The feed the environment configures, or None. A file wins over a URL.

    Neither variable set means the precheck is OFF, and `arm` must say so out loud
    rather than imply entries are guarded.
    """
    age = DEFAULT_MAX_AGE_SECONDS if max_age_seconds is None else max_age_seconds
    snapshot_path = os.environ.get("MFF_ACCOUNT_SNAPSHOT", "").strip()
    if snapshot_path:
        return FileAccountFeed(path=Path(snapshot_path), max_age_seconds=age)
    url = os.environ.get("MFF_ACCOUNT_URL", "").strip()
    if url:
        return HttpAccountFeed(max_age_seconds=age)
    return None


# ── fees: a declared model, not a guess ──────────────────────────────────────
@dataclass(frozen=True)
class FeeModel:
    """The venue's declared fee schedule, in integer cents.

    Per-contract fees are linear; `minimum_fee_cents` is the floor per order. The fee
    for an order is `max(minimum_fee_cents, qty * (venue + regulatory))`. A declared
    zero model (`FeeModel()`) is legitimate and is written explicitly; a missing model
    is not, and the guard refuses an entry with `fee_model_missing`.
    """

    per_contract_venue_cents: int = 0
    per_contract_regulatory_cents: int = 0
    minimum_fee_cents: int = 0
    schema: str = FEE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "per_contract_venue_cents",
            "per_contract_regulatory_cents",
            "minimum_fee_cents",
        ):
            _cents(getattr(self, name), field=name)
        if not isinstance(self.schema, str):
            raise AccountError(
                "fee model schema must be a string", reason=AccountReason.MALFORMED_ACCOUNT
            )

    @property
    def per_contract_cents(self) -> int:
        return self.per_contract_venue_cents + self.per_contract_regulatory_cents

    def fees_for(self, qty: int) -> int:
        """The declared fee for `qty` contracts, in whole cents."""
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise AccountError(
                f"fees_for: qty must be a positive integer, got {qty!r}",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )
        return max(self.minimum_fee_cents, qty * self.per_contract_cents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "per_contract_venue_cents": self.per_contract_venue_cents,
            "per_contract_regulatory_cents": self.per_contract_regulatory_cents,
            "minimum_fee_cents": self.minimum_fee_cents,
        }

    @classmethod
    def from_dict(cls, payload: object) -> FeeModel:
        if not isinstance(payload, Mapping):
            raise AccountError(
                "fee model must be an object", reason=AccountReason.MALFORMED_ACCOUNT
            )
        known = {
            "schema",
            "version",
            "per_contract_venue_cents",
            "per_contract_regulatory_cents",
            "minimum_fee_cents",
        }
        unknown = sorted(str(key) for key in payload if key not in known)
        if unknown:
            raise AccountError(
                f"fee model has unknown key(s): {', '.join(unknown)}",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )
        version = payload.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise AccountError(
                f"fee model version must be 1, got {version!r}",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )
        values: dict[str, int] = {}
        for name in (
            "per_contract_venue_cents",
            "per_contract_regulatory_cents",
            "minimum_fee_cents",
        ):
            values[name] = _cents(payload.get(name, 0), field=f"fee model.{name}")
        return cls(
            per_contract_venue_cents=values["per_contract_venue_cents"],
            per_contract_regulatory_cents=values["per_contract_regulatory_cents"],
            minimum_fee_cents=values["minimum_fee_cents"],
        )

    @classmethod
    def load(cls, path: str | Path, *, missing_ok: bool = True) -> FeeModel | None:
        """Read a declared fee model. A missing file is `None`, never a zero model."""
        target = Path(path)
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            if missing_ok:
                return None
            raise AccountError(
                f"fee model not found: {target}", reason=AccountReason.NO_ACCOUNT
            ) from None
        except OSError as exc:
            raise AccountError(
                f"cannot read fee model {target}: {exc}", reason=AccountReason.NO_ACCOUNT
            ) from None
        try:
            payload: object = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AccountError(
                f"fee model {target} is not JSON: {exc}", reason=AccountReason.MALFORMED_ACCOUNT
            ) from None
        return cls.from_dict(payload)


def _env_cents(name: str, env: Mapping[str, str]) -> int:
    raw = env.get(name, "").strip()
    try:
        return int(raw)
    except ValueError:
        raise AccountError(
            f"{name}: {raw!r} is not integer cents", reason=AccountReason.MALFORMED_ACCOUNT
        ) from None


def fee_model_from_env(env: Mapping[str, str] | None = None) -> FeeModel | None:
    """A declared fee model from the environment, or None when nothing is configured.

    `MFF_ACCOUNT_FEES` names a JSON file (a missing file raises here: a configured path
    that disappeared must not silently become a zero-fee model). Otherwise the three
    `MFF_FEE_*_CENTS` variables must be declared together; a partial declaration is
    refused rather than completed with invented zeros.
    """
    source = os.environ if env is None else env
    path = source.get("MFF_ACCOUNT_FEES", "").strip()
    if path:
        model = FeeModel.load(Path(path), missing_ok=False)
        assert model is not None  # missing_ok=False either raises or returns
        return model
    names = ("MFF_FEE_VENUE_CENTS", "MFF_FEE_REGULATORY_CENTS", "MFF_FEE_MINIMUM_CENTS")
    declared = [name for name in names if source.get(name, "").strip()]
    if not declared:
        return None
    if len(declared) != len(names):
        missing = [name for name in names if name not in declared]
        raise AccountError(
            f"fee model is partially declared; missing {', '.join(missing)}",
            reason=AccountReason.MALFORMED_ACCOUNT,
        )
    return FeeModel(
        per_contract_venue_cents=_env_cents(names[0], source),
        per_contract_regulatory_cents=_env_cents(names[1], source),
        minimum_fee_cents=_env_cents(names[2], source),
    )


@dataclass(frozen=True)
class PremiumCost:
    """The exact cents one order debits, with fees.

    `limit_cents` is the per-share limit in cents and `multiplier` is shares per
    contract (100 for a standard US equity option). The debit is
    `limit_cents * multiplier * qty`, computed as integers, plus the declared fees.
    Nothing here is a float; no multiplication happens on `1.15`.
    """

    qty: int
    limit_cents: int
    multiplier: int = OPTION_MULTIPLIER
    fee_model: FeeModel = field(default_factory=FeeModel)

    def __post_init__(self) -> None:
        if isinstance(self.qty, bool) or not isinstance(self.qty, int) or self.qty <= 0:
            raise AccountError(
                f"qty must be a positive integer, got {self.qty!r}",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )
        if (
            isinstance(self.limit_cents, bool)
            or not isinstance(self.limit_cents, int)
            or self.limit_cents <= 0
        ):
            raise AccountError(
                f"limit_cents must be a positive integer, got {self.limit_cents!r}",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )
        if (
            isinstance(self.multiplier, bool)
            or not isinstance(self.multiplier, int)
            or self.multiplier <= 0
        ):
            raise AccountError(
                f"multiplier must be a positive integer, got {self.multiplier!r}",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )
        if not isinstance(self.fee_model, FeeModel):
            raise AccountError(
                "fee_model must be a declared FeeModel", reason=AccountReason.MALFORMED_ACCOUNT
            )

    @property
    def per_contract_cents(self) -> int:
        """The premium for ONE contract: limit per share * shares per contract."""
        return self.limit_cents * self.multiplier

    @property
    def premium_cents(self) -> int:
        return self.per_contract_cents * self.qty

    @property
    def venue_fee_cents(self) -> int:
        return self.fee_model.per_contract_venue_cents * self.qty

    @property
    def regulatory_fee_cents(self) -> int:
        return self.fee_model.per_contract_regulatory_cents * self.qty

    @property
    def fees_cents(self) -> int:
        return self.fee_model.fees_for(self.qty)

    @property
    def total_cents(self) -> int:
        return self.premium_cents + self.fees_cents

    def to_dict(self) -> dict[str, Any]:
        return {
            "qty": self.qty,
            "limit_cents": self.limit_cents,
            "multiplier": self.multiplier,
            "per_contract_cents": self.per_contract_cents,
            "premium_cents": self.premium_cents,
            "venue_fee_cents": self.venue_fee_cents,
            "regulatory_fee_cents": self.regulatory_fee_cents,
            "fees_cents": self.fees_cents,
            "total_cents": self.total_cents,
        }

    @classmethod
    def from_limit_usd(
        cls,
        limit: object,
        *,
        qty: int,
        multiplier: int = OPTION_MULTIPLIER,
        fee_model: FeeModel | None = None,
    ) -> PremiumCost:
        """Build from a dollar limit (the engine's price) without any float math."""
        return cls(
            qty=qty,
            limit_cents=usd_to_cents(limit, field="limit"),
            multiplier=multiplier,
            fee_model=fee_model or FeeModel(),
        )


# ── the decision ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BpDecision:
    """Admit or refuse one entry, with the numbers that produced the answer.

    `reason` is empty on success and one of `BpReason` on refusal. `required_cents` is
    always populated so the fill log can show how far the order was from fitting, and
    `log_reason` is the exact string the listener writes: `bp_refused:<reason>`.
    """

    ok: bool
    reason: str = ""
    required_cents: int = 0
    snapshot_age_seconds: float | None = None
    premium_cents: int = 0
    fees_cents: int = 0
    source: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise AccountError("ok must be a boolean", reason=AccountReason.MALFORMED_ACCOUNT)
        if not self.ok and not self.reason:
            raise AccountError(
                "a refusal must name its reason", reason=AccountReason.MALFORMED_ACCOUNT
            )
        if isinstance(self.required_cents, bool) or not isinstance(self.required_cents, int):
            raise AccountError(
                "required_cents must be integer cents", reason=AccountReason.MALFORMED_ACCOUNT
            )
        if self.required_cents < 0:
            raise AccountError(
                "required_cents must not be negative", reason=AccountReason.MALFORMED_ACCOUNT
            )

    @property
    def log_reason(self) -> str:
        """The fill-log reason for this check: one named string, success included."""
        return "bp_ok" if self.ok else f"bp_refused:{self.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "log_reason": self.log_reason,
            "required_cents": self.required_cents,
            "premium_cents": self.premium_cents,
            "fees_cents": self.fees_cents,
            "snapshot_age_seconds": self.snapshot_age_seconds,
            "source": self.source,
            "detail": self.detail,
        }


def _normalize_side(side: object, contract: object) -> str:
    """Normalise an order action; exits are the only value that bypasses this guard."""
    raw = getattr(side, "value", side)
    if not isinstance(raw, str):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
    normalised = raw.strip().lower()
    if normalised not in {"buy", "sell"}:
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
    if not isinstance(contract, str):
        raise ValueError(f"contract must be a string, got {contract!r}")
    return normalised


@dataclass
class BuyingPowerGuard:
    """The precheck the listener runs before an entry reaches the broker.

    The guard is a pure decision once a snapshot exists: `check(contract, side, qty,
    limit, snapshot, now)`. `precheck` is the integration seam that also asks the feed
    for a fresh snapshot and converts a feed refusal into a refusal decision. Neither
    method sends an order, and neither ever blocks a `sell`.
    """

    feed: AccountFeed | None = None
    fee_model: FeeModel | None = None
    max_age_seconds: float | None = DEFAULT_MAX_AGE_SECONDS
    multiplier: int = OPTION_MULTIPLIER
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        _check_max_age(self.max_age_seconds)
        if (
            isinstance(self.multiplier, bool)
            or not isinstance(self.multiplier, int)
            or self.multiplier <= 0
        ):
            raise AccountError(
                f"multiplier must be a positive integer, got {self.multiplier!r}",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )
        if self.fee_model is not None and not isinstance(self.fee_model, FeeModel):
            raise AccountError(
                "fee_model must be a declared FeeModel or None",
                reason=AccountReason.MALFORMED_ACCOUNT,
            )

    # ── the pure decision ─────────────────────────────────────────────────────
    def check(
        self,
        contract: str,
        side: str,
        qty: int,
        limit: object,
        snapshot: AccountSnapshot | None,
        now: float | None = None,
    ) -> BpDecision:
        """Decide one order against a snapshot. Never blocks an exit.

        `side` is the ORDER action (`"buy"` / `"sell"`), not call/put. A `"sell"` is
        admitted immediately: refusing to reduce risk because the account feed is down
        would invert the safety property the desk is built on.
        """
        action = _normalize_side(side, contract)
        moment = self.clock() if now is None else float(now)
        if action == "sell":
            age = snapshot.age_s(moment) if isinstance(snapshot, AccountSnapshot) else None
            return BpDecision(
                ok=True,
                required_cents=0,
                snapshot_age_seconds=age,
                source=snapshot.source if isinstance(snapshot, AccountSnapshot) else "",
                detail="exit: buying power is never a reason to hold risk",
            )
        if snapshot is None:
            return BpDecision(
                ok=False, reason=BpReason.NO_ACCOUNT.value, detail="no account snapshot"
            )
        if not isinstance(snapshot, AccountSnapshot):
            return BpDecision(
                ok=False,
                reason=BpReason.MALFORMED_ACCOUNT.value,
                detail=f"not an AccountSnapshot: {type(snapshot).__name__}",
            )
        age = snapshot.age_s(moment)
        if self.max_age_seconds is not None:
            if age is None:
                return BpDecision(
                    ok=False,
                    reason=BpReason.MALFORMED_ACCOUNT.value,
                    source=snapshot.source,
                    detail="undatable account snapshot; a balance with no time is not evidence",
                )
            if age > self.max_age_seconds:
                return BpDecision(
                    ok=False,
                    reason=BpReason.STALE_ACCOUNT.value,
                    snapshot_age_seconds=age,
                    source=snapshot.source,
                    detail=(
                        f"account snapshot is {age:.0f}s old (limit {self.max_age_seconds:.0f}s)"
                    ),
                )
        if self.fee_model is None:
            return BpDecision(
                ok=False,
                reason=BpReason.FEE_MODEL_MISSING.value,
                snapshot_age_seconds=age,
                source=snapshot.source,
                detail="no declared fee model; the debit cannot be stated exactly",
            )
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValueError(f"qty must be a positive integer, got {qty!r}")
        cost = PremiumCost(
            qty=qty,
            limit_cents=usd_to_cents(limit, field=f"{contract} limit"),
            multiplier=self.multiplier,
            fee_model=self.fee_model,
        )
        required = cost.total_cents
        if required > snapshot.cash:
            return BpDecision(
                ok=False,
                reason=BpReason.INSUFFICIENT_CASH.value,
                required_cents=required,
                snapshot_age_seconds=age,
                premium_cents=cost.premium_cents,
                fees_cents=cost.fees_cents,
                source=snapshot.source,
                detail=(f"needs {cents_to_usd(required)}; cash is {cents_to_usd(snapshot.cash)}"),
            )
        if required > snapshot.buying_power:
            return BpDecision(
                ok=False,
                reason=BpReason.INSUFFICIENT_BUYING_POWER.value,
                required_cents=required,
                snapshot_age_seconds=age,
                premium_cents=cost.premium_cents,
                fees_cents=cost.fees_cents,
                source=snapshot.source,
                detail=(
                    f"needs {cents_to_usd(required)}; buying power is "
                    f"{cents_to_usd(snapshot.buying_power)}"
                ),
            )
        return BpDecision(
            ok=True,
            required_cents=required,
            snapshot_age_seconds=age,
            premium_cents=cost.premium_cents,
            fees_cents=cost.fees_cents,
            source=snapshot.source,
            detail=(
                f"{cents_to_usd(required)} fits (cash "
                f"{cents_to_usd(snapshot.cash)}, buying power "
                f"{cents_to_usd(snapshot.buying_power)})"
            ),
        )

    # ── the integration seam ──────────────────────────────────────────────────
    def precheck(
        self, contract: str, side: str, qty: int, limit: object, now: float | None = None
    ) -> BpDecision:
        """Ask the feed for fresh evidence, then `check` it. The listener's hook.

        An exit returns before the feed is touched: no evidence is needed to reduce
        risk. Any feed refusal is translated into a refusal decision with the closed
        reason set, so the caller records one `bp_refused:<reason>` row and does not
        call the broker.
        """
        action = _normalize_side(side, contract)
        moment = self.clock() if now is None else float(now)
        if action == "sell":
            return self.check(contract, side, qty, limit, None, moment)
        feed = self.feed
        if feed is None or not feed.configured:
            return BpDecision(
                ok=False, reason=BpReason.NO_ACCOUNT.value, detail="no account feed configured"
            )
        try:
            snapshot = feed.snapshot(moment)
        except AccountError as exc:
            return _refusal_from_feed(exc)
        return self.check(contract, side, qty, limit, snapshot, moment)

    def precheck_intent(self, intent: OrderIntent, now: float | None = None) -> BpDecision:
        """`precheck` for the objects the listener actually places."""
        return self.precheck(intent.contract_key, intent.action, intent.qty, intent.limit, now)


def _refusal_from_feed(exc: AccountError) -> BpDecision:
    """Map a feed's named reason onto the guard's closed refusal set."""
    if exc.reason is AccountReason.STALE_ACCOUNT:
        reason = BpReason.STALE_ACCOUNT
    elif exc.reason is AccountReason.NO_ACCOUNT:
        reason = BpReason.NO_ACCOUNT
    else:
        reason = BpReason.MALFORMED_ACCOUNT
    return BpDecision(ok=False, reason=reason.value, detail=f"{exc.reason.value}: {exc}")


# ── rendering (the CLI helpers the Lead wires) ───────────────────────────────
def render_snapshot(snapshot: AccountSnapshot, now: float | None = None) -> str:
    """A short human view of one stamped account snapshot."""
    age = snapshot.age_s(now)
    age_text = "undatable" if age is None else f"{age:.0f}s old"
    return "\n".join(
        [
            f"account  {snapshot.source or '(unknown source)'}",
            f"cash           {cents_to_usd(snapshot.cash)}",
            f"buying power   {cents_to_usd(snapshot.buying_power)}",
            f"equity         {cents_to_usd(snapshot.equity)}",
            f"available      {cents_to_usd(snapshot.available_cents)}",
            f"taken          {age_text}",
        ]
    )


def render_fee_model(model: FeeModel) -> str:
    """The declared fee schedule, in cents (the unit the model stores)."""
    return "\n".join(
        [
            f"venue fee      {model.per_contract_venue_cents} cents/contract",
            f"regulatory     {model.per_contract_regulatory_cents} cents/contract",
            f"minimum        {model.minimum_fee_cents} cents/order",
            f"per contract   {model.per_contract_cents} cents",
        ]
    )


def render_decision(decision: BpDecision, *, contract: str = "") -> str:
    """One line for `account check` and one row for the fill log."""
    subject = f" {contract}" if contract else ""
    line = f"{decision.log_reason}{subject}: requires {cents_to_usd(decision.required_cents)}"
    if decision.premium_cents:
        line += (
            f" ({cents_to_usd(decision.premium_cents)} premium + "
            f"{cents_to_usd(decision.fees_cents)} fees)"
        )
    if decision.detail:
        line += f" - {decision.detail}"
    return line
