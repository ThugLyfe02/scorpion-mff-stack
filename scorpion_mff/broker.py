"""Order placement: a Broker protocol, a deterministic paper broker, and the
Robinhood Agentic HTTP adapter.

Hard rules honoured here:
  * no shell, ever - urllib only, argv never carries remote text
  * dry_run defaults to TRUE; live trading is an explicit opt-in
  * the access token is read from the environment and never logged, echoed in
    an error, or written to the fill log
  * every order carries an Idempotency-Key so a retry cannot double-fill
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .types import OrderIntent, Quote


class BrokerError(RuntimeError):
    """Placement failed. The message is always token-free."""


class TransportError(BrokerError):
    """The HTTP transport could not reach the broker. Token-free by construction."""


class OrderLookup(StrEnum):
    """Tri-state answer to 'did this idempotency key ever reach the venue?'."""

    SEEN = "seen"              # the venue has the order
    NOT_SEEN = "not_seen"      # the venue never saw the order
    CANNOT_SAY = "cannot_say"  # the lookup could not decide; the row stays open


class OrderLookupProvider(Protocol):
    """Anything that can say whether an idempotency key reached the venue."""

    def lookup(self, idempotency_key: str) -> OrderLookup: ...


def intent_key(intent: OrderIntent) -> str:
    """The effective idempotency key: the explicit one, else contract:action."""
    return intent.idempotency_key or f"{intent.contract_key}:{intent.action}"


@dataclass(frozen=True)
class Ack:
    ok: bool
    contract_key: str
    qty: int
    price: float
    broker_order_id: str = ""
    error: str = ""
    dry_run: bool = False


class QuoteProvider(Protocol):
    def quote(self, contract_key: str) -> Quote | None: ...


@dataclass
class QuoteScript:
    """Deterministic quotes for replay and tests.

    script = {"QQQ|CALL|719|0DTE": [(1.01, 1.05), (1.11, 1.14)]}
    Each call to quote() consumes the next entry, then sticks on the last one -
    a missing quote must never be invented, and must never silently repeat.
    """

    script: dict[str, list[tuple[float, float]]]
    _idx: dict[str, int] = field(default_factory=dict)

    def quote(self, contract_key: str) -> Quote | None:
        series = self.script.get(contract_key)
        if not series:
            return None
        i = min(self._idx.get(contract_key, 0), len(series) - 1)
        self._idx[contract_key] = i + 1
        bid, ask = series[i]
        return Quote(bid=bid, ask=ask)


@dataclass
class StaticQuotes:
    """One fixed quote per contract, for the simple cases."""

    prices: dict[str, tuple[float, float]]

    def quote(self, contract_key: str) -> Quote | None:
        pair = self.prices.get(contract_key)
        return Quote(bid=pair[0], ask=pair[1]) if pair else None


@dataclass
class PaperBroker:
    """Fills at the limit price. Optional slippage for honest replays.

    The lookup is deterministic: SEEN for a key this broker actually placed,
    NOT_SEEN for anything else - there is no transport to be unsure about.
    """

    slippage: float = 0.0
    acks: list[Ack] = field(default_factory=list)
    placed_keys: set[str] = field(default_factory=set)

    def place(self, intent: OrderIntent) -> Ack:
        if intent.qty <= 0:
            raise BrokerError(f"refusing a {intent.qty}-contract order")
        if intent.action == "buy":
            price = intent.limit * (1 + self.slippage)
        elif intent.action == "sell":
            price = intent.limit * (1 - self.slippage)
        else:
            raise BrokerError(f"unknown action {intent.action!r}")
        price = round(max(0.01, price), 4)
        self.placed_keys.add(intent_key(intent))
        ack = Ack(ok=True, contract_key=intent.contract_key, qty=intent.qty,
                  price=price, broker_order_id=f"paper-{len(self.acks) + 1}")
        self.acks.append(ack)
        return ack

    def lookup(self, idempotency_key: str) -> OrderLookup:
        return (OrderLookup.SEEN if idempotency_key in self.placed_keys
                else OrderLookup.NOT_SEEN)


def mask(secret: str) -> str:
    """Never let a token reach a log line."""
    if not secret:
        return "(unset)"
    return f"{secret[:4]}...{secret[-2:]}" if len(secret) > 8 else "***"


@dataclass
class RobinhoodAgenticBroker:
    """POSTs orders to the Robinhood Agentic endpoint.

    `transport` is injectable so the HTTP shape, the idempotency key and the
    retry ladder are tested against a real local server rather than mocked away.
    """

    base_url: str = field(default_factory=lambda: os.environ.get(
        "MFF_RH_BASE", "https://api.robinhood.com"))
    token: str = field(default_factory=lambda: os.environ.get("MFF_RH_TOKEN", ""))
    account: str = field(default_factory=lambda: os.environ.get("MFF_RH_ACCOUNT", "agentic"))
    lookup_template: str = field(default_factory=lambda: os.environ.get(
        "MFF_RH_ORDER_LOOKUP_TEMPLATE", ""))
    dry_run: bool = True
    timeout_s: float = 5.0
    retries: int = 3
    backoff_s: float = 0.5
    transport: Callable[[urllib.request.Request, float], tuple[int, dict[str, Any]]] | None = None
    sleep: Callable[[float], None] = time.sleep
    acks: list[Ack] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.dry_run and not self.token:
            raise BrokerError("live mode needs MFF_RH_TOKEN; refusing to run dry-armed")

    # ── public ───────────────────────────────────────────────────────────────
    def place(self, intent: OrderIntent) -> Ack:
        if intent.qty <= 0:
            raise BrokerError(f"refusing a {intent.qty}-contract order")
        if self.dry_run:
            ack = Ack(ok=True, contract_key=intent.contract_key, qty=intent.qty,
                      price=intent.limit, broker_order_id=f"dry-{len(self.acks) + 1}",
                      dry_run=True)
            self.acks.append(ack)
            return ack

        request = self._build_request(intent)
        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                status, body = self._send(request)
            except TransportError as exc:
                # Preserve the classification: the listener's circuit breaker
                # must see this as TRANSPORT (urllib/timeout/OSError), not as a
                # business reject.
                raise TransportError(
                    f"{intent.action} {intent.qty} {intent.contract_key}: {exc}"
                ) from exc
            if status == 0:
                # A transport-shaped response (an injected transport or a wrapped
                # connection failure): no HTTP status means the venue was never
                # reached, which is transport, not a business refusal.
                raise TransportError(
                    f"{intent.action} {intent.qty} {intent.contract_key}: "
                    f"transport error: {body.get('error', 'unknown')}"
                )
            if 200 <= status < 300:
                ack = Ack(ok=True, contract_key=intent.contract_key, qty=intent.qty,
                          price=float(body.get("average_price") or intent.limit),
                          broker_order_id=str(body.get("id") or ""))
                self.acks.append(ack)
                return ack
            last_error = self._describe(status, body)
            if (status in (429,) or 500 <= status < 600) and attempt < self.retries:
                self.sleep(self._retry_after(body, attempt))
                continue
            break
        raise BrokerError(f"{intent.action} {intent.qty} {intent.contract_key}: {last_error}")

    def lookup(self, idempotency_key: str) -> OrderLookup:
        """SEEN/NOT_SEEN/CANNOT_SAY from MFF_RH_ORDER_LOOKUP_TEMPLATE only.

        No template -> CANNOT_SAY without a network call. The template's `{key}`
        placeholder is URL-quoted; the response decides the result (see
        `_interpret_lookup`). A transport failure is CANNOT_SAY, never a guess.
        """
        if not self.lookup_template:
            return OrderLookup.CANNOT_SAY
        url = self.lookup_template.replace("{key}", urllib.parse.quote(idempotency_key))
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        try:
            status, body = self._send(req)
        except TransportError:
            return OrderLookup.CANNOT_SAY
        return self._interpret_lookup(status, body)

    @staticmethod
    def _interpret_lookup(status: int, body: dict[str, Any]) -> OrderLookup:
        """A lookup response decides the tri-state; an ambiguous one cannot say."""
        if status == 404:
            return OrderLookup.NOT_SEEN
        if not (200 <= status < 300):
            return OrderLookup.CANNOT_SAY
        found = body.get("found")
        if isinstance(found, bool):
            return OrderLookup.SEEN if found else OrderLookup.NOT_SEEN
        results = body.get("results")
        if isinstance(results, list):
            return OrderLookup.SEEN if results else OrderLookup.NOT_SEEN
        if body.get("id") or body.get("order_id") or body.get("order"):
            return OrderLookup.SEEN
        return OrderLookup.CANNOT_SAY

    # ── internals ────────────────────────────────────────────────────────────
    def _build_request(self, intent: OrderIntent) -> urllib.request.Request:
        payload = {
            "account": self.account,
            "contract_key": intent.contract_key,
            "side": intent.action,
            "quantity": intent.qty,
            "limit_price": round(intent.limit, 2),
            "time_in_force": "gtc",
            "reason": intent.reason[:120],
        }
        idempotency_key = intent_key(intent)
        req = urllib.request.Request(
            url=f"{self.base_url.rstrip('/')}/orders",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Idempotency-Key": idempotency_key,
            },
        )
        return req

    def _send(self, request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
        if self.transport is not None:
            return self.transport(request, self.timeout_s)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"raw": raw[:200]}
            return exc.code, body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"transport error: {str(exc)[:200]}") from exc

    @staticmethod
    def _retry_after(body: dict[str, Any], attempt: int) -> float:
        try:
            return max(0.0, float(body.get("retry_after") or 0.0)) or attempt * 0.5
        except (TypeError, ValueError):
            return attempt * 0.5

    def _describe(self, status: int, body: dict[str, Any]) -> str:
        if status == 0:
            return f"transport error: {body.get('error', 'unknown')}"
        detail = body.get("detail") or body.get("error") or body.get("raw") or ""
        return f"HTTP {status}" + (f": {str(detail)[:160]}" if detail else "")


def jitter(base: float, spread: float = 0.25) -> float:
    """Backoff helper for the live loop; deterministic when spread=0."""
    return base * (1 + (random.random() * spread if spread else 0))


class QuoteError(RuntimeError):
    """The quote feed could not price a contract. The engine fails closed."""


@dataclass
class HttpQuoteProvider:
    """Bid/ask for a contract key from a URL template YOU supply.

    Deliberately not hard-coded to a vendor schema: the Robinhood options-quote
    response shape is not invented here. You set

        MFF_QUOTE_URL_TEMPLATE=https://.../quotes?symbol={key}
        MFF_QUOTE_BID_PATH=results.0.bid_price
        MFF_QUOTE_ASK_PATH=results.0.ask_price

    and this reads those dotted paths out of the JSON. Unconfigured, `quote()`
    raises QuoteError and the engine rejects the entry with `no_quote` - an
    unpriceable signal must never become a guessed limit price.
    """

    template: str = field(default_factory=lambda: os.environ.get("MFF_QUOTE_URL_TEMPLATE", ""))
    bid_path: str = field(default_factory=lambda: os.environ.get("MFF_QUOTE_BID_PATH", "bid"))
    ask_path: str = field(default_factory=lambda: os.environ.get("MFF_QUOTE_ASK_PATH", "ask"))
    token: str = field(default_factory=lambda: os.environ.get("MFF_RH_TOKEN", ""))
    timeout_s: float = 3.0
    cache_s: float = 5.0
    transport: Callable[[urllib.request.Request, float], tuple[int, dict[str, Any]]] | None = None
    _cache: dict[str, tuple[float, Quote]] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.template)

    def quote(self, contract_key: str) -> Quote | None:
        now = time.time()
        cached = self._cache.get(contract_key)
        if cached and now - cached[0] <= self.cache_s:
            return cached[1]
        if not self.template:
            raise QuoteError("no quote feed configured (set MFF_QUOTE_URL_TEMPLATE)")
        url = self.template.replace("{key}", urllib.parse.quote(contract_key))
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        status, body = self._send(req)
        if not (200 <= status < 300):
            raise QuoteError(f"quote feed HTTP {status}")
        bid = _dig(body, self.bid_path)
        ask = _dig(body, self.ask_path)
        if bid is None or ask is None:
            raise QuoteError(f"quote fields missing ({self.bid_path}/{self.ask_path})")
        quote = Quote(bid=float(bid), ask=float(ask))
        self._cache[contract_key] = (now, quote)
        return quote

    def _send(self, request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
        if self.transport is not None:
            return self.transport(request, self.timeout_s)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            return exc.code, {}
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0, {}


def _dig(payload: Any, dotted: str) -> Any:
    """Read 'results.0.bid_price' out of nested dicts/lists. None when absent."""
    cur = payload
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur
