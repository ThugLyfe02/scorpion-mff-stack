"""Live option-contract identity: OSI/OPRA symbols, validation, and resolution.

The hot path used to carry one identity for an option: the internal
``"TICKER|SIDE|STRIKE|EXPIRY"`` contract key. That key is fine for the engine's own
book, but it is not what a venue can execute: a Robinhood order needs the **OSI/OPRA
symbol** (or an instrument id), the **contract multiplier**, and a chain record that
proves the contract exists. Posting the internal key as if it were an instrument is
how a live order becomes unresolvable - or, worse, resolvable to the wrong strike.

This module is that missing boundary. It is pure logic: no HTTP, no disk, no clock
except through the callables a caller injects.

Two identities, one validation path
-----------------------------------

``OptionContract`` is an immutable, validated option identity:

* ticker/root - up to six characters, ``A-Z0-9.`` (``BRK.B`` is a real root);
* side - ``CALL`` / ``PUT``;
* strike - **integer cents** (``2.5`` dollars is ``250`` cents), because money math
  in floats is how a $227.30 strike becomes a $227.29 order;
* expiry - a ``datetime.date``, so ``0DTE`` must be resolved against a session date
  by exactly one caller, once.

Every construction refusal is a named reason: ``bad_ticker``, ``bad_side``,
``bad_strike``, ``bad_expiry``. The OSI symbol is *derived*, never parsed back out
of thin air: 6-char space-padded root, ``YYMMDD``, ``C``/``P``, and strike*1000
zero-padded to eight digits (``QQQ   260918C00719000``).

Parsing a contract key
----------------------

``OptionContract.from_contract_key`` accepts the two forms the engine emits -
``QQQ|CALL|719|0DTE`` and ``NVDA|PUT|227.5|2026-09-11`` - and refuses everything it
cannot turn into a real date. ``Dec 5`` is refused because a month/day without a
year is a guess; the parser's year-roll belongs in the parser, and an identity that
was already rolled once is not rolled twice. ``0DTE`` requires the ``session_date``
argument; without it the refusal is ``bad_expiry``, never "today".

Resolution
----------

``ContractResolver`` is the protocol the broker depends on. ``ChainContractResolver``
reads a chain through an injected callable and accepts a contract only when:

* exactly one candidate matches strike + expiry + side (two matches is
  ``contract_ambiguous``; a first-pick would silently choose one of two real
  instruments);
* the candidate's multiplier is in the declared set (default ``{100}``);
* the candidate names an instrument id;
* the contract has not already expired relative to the session (``0DTE`` contracts
  leave the chain intraday: that is ``contract_expired_same_day``, not a missing
  strike).

Chain rows are *declared*, not invented from a vendor schema. A row is an OSI
symbol (``osi_symbol`` / ``symbol``) or explicit ``ticker``/``side``/``strike``
(dollars) or ``strike_cents``/``expiry`` (ISO date), plus an ``instrument_id`` and a
``multiplier``. Rows are evidence: a malformed row refuses the whole resolution
(``contract_chain_malformed``) rather than being skipped, because a chain that
contains junk is a chain that cannot be trusted.

``StaticContractResolver`` is the offline/test resolver: a declared mapping from
contract key to ``InstrumentRef``.

Broker hook
-----------

``RobinhoodAgenticBroker`` accepts an optional ``resolver``. When one is present
``_build_request`` resolves the intent's contract key and adds ``osi_symbol``,
``instrument_id`` and ``multiplier`` to the order body; a refused resolution raises
a token-free ``BrokerError`` naming the reason **before** any HTTP send. When the
resolver is absent the payload is byte-for-byte what it was before.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol, TypeGuard

from .types import Side

# ── named refusals: the contract itself ──────────────────────────────────────
BAD_TICKER = "bad_ticker"
BAD_SIDE = "bad_side"
BAD_STRIKE = "bad_strike"
BAD_EXPIRY = "bad_expiry"
BAD_CONTRACT_KEY = "bad_contract_key"
BAD_OSI_SYMBOL = "bad_osi_symbol"

# ── named refusals: resolution ───────────────────────────────────────────────
CONTRACT_MISSING = "contract_missing"
CONTRACT_AMBIGUOUS = "contract_ambiguous"
CONTRACT_EXPIRED_SAME_DAY = "contract_expired_same_day"
CONTRACT_STRIKE_MISMATCH = "contract_strike_mismatch"
CONTRACT_MULTIPLIER_INVALID = "contract_multiplier_invalid"
CONTRACT_CHAIN_MALFORMED = "contract_chain_malformed"
CONTRACT_INSTRUMENT_MISSING = "contract_instrument_missing"

DEFAULT_MULTIPLIERS: frozenset[int] = frozenset({100})
OSI_LENGTH = 21

# A root is what OSI can carry: six characters, padded. The dot covers dual-class
# tickers such as BRK.B; the first character must be a letter so a numeric strike
# token can never be mistaken for a root.
_TICKER_RE = re.compile(r"[A-Z][A-Z0-9.]{0,5}")
_STRIKE_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Month-day spellings are refused by name in `from_contract_key`; the regex only
# exists to give that refusal a useful detail ("roll the year in the parser").
_MONTH_DAY_RE = re.compile(r"[A-Za-z]{3,9}\.?\s+[0-9]{1,2}")

# The strike field is strike*1000 in eight digits: $99,999.999 is the ceiling. At
# cent granularity that is strike cents <= 9,999,999. A strike above it cannot be
# encoded, so it must be refused here rather than silently truncated into an order
# for a different contract.
_MAX_STRIKE_CENTS = 9_999_999


class InstrumentError(ValueError):
    """A contract or instrument identity that cannot be trusted.

    ``reason`` is always one of the module's named refusal constants; ``detail`` is
    a token-free, human-readable explanation. Nothing on this path touches I/O, so
    the reason is the only thing a caller can branch on - it must never be blank.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _is_date(value: object) -> TypeGuard[date]:
    """A plain ``date``. A ``datetime`` is not an expiry: it hides a time of day."""
    return isinstance(value, date) and not isinstance(value, datetime)


