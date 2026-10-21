"""The pager and the watchdog.

Two things are being defended here: the token must never leak into an error string,
and a failing page must never look like a successful one.
"""

from __future__ import annotations

import json

import pytest

from scorpion_mff.page import PageResult, TelegramPager, chunk_text, redact
from scorpion_mff.watch import assess, run_watch

TOKEN = "123456:AAH-test-token-not-real"
CHAT = "5655445905"


class FakeTransport:
    """Records every call; replies from a script of (status, body) responses."""

    def __init__(self, script=None):
        self.calls: list[tuple[str, dict]] = []
        self.script = list(script or [(200, {"ok": True, "result": {"message_id": 1}})])
        self.slept: list[float] = []

    def __call__(self, url, payload, timeout):
        self.calls.append((url, payload))
        status, body = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return status, body


def pager(transport, token: str = TOKEN, chat: str = CHAT) -> TelegramPager:
    return TelegramPager(token=token, chat_id=chat, transport=transport, sleep=lambda s: None)


# ── chunking: lossless, on line boundaries ───────────────────────────────────
def test_short_text_is_one_chunk():
    assert chunk_text("hello") == ["hello"]


def test_long_text_splits_losslessly_on_lines():
    text = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(200))
    chunks = chunk_text(text, limit=500)
    assert len(chunks) > 1
    assert "".join(chunks) == text, "a split must never lose or reorder a character"
    assert all(len(c) <= 500 for c in chunks)


def test_a_single_pathological_line_is_split_not_truncated():
    chunks = chunk_text("y" * 1200, limit=500)
    assert "".join(chunks) == "y" * 1200


# ── the token can never escape ───────────────────────────────────────────────
def test_redact_removes_the_token():
    assert TOKEN not in redact(f"failed for bot{TOKEN}/sendMessage", TOKEN)


def test_a_transport_error_carries_no_token():
    class Boom:
        def __call__(self, url, payload, timeout):
            from scorpion_mff.page import PagerError
            raise PagerError(f"network down calling {url}")

    result = pager(Boom()).send("hello")
    assert result.ok is False
    assert TOKEN not in result.error


def test_a_4xx_body_mentioning_the_token_is_redacted():
    transport = FakeTransport([(400, {"ok": False, "description": f"bad token {TOKEN}"})])
    result = pager(transport).send("hello")
    assert result.ok is False and TOKEN not in result.error


# ── fail closed ──────────────────────────────────────────────────────────────
def test_no_token_sends_nothing():
    transport = FakeTransport()
    result = pager(transport, token="").send("hello")
    assert result.ok is False and result.error.startswith("no token")
    assert transport.calls == [], "an unconfigured pager must not dial out"


def test_no_chat_sends_nothing():
    transport = FakeTransport()
    result = pager(transport, chat="").send("hello")
    assert result.ok is False and result.error.startswith("no chat id")
    assert transport.calls == []


def test_dry_run_sends_nothing_and_reports_success():
    transport = FakeTransport()
    result = pager(transport).send("hello", dry_run=True)
    assert result.ok and result.dry_run and transport.calls == []


# ── retry behaviour ─────────────────────────────────────────────────────────
def test_429_honours_retry_after_and_then_succeeds():
    transport = FakeTransport([
        (429, {"ok": False, "description": "Too Many Requests",
               "parameters": {"retry_after": 2}}),
        (200, {"ok": True, "result": {"message_id": 77}}),
    ])
    slept: list[float] = []
    p = TelegramPager(token=TOKEN, chat_id=CHAT, transport=transport, sleep=slept.append)
    result = p.send("hello")
    assert result.ok and result.message_ids == (77,)
    assert slept == [2.0], "retry_after must be honoured, not a fixed backoff"


def test_4xx_is_not_retried():
    transport = FakeTransport([(403, {"ok": False, "description": "bot was blocked"})])
    result = pager(transport).send("hello")
    assert result.ok is False
    assert len(transport.calls) == 1, "a 4xx will not become a 200 by trying again"


def test_5xx_is_retried_once_then_gives_up():
    transport = FakeTransport([(502, {"ok": False, "description": "bad gateway"})])
    result = pager(transport).send("hello")
    assert result.ok is False
    assert len(transport.calls) == 3, "1 attempt + 2 retries"


def test_multi_chunk_send_reports_every_message_id():
    transport = FakeTransport([(200, {"ok": True, "result": {"message_id": 5}})])
    text = "\n".join(f"line {i} " + "z" * 60 for i in range(150))
    result = pager(transport).send(text)
    assert result.ok and result.chunks == len(transport.calls) > 1


# ── the watchdog's judgement ────────────────────────────────────────────────
def _write_heartbeat(path, ts: float, beats: int = 3) -> None:
    path.write_text(json.dumps({"ts": ts, "beats": beats, "note": ""}), encoding="utf-8")


def _fill(ts: float, event: str, reason: str = "") -> str:
    return json.dumps({"ts": ts, "event": event, "reason": reason, "contract_key": "QQQ|CALL|700|0DTE"})


