"""Broker tests. The HTTP path is tested against a REAL local server, not a mock
of our own making, so the request shape, the idempotency key and the retry ladder
are exercised for real. The token must never appear in an error.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scorpion_mff.broker import (
    BrokerError,
    HttpQuoteProvider,
    PaperBroker,
    QuoteError,
    RobinhoodAgenticBroker,
    mask,
)
from scorpion_mff.types import OrderIntent

TOKEN = "secret-token-value-1234567890"


class Recorder(BaseHTTPRequestHandler):
    """Answers with a scripted list of (status, body) and records every request."""

    script: list[tuple[int, dict]] = []
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        status, payload = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # silence the default stderr logging
        return


@pytest.fixture()
def server():
    Recorder.requests = []
    Recorder.script = [(200, {"id": "abc123", "average_price": 1.06})]
    srv = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", Recorder
    finally:
        srv.shutdown()


def intent(**kw) -> OrderIntent:
    base = dict(action="buy", qty=5, limit=1.06, contract_key="NVDA|PUT|227.5|2026-09-11",
                reason="entry:standard", idempotency_key="NVDA|PUT|227.5|2026-09-11:buy:2")
    base.update(kw)
    return OrderIntent(**base)


# ── paper ────────────────────────────────────────────────────────────────────
def test_paper_broker_fills_at_the_limit():
    ack = PaperBroker().place(intent())
    assert ack.ok and ack.qty == 5 and ack.price == 1.06


def test_paper_broker_applies_slippage_in_the_adverse_direction():
    buy = PaperBroker(slippage=0.01).place(intent(action="buy", limit=1.00))
    sell = PaperBroker(slippage=0.01).place(intent(action="sell", limit=1.00))
    assert buy.price > 1.00 and sell.price < 1.00


@pytest.mark.parametrize("qty", [0, -3])
def test_paper_broker_refuses_a_nonsense_quantity(qty):
    with pytest.raises(BrokerError):
        PaperBroker().place(intent(qty=qty))


# ── robinhood agentic ────────────────────────────────────────────────────────
def test_dry_run_is_the_default_and_places_nothing():
    broker = RobinhoodAgenticBroker(token="")          # no token needed when dry
    ack = broker.place(intent())
    assert ack.ok and ack.dry_run and not ack.broker_order_id.startswith("abc")


def test_live_without_a_token_refuses_to_construct():
    with pytest.raises(BrokerError):
        RobinhoodAgenticBroker(token="", dry_run=False)


def test_live_order_shape_and_idempotency(server):
    url, rec = server
    broker = RobinhoodAgenticBroker(base_url=url, token=TOKEN, dry_run=False)
    ack = broker.place(intent())

    assert ack.ok and ack.broker_order_id == "abc123" and ack.price == 1.06
    assert len(rec.requests) == 1
    sent = rec.requests[0]
    assert sent["path"] == "/orders"
    assert sent["body"]["contract_key"] == "NVDA|PUT|227.5|2026-09-11"
    assert sent["body"]["quantity"] == 5 and sent["body"]["side"] == "buy"
    assert sent["body"]["limit_price"] == 1.06
    assert sent["headers"]["Idempotency-Key"] == intent().idempotency_key
    assert sent["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_retry_on_5xx_then_success(server):
    url, rec = server
    rec.script = [(500, {"detail": "temporary"}), (200, {"id": "retry-ok", "average_price": 1.07})]
    broker = RobinhoodAgenticBroker(base_url=url, token=TOKEN, dry_run=False,
                                    backoff_s=0, sleep=lambda _s: None)
    ack = broker.place(intent())
    assert ack.ok and ack.broker_order_id == "retry-ok"
    assert len(rec.requests) == 2


def test_retry_on_429_honours_retry_after(server):
    url, rec = server
    rec.script = [(429, {"retry_after": 0.01}), (200, {"id": "flood-ok", "average_price": 1.05})]
    slept: list[float] = []
    broker = RobinhoodAgenticBroker(base_url=url, token=TOKEN, dry_run=False,
                                    sleep=slept.append)
    ack = broker.place(intent())
    assert ack.ok and slept == [0.01]


def test_a_4xx_is_not_retried(server):
    url, rec = server
    rec.script = [(400, {"detail": "insufficient buying power"})]
    broker = RobinhoodAgenticBroker(base_url=url, token=TOKEN, dry_run=False,
                                    sleep=lambda _s: None)
    with pytest.raises(BrokerError) as exc:
        broker.place(intent())
    assert len(rec.requests) == 1
    assert "insufficient buying power" in str(exc.value)


def test_the_token_never_leaks_into_an_error(server):
    url, rec = server
    rec.script = [(500, {"detail": "boom"})]
    broker = RobinhoodAgenticBroker(base_url=url, token=TOKEN, dry_run=False,
                                    retries=1, sleep=lambda _s: None)
    with pytest.raises(BrokerError) as exc:
        broker.place(intent())
    assert TOKEN not in str(exc.value)
    assert mask(TOKEN).split("...")[0] in mask(TOKEN)


def test_transport_error_becomes_a_clean_broker_error():
    broker = RobinhoodAgenticBroker(base_url="http://127.0.0.1:9", token=TOKEN,
                                    dry_run=False, retries=1, timeout_s=0.2,
                                    sleep=lambda _s: None)
    with pytest.raises(BrokerError) as exc:
        broker.place(intent())
    assert "transport error" in str(exc.value)
    assert TOKEN not in str(exc.value)


# ── quote provider ───────────────────────────────────────────────────────────
def test_quote_provider_refuses_when_unconfigured():
    with pytest.raises(QuoteError):
        HttpQuoteProvider(template="").quote("QQQ|CALL|719|0DTE")


def test_quote_provider_reads_dotted_paths():
    def transport(request, timeout):
        return 200, {"results": [{"bid_price": "1.01", "ask_price": "1.05"}]}

    provider = HttpQuoteProvider(template="http://x/{key}", bid_path="results.0.bid_price",
                                 ask_path="results.0.ask_price", transport=transport)
    quote = provider.quote("QQQ|CALL|719|0DTE")
    assert quote is not None and quote.bid == 1.01 and quote.ask == 1.05


def test_quote_provider_raises_when_the_fields_are_missing():
    provider = HttpQuoteProvider(template="http://x/{key}", transport=lambda r, t: (200, {}))
    with pytest.raises(QuoteError):
        provider.quote("QQQ|CALL|719|0DTE")


def test_quote_provider_caches_within_the_window():
    calls: list[str] = []

    def transport(request, timeout):
        calls.append(request.full_url)
        return 200, {"bid": 1.0, "ask": 1.1}

    provider = HttpQuoteProvider(template="http://x/{key}", transport=transport, cache_s=60)
    provider.quote("K")
    provider.quote("K")
    assert len(calls) == 1