@dataclass(frozen=True)
class OptionContract:
    """One option contract, immutable and validated at construction.

    ``strike_cents`` is integer cents on purpose. The engine's parser carries strikes
    as floats; the identity that reaches a venue must not, because a float strike can
    round away from the strike the chain actually lists.
    """

    ticker: str
    side: Side
    strike_cents: int
    expiry: date

    def __post_init__(self) -> None:
        ticker = self.ticker.strip().upper() if isinstance(self.ticker, str) else ""
        if not _TICKER_RE.fullmatch(ticker):
            raise InstrumentError(
                BAD_TICKER,
                f"{self.ticker!r} is not an option root (1-6 characters A-Z0-9., "
                f"OSI pads to six)")
        object.__setattr__(self, "ticker", ticker)

        side = self.side
        if isinstance(side, str):
            try:
                side = Side(side.strip().upper())
            except ValueError:
                raise InstrumentError(BAD_SIDE, f"{self.side!r} is not CALL or PUT") from None
        elif not isinstance(side, Side):
            raise InstrumentError(BAD_SIDE, f"{type(side).__name__} is not a Side")
        object.__setattr__(self, "side", side)

        strike = self.strike_cents
        if isinstance(strike, bool) or not isinstance(strike, int):
            raise InstrumentError(
                BAD_STRIKE, f"strike_cents must be an integer, got {type(strike).__name__}")
        if strike <= 0:
            raise InstrumentError(BAD_STRIKE, f"strike cents must be positive, got {strike}")
        if strike > _MAX_STRIKE_CENTS:
            raise InstrumentError(
                BAD_STRIKE, f"{strike / 100:,.2f} does not fit the eight-digit OSI strike")

        if not _is_date(self.expiry):
            raise InstrumentError(
                BAD_EXPIRY, f"expiry must be a date, got {type(self.expiry).__name__}")

    # ── derived identity ─────────────────────────────────────────────────────
    @property
    def strike(self) -> float:
        """Strike in dollars. Display/chain-read only; the identity is cents."""
        return self.strike_cents / 100

    @property
    def osi_symbol(self) -> str:
        """The 21-character OSI/OPRA symbol for this contract.

        Layout: root left-justified in six characters, expiry ``YYMMDD``, ``C``/``P``,
        strike*1000 zero-padded to eight digits. Example:
        ``QQQ   260918C00719000``.
        """
        root = self.ticker.ljust(6)
        month_day = f"{self.expiry:%y%m%d}"
        right = "C" if self.side.is_call else "P"
        strike = f"{self.strike_cents * 10:08d}"
        return f"{root}{month_day}{right}{strike}"

    @property
    def contract_key(self) -> str:
        """The engine's internal identity: ``TICKER|SIDE|STRIKE|ISO-DATE``.

        ``0DTE`` is resolved before this is reachable, so the key always carries a
        real date. That makes the key comparable across sessions without a special
        token that means a different date every day.
        """
        return f"{self.ticker}|{self.side.value}|{self.strike:g}|{self.expiry.isoformat()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "side": self.side.value,
            "strike_cents": self.strike_cents,
            "strike": self.strike,
            "expiry": self.expiry.isoformat(),
            "osi_symbol": self.osi_symbol,
            "contract_key": self.contract_key,
        }

    # ── parsers ──────────────────────────────────────────────────────────────
    @classmethod
    def parse_osi_symbol(cls, symbol: str) -> OptionContract:
        """Parse a 21-character OSI symbol. Every defect names its field.

        The symbol is space-padded, so the root is right-stripped before validation;
        the padding itself is required because an unpadded symbol of a different
        length is ambiguous about where the root ends and the date begins.
        """
        if not isinstance(symbol, str):
            raise InstrumentError(BAD_OSI_SYMBOL, "OSI symbol must be a string")
        if len(symbol) != OSI_LENGTH:
            raise InstrumentError(
                BAD_OSI_SYMBOL,
                f"OSI symbol must be {OSI_LENGTH} characters including root padding, "
                f"got {len(symbol)} ({symbol!r})")
        root = symbol[:6].rstrip()
        date_text = symbol[6:12]
        side_text = symbol[12]
        strike_text = symbol[13:]
        if not root:
            raise InstrumentError(BAD_OSI_SYMBOL, f"OSI symbol {symbol!r} has an empty root")
        if not date_text.isdigit():
            raise InstrumentError(
                BAD_EXPIRY, f"OSI date field {date_text!r} is not YYMMDD")
        if side_text not in ("C", "P"):
            raise InstrumentError(BAD_SIDE, f"OSI side field {side_text!r} is not C or P")
        if not strike_text.isdigit():
            raise InstrumentError(
                BAD_STRIKE, f"OSI strike field {strike_text!r} is not numeric")
        try:
            expiry = date(2000 + int(date_text[:2]), int(date_text[2:4]), int(date_text[4:]))
        except ValueError as exc:
            raise InstrumentError(BAD_EXPIRY, f"OSI date {date_text!r}: {exc}") from None
        field = int(strike_text)
        if field % 10:
            # OSI strikes are thousandths of a dollar; our identity is cents, so a
            # sub-cent strike cannot be represented without silently rounding it.
            raise InstrumentError(
                BAD_STRIKE,
                f"OSI strike {field} is not a whole number of cents")
        return cls(ticker=root, side=Side.CALL if side_text == "C" else Side.PUT,
                   strike_cents=field // 10, expiry=expiry)

    @classmethod
    def from_contract_key(cls, key: str, session_date: date | None = None) -> OptionContract:
        """Parse ``TICKER|SIDE|STRIKE|EXPIRY`` into a validated contract.

        ``EXPIRY`` is ``0DTE`` (resolved to ``session_date``), an ISO date, or a
        refusal. A month/day spelling such as ``Dec 5`` is refused with
        ``bad_expiry``: the year-roll belongs in the parser, and an identity that
        guessed a year once is exactly the contract that expires on the wrong day.
        """
        if not isinstance(key, str):
            raise InstrumentError(BAD_CONTRACT_KEY, f"{type(key).__name__} is not a contract key")
        parts = key.split("|")
        if len(parts) != 4:
            raise InstrumentError(
                BAD_CONTRACT_KEY,
                f"{key!r} is not TICKER|SIDE|STRIKE|EXPIRY ({len(parts)} fields)")
        ticker, side_token, strike_token, expiry_token = (part.strip() for part in parts)

        try:
            side = Side(side_token.upper())
        except ValueError:
            raise InstrumentError(
                BAD_SIDE, f"{side_token!r} in {key!r} is not CALL or PUT") from None

        if not _STRIKE_RE.fullmatch(strike_token):
            raise InstrumentError(
                BAD_STRIKE,
                f"{strike_token!r} in {key!r} is not a positive strike with at most "
                f"two decimals")
        whole, _, fraction = strike_token.partition(".")
        strike_cents = int(whole) * 100 + int(fraction.ljust(2, "0") if fraction else "0")

        upper = expiry_token.upper()
        if upper == "0DTE":
            if not _is_date(session_date):
                raise InstrumentError(
                    BAD_EXPIRY,
                    f"{key!r} is 0DTE but no session date was supplied: refusing to guess "
                    f"today's date")
            expiry = session_date
        elif _ISO_DATE_RE.fullmatch(expiry_token):
            try:
                expiry = date.fromisoformat(expiry_token)
            except ValueError as exc:
                raise InstrumentError(
                    BAD_EXPIRY, f"{expiry_token!r} in {key!r}: {exc}") from None
        elif _MONTH_DAY_RE.fullmatch(expiry_token):
            raise InstrumentError(
                BAD_EXPIRY,
                f"{expiry_token!r} is a month/day with no year: roll it in the parser, "
                f"then resolve the ISO date")
        else:
            raise InstrumentError(
                BAD_EXPIRY,
                f"{expiry_token!r} in {key!r} is not 0DTE or an ISO date")

        return cls(ticker=ticker, side=side, strike_cents=strike_cents, expiry=expiry)


@dataclass(frozen=True)
class InstrumentRef:
    """What the venue calls one contract: an id, a symbol, and its multiplier."""

    instrument_id: str
    osi_symbol: str
    multiplier: int
    venue: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise InstrumentError(
                CONTRACT_INSTRUMENT_MISSING,
                "the venue chain row named no instrument_id; an order cannot reference it")
        object.__setattr__(self, "instrument_id", self.instrument_id.strip())
        if not isinstance(self.osi_symbol, str) or len(self.osi_symbol) != OSI_LENGTH:
            raise InstrumentError(
                BAD_OSI_SYMBOL,
                f"instrument {self.instrument_id!r} has no 21-character OSI symbol "
                f"({self.osi_symbol!r})")
        # Validate through the parser so a bad symbol cannot ride along into an order.
        OptionContract.parse_osi_symbol(self.osi_symbol)
        if isinstance(self.multiplier, bool) or not isinstance(self.multiplier, int):
            raise InstrumentError(
                CONTRACT_MULTIPLIER_INVALID,
                f"instrument {self.instrument_id!r} multiplier must be an integer")
        if self.multiplier <= 0:
            raise InstrumentError(
                CONTRACT_MULTIPLIER_INVALID,
                f"instrument {self.instrument_id!r} multiplier {self.multiplier} is not positive")
        if not isinstance(self.venue, str):
            raise InstrumentError(
                CONTRACT_INSTRUMENT_MISSING,
                f"instrument {self.instrument_id!r} venue must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "osi_symbol": self.osi_symbol,
            "multiplier": self.multiplier,
            "venue": self.venue,
        }


class ContractResolver(Protocol):
    """Anything that can turn a validated contract into a venue instrument."""

    def resolve(self, contract: OptionContract) -> InstrumentRef: ...


class ChainLookup(Protocol):
    """Read one chain slice. The resolver owns the cache; this owns the data."""

    def __call__(self, ticker: str, side: Side, strike: float,
                 expiry: date) -> Sequence[Mapping[str, Any]]: ...


def _int_or_refused(value: object, reason: str, what: str) -> int:
    """An integer field from a chain row, refused rather than coerced."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InstrumentError(reason, f"{what} must be an integer, got {type(value).__name__}")
    return value


def _row_date(value: object, reason: str) -> date:
    if _is_date(value):
        return value
    if isinstance(value, str):
        text = value.strip()
        if _ISO_DATE_RE.fullmatch(text):
            try:
                return date.fromisoformat(text)
            except ValueError as exc:
                raise InstrumentError(reason, f"expiry {text!r}: {exc}") from None
    raise InstrumentError(reason, f"chain row expiry {value!r} is not an ISO date")


def _row_contract(row: Mapping[str, Any], fallback: OptionContract) -> OptionContract:
    """Parse one chain row into a contract. A malformed row refuses the resolution.

    The row may carry the OSI symbol directly (preferred: it is the venue's own
    identity), or the explicit fields. A row that carries neither a symbol nor a
    strike is not evidence about any contract.
    """
    symbol = row.get("osi_symbol", row.get("symbol"))
    if isinstance(symbol, str) and symbol.strip():
        try:
            return OptionContract.parse_osi_symbol(symbol)
        except InstrumentError as exc:
            raise InstrumentError(
                CONTRACT_CHAIN_MALFORMED, f"chain row symbol {symbol!r}: {exc}") from None

    strike_cents_raw = row.get("strike_cents")
    if strike_cents_raw is not None:
        strike_cents = _int_or_refused(strike_cents_raw, CONTRACT_CHAIN_MALFORMED,
                                       "chain row strike_cents")
        if strike_cents <= 0:
            raise InstrumentError(
                CONTRACT_CHAIN_MALFORMED, f"chain row strike_cents {strike_cents} is not positive")
    else:
        strike_raw = row.get("strike", row.get("strike_price"))
        if isinstance(strike_raw, bool) or not isinstance(strike_raw, (int, float)):
            raise InstrumentError(
                CONTRACT_CHAIN_MALFORMED,
                f"chain row has no strike (got {type(strike_raw).__name__})")
        cents = round(float(strike_raw) * 100)
        if cents <= 0:
            raise InstrumentError(
                CONTRACT_CHAIN_MALFORMED, f"chain row strike {strike_raw!r} is not positive")
        strike_cents = cents

    expiry = _row_date(
        row.get("expiry", row.get("expiration_date", row.get("expiration"))),
        CONTRACT_CHAIN_MALFORMED)
    ticker_raw = row.get("ticker", fallback.ticker)
    ticker = ticker_raw.strip().upper() if isinstance(ticker_raw, str) else ""
    if not ticker:
        raise InstrumentError(CONTRACT_CHAIN_MALFORMED, "chain row has no ticker")
    side_raw = row.get("side", row.get("type", fallback.side.value))
    if not isinstance(side_raw, str):
        raise InstrumentError(
            CONTRACT_CHAIN_MALFORMED, f"chain row side {side_raw!r} is not a string")
    try:
        side = Side(side_raw.strip().upper())
    except ValueError:
        raise InstrumentError(
            CONTRACT_CHAIN_MALFORMED, f"chain row side {side_raw!r} is not CALL or PUT") from None
    try:
        return OptionContract(ticker=ticker, side=side, strike_cents=strike_cents, expiry=expiry)
    except InstrumentError as exc:
        raise InstrumentError(CONTRACT_CHAIN_MALFORMED, f"chain row: {exc}") from None


@dataclass
class ChainContractResolver:
    """Resolve an option contract against an injected chain reader, fail-closed.

    The one dangerous shortcut this refuses is "pick the first row that looks close".
    Resolution requires *exactly one* candidate matching strike + expiry + side; two
    rows are two instruments and choosing one is a coin flip with money. Rows are
    cached per session so a single signal's entry/exit path reads the chain once, and
    a new session re-reads because a chain is a same-day snapshot, not a constant.
    """

    lookup: ChainLookup
    multipliers: frozenset[int] = DEFAULT_MULTIPLIERS
    venue: str = ""
    clock: Callable[[], float] = time.time
    cache: dict[tuple[str, str], InstrumentRef] = field(default_factory=dict)
    lookups: int = 0

    def __post_init__(self) -> None:
        if not self.multipliers:
            raise InstrumentError(
                CONTRACT_MULTIPLIER_INVALID, "the declared multiplier set is empty")
        for value in self.multipliers:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InstrumentError(
                    CONTRACT_MULTIPLIER_INVALID,
                    f"declared multiplier {value!r} is not a positive integer")

    # ── session ──────────────────────────────────────────────────────────────
    def session_key(self) -> str:
        """The cache partition: the UTC date of the injected clock."""
        return time.strftime("%Y-%m-%d", time.gmtime(self.clock()))

    def session_date(self) -> date:
        """The session date ``0DTE`` and the expired check are compared against."""
        return datetime.fromtimestamp(self.clock(), UTC).date()

    # ── resolution ───────────────────────────────────────────────────────────
    def resolve(self, contract: OptionContract) -> InstrumentRef:
        """Return the one venue instrument for ``contract`` or raise ``InstrumentError``.

        The cache is keyed by ``(session, contract key)``: within one session the
        same contract cannot become a different instrument, and across sessions it
        must be re-read (that is the difference between a cache and a guess).
        """
        key = (self.session_key(), contract.contract_key)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        ref = self._resolve_uncached(contract)
        self.cache[key] = ref
        return ref

    def _resolve_uncached(self, contract: OptionContract) -> InstrumentRef:
        session = self.session_date()
        if contract.expiry < session:
            raise InstrumentError(
                CONTRACT_EXPIRED_SAME_DAY,
                f"{contract.contract_key} expired on {contract.expiry.isoformat()} but the "
                f"session is {session.isoformat()}: a live order cannot be placed")
        rows = self.lookup(contract.ticker, contract.side, contract.strike, contract.expiry)
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise InstrumentError(
                CONTRACT_CHAIN_MALFORMED,
                f"chain lookup for {contract.contract_key} returned {type(rows).__name__}, "
                f"not a sequence of rows")
        self.lookups += 1
        candidates: list[tuple[OptionContract, Mapping[str, Any]]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise InstrumentError(
                    CONTRACT_CHAIN_MALFORMED, f"chain row {index} is not an object")
            parsed = _row_contract(row, contract)
            if (parsed.ticker != contract.ticker or parsed.side is not contract.side
                    or parsed.expiry != contract.expiry):
                continue
            candidates.append((parsed, row))
        if not candidates:
            if contract.expiry == session:
                raise InstrumentError(
                    CONTRACT_EXPIRED_SAME_DAY,
                    f"{contract.contract_key} expires today and the chain no longer lists it: "
                    f"the contract has expired intraday")
            raise InstrumentError(
                CONTRACT_MISSING,
                f"the chain has no {contract.side.value} expiring {contract.expiry.isoformat()} "
                f"for {contract.ticker}")
        matches = [item for item in candidates if item[0].strike_cents == contract.strike_cents]
        if not matches:
            listed = sorted({item[0].strike for item in candidates})
            raise InstrumentError(
                CONTRACT_STRIKE_MISMATCH,
                f"{contract.contract_key}: the chain lists strikes {listed} on that expiry")
        if len(matches) > 1:
            raise InstrumentError(
                CONTRACT_AMBIGUOUS,
                f"{len(matches)} chain rows match {contract.contract_key}; two matches are "
                f"two instruments and picking one is a guess")
        parsed, row = matches[0]
        return self._ref_from_row(row, parsed)

    def _ref_from_row(self, row: Mapping[str, Any],
                      contract: OptionContract) -> InstrumentRef:
        raw_id = row.get("instrument_id", row.get("id"))
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise InstrumentError(
                CONTRACT_INSTRUMENT_MISSING,
                f"the chain row for {contract.contract_key} has no instrument_id")
        raw_multiplier = row.get("multiplier", row.get("contract_multiplier"))
        if isinstance(raw_multiplier, bool) or not isinstance(raw_multiplier, int):
            raise InstrumentError(
                CONTRACT_MULTIPLIER_INVALID,
                f"{contract.contract_key}: chain row multiplier {raw_multiplier!r} is not an "
                f"integer; the contract cannot be priced without one")
        if raw_multiplier not in self.multipliers:
            raise InstrumentError(
                CONTRACT_MULTIPLIER_INVALID,
                f"{contract.contract_key}: multiplier {raw_multiplier} is not in the declared "
                f"set {sorted(self.multipliers)}")
        raw_venue = row.get("venue", self.venue)
        venue = raw_venue if isinstance(raw_venue, str) else self.venue
        return InstrumentRef(instrument_id=raw_id, osi_symbol=contract.osi_symbol,
                             multiplier=raw_multiplier, venue=venue)


@dataclass
class StaticContractResolver:
    """A declared contract-key -> instrument map for tests and offline runs.

    Offline runs must be able to execute the whole order path without a chain feed;
    this resolver is the declared, visible stand-in. A key it does not hold is
    ``contract_missing`` - never a synthesised instrument id.
    """

    refs: Mapping[str, InstrumentRef] = field(default_factory=dict)

    def resolve(self, contract: OptionContract) -> InstrumentRef:
        ref = self.refs.get(contract.contract_key)
        if ref is None:
            raise InstrumentError(
                CONTRACT_MISSING,
                f"the static resolver declares no instrument for {contract.contract_key}")
        return ref

    @classmethod
    def from_payload(cls, payload: Mapping[str, Mapping[str, Any]],
                      session_date: date | None = None) -> StaticContractResolver:
        """Build a resolver from a declared payload: key -> instrument fields.

        Accepted fields: ``instrument_id``/``id``, ``multiplier`` (default 100),
        ``venue``, ``osi_symbol`` (default: derived from the key). Dates in the key
        are resolved exactly like ``from_contract_key``; a defect raises
        ``InstrumentError`` with the field's named reason.
        """
        refs: dict[str, InstrumentRef] = {}
        for key, row in payload.items():
            contract = OptionContract.from_contract_key(key, session_date)
            instrument_id = row.get("instrument_id", row.get("id"))
            if not isinstance(instrument_id, str) or not instrument_id.strip():
                raise InstrumentError(
                    CONTRACT_INSTRUMENT_MISSING, f"{key!r} declares no instrument_id")
            multiplier = row.get("multiplier", 100)
            venue = row.get("venue", "")
            symbol = row.get("osi_symbol", contract.osi_symbol)
            refs[contract.contract_key] = InstrumentRef(
                instrument_id=instrument_id,
                osi_symbol=symbol if isinstance(symbol, str) else contract.osi_symbol,
                multiplier=_int_or_refused(multiplier, CONTRACT_MULTIPLIER_INVALID,
                                           f"{key!r} multiplier"),
                venue=venue if isinstance(venue, str) else "",
            )
        return cls(refs=refs)


@dataclass(frozen=True)
class CliContract:
    """The answer `contracts parse|osi` prints: a contract or a named refusal."""

    contract: OptionContract | None = None
    reason: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.contract is not None

    @property
    def osi_symbol(self) -> str:
        return self.contract.osi_symbol if self.contract is not None else ""

    def to_dict(self) -> dict[str, Any]:
        if self.contract is not None:
            return {"ok": True, **self.contract.to_dict()}
        return {"ok": False, "reason": self.reason, "detail": self.detail}


def parse_cli_contract_key(key: str, session_date: date | None = None) -> CliContract:
    """Parse a CLI contract key; a refusal is returned, never raised.

    The CLI must print the named reason (that is the whole point of the refusal
    taxonomy) instead of a traceback, so this helper converts ``InstrumentError``
    into a ``CliContract``. A caller that passed ``0DTE`` without ``--session-date``
    gets ``bad_expiry`` and a detail that says exactly that.
    """
    try:
        return CliContract(contract=OptionContract.from_contract_key(key, session_date))
    except InstrumentError as exc:
        return CliContract(reason=exc.reason, detail=exc.detail)