def test_no_heartbeat_is_down(tmp_path):
    health = assess(now=1_000_000.0, heartbeat_path=tmp_path / "hb.json",
                    fills_path=tmp_path / "f.jsonl", kill_path=tmp_path / "NO_TRADE")
    assert health.status == "down" and not health.healthy
    assert "has not run here" in health.problems[0]


def test_an_old_heartbeat_is_stale(tmp_path):
    path = tmp_path / "hb.json"
    now = 1_000_000.0
    _write_heartbeat(path, now - 600)
    health = assess(now=now, heartbeat_path=path, fills_path=tmp_path / "f.jsonl",
                    kill_path=tmp_path / "NO_TRADE", stale_after_s=120)
    assert health.status == "stale"
    assert health.heartbeat_age_s == pytest.approx(600)


def test_a_fresh_heartbeat_with_no_alerts_is_healthy(tmp_path):
    path = tmp_path / "hb.json"
    now = 1_000_000.0
    _write_heartbeat(path, now - 5)
    health = assess(now=now, heartbeat_path=path, fills_path=tmp_path / "f.jsonl",
                    kill_path=tmp_path / "NO_TRADE")
    assert health.status == "ok" and health.healthy


def test_an_engine_alert_makes_it_unhealthy(tmp_path):
    path = tmp_path / "hb.json"
    fills = tmp_path / "f.jsonl"
    now = 1_000_000.0
    _write_heartbeat(path, now - 5)
    fills.write_text("\n".join([
        _fill(now - 100, "buy", "entry:pipe"),
        _fill(now - 50, "alert", "ambiguous_exit:named_no_contract"),
    ]) + "\n", encoding="utf-8")
    health = assess(now=now, heartbeat_path=path, fills_path=fills,
                    kill_path=tmp_path / "NO_TRADE")
    assert health.status == "alerting"
    assert health.alerts_today == 1 and health.fills_today == 1
    assert "ambiguous_exit" in health.last_alert


def test_yesterdays_fills_do_not_count_today(tmp_path):
    path = tmp_path / "hb.json"
    fills = tmp_path / "f.jsonl"
    now = 1_000_000.0
    _write_heartbeat(path, now - 5)
    fills.write_text(_fill(now - 3 * 86_400, "alert", "ambiguous_exit:x") + "\n", encoding="utf-8")
    health = assess(now=now, heartbeat_path=path, fills_path=fills,
                    kill_path=tmp_path / "NO_TRADE")
    assert health.status == "ok" and health.alerts_today == 0


def test_a_corrupt_fill_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "hb.json"
    fills = tmp_path / "f.jsonl"
    now = 1_000_000.0
    _write_heartbeat(path, now - 5)
    fills.write_text("{not json}\n" + _fill(now - 10, "buy") + "\n", encoding="utf-8")
    health = assess(now=now, heartbeat_path=path, fills_path=fills,
                    kill_path=tmp_path / "NO_TRADE")
    assert health.status == "ok" and health.fills_today == 1


def test_the_kill_switch_is_reported_but_is_not_a_fault(tmp_path):
    path = tmp_path / "hb.json"
    now = 1_000_000.0
    _write_heartbeat(path, now - 5)
    kill = tmp_path / "NO_TRADE"
    kill.write_text("stop\n", encoding="utf-8")
    health = assess(now=now, heartbeat_path=path, fills_path=tmp_path / "f.jsonl",
                    kill_path=kill)
    assert health.status == "ok" and health.kill_switch is True
    assert "ARMED" in health.problems[0]


# ── the whole loop ──────────────────────────────────────────────────────────
def test_run_watch_pages_when_ill(tmp_path):
    transport = FakeTransport()
    health, result = run_watch(pager=pager(transport), now=1_000_000.0,
                               heartbeat_path=tmp_path / "hb.json",
                               fills_path=tmp_path / "f.jsonl",
                               kill_path=tmp_path / "NO_TRADE")
    assert health.status == "down"
    assert result is not None and result.ok
    sent = transport.calls[0][1]["text"]
    assert "DOWN" in sent and "nothing was traded" in sent.lower()


def test_run_watch_stays_silent_when_healthy(tmp_path):
    transport = FakeTransport()
    path = tmp_path / "hb.json"
    now = 1_000_000.0
    _write_heartbeat(path, now - 5)
    health, result = run_watch(pager=pager(transport), now=now, heartbeat_path=path,
                               fills_path=tmp_path / "f.jsonl", kill_path=tmp_path / "NO_TRADE")
    assert health.healthy and result is None
    assert transport.calls == []


def test_the_page_is_short_enough_for_a_phone(tmp_path):
    now = 1_000_000.0
    health = assess(now=now, heartbeat_path=tmp_path / "hb.json",
                    fills_path=tmp_path / "f.jsonl", kill_path=tmp_path / "NO_TRADE")
    text = health.page_text()
    assert len(text) < 1000 and "SCORPION MFF" in text


def test_page_result_serialises_without_a_token():
    result = PageResult(ok=True, chunks=1, message_ids=(9,))
    assert json.dumps(result.to_dict())
    assert TOKEN not in json.dumps(result.to_dict())
