"""Page a human when the hone is ill, over the same bot the desk already uses.

The fill log records everything a machine needs. This is for the case the log cannot
cover: nobody is looking at it. A hot path that stops mid-session is worse than one
that never started, because the absence of fills looks identical to a quiet market.

Deliberately small and boring:
  * fail closed - no token or chat id means no page and a non-zero result, never a
    page to some default chat;
  * the token is redacted from every error string that can leave this module;
  * 429 honours Telegram's retry_after, 5xx retries once, 4xx never retries;
  * long text is chunked on line boundaries, because a silent truncation is a lie.

Read-only: it sends messages. It never calls getUpdates, so it cannot consume a
command channel, and it has no access to the broker.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Protocol

API = "https://api.telegram.org"
MAX_CHUNK = 4000            # Telegram's hard limit is 4096; leave room for the split
MAX_RETRIES = 2


class PagerError(Exception):
    """A page could not be delivered. Never carries the token."""


class Transport(Protocol):
    def __call__(self, url: str, payload: dict, timeout: float) -> tuple[int, dict]:
        """-> (http_status, parsed_body)."""


@dataclass(frozen=True)
class PageResult:
    ok: bool
    chunks: int = 0
    message_ids: tuple[int, ...] = ()
    error: str = ""
    dry_run: bool = False
    attempts: int = 0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "chunks": self.chunks, "message_ids": list(self.message_ids),
                "error": self.error, "dry_run": self.dry_run, "attempts": self.attempts}


def redact(text: str, secret: str) -> str:
    """Never let a token reach a log line, a page, or an exception message."""
    if not secret:
        return text
    return text.replace(secret, "<redacted>")


def chunk_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split on line boundaries, never mid-line, never dropping a character."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        while len(line) > limit:                      # one pathological line
            if current:
                chunks.append("".join(current))
                current, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) > limit and current:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    assert "".join(chunks) == text, "chunking must be lossless"
    return chunks


def _urllib_transport(url: str, payload: dict, timeout: float) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:            # 4xx/5xx still carry a body
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"description": raw[:200]}
    except urllib.error.URLError as exc:
        raise PagerError(f"network: {exc.reason}") from None


@dataclass
class TelegramPager:
    token: str = ""
    chat_id: str = ""
    transport: Transport = _urllib_transport
    timeout: float = 15.0
    sleep: Callable[[float], None] = time.sleep

    @classmethod
    def from_env(cls, env: dict | None = None, **kwargs) -> "TelegramPager":
        e = os.environ if env is None else env
        return cls(token=e.get("MFF_TELEGRAM_TOKEN", e.get("TELEGRAM_BOT_TOKEN", "")),
                   chat_id=e.get("MFF_TELEGRAM_CHAT", e.get("TELEGRAM_CHAT_ID", "")), **kwargs)

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, *, dry_run: bool = False) -> PageResult:
        if dry_run:
            return PageResult(ok=True, chunks=len(chunk_text(text)), dry_run=True)
        if not self.token:
            return PageResult(ok=False, error="no token (MFF_TELEGRAM_TOKEN / TELEGRAM_BOT_TOKEN)")
        if not self.chat_id:
            return PageResult(ok=False, error="no chat id (MFF_TELEGRAM_CHAT / TELEGRAM_CHAT_ID)")

        url = f"{API}/bot{self.token}/sendMessage"
        ids: list[int] = []
        chunks = chunk_text(text)
        attempts = 0
        for chunk in chunks:
            payload = {"chat_id": self.chat_id, "text": chunk,
                       "disable_web_page_preview": True}
            delivered = False
            for attempt in range(MAX_RETRIES + 1):
                attempts += 1
                try:
                    status, body = self.transport(url, payload, self.timeout)
                except PagerError as exc:
                    return PageResult(ok=False, chunks=len(ids), message_ids=tuple(ids),
                                      error=redact(str(exc), self.token), attempts=attempts)
                if status == 200 and body.get("ok"):
                    result = body.get("result") or {}
                    if isinstance(result.get("message_id"), int):
                        ids.append(result["message_id"])
                    delivered = True
                    break
                description = redact(str(body.get("description") or f"http {status}"), self.token)
                if status == 429 and attempt < MAX_RETRIES:
                    retry_after = (body.get("parameters") or {}).get("retry_after", 1)
                    self.sleep(min(float(retry_after), 30.0))
                    continue
                if 500 <= status < 600 and attempt < MAX_RETRIES:
                    self.sleep(1.0)
                    continue
                # 4xx (or a 200 that says ok:false) is not worth retrying.
                return PageResult(ok=False, chunks=len(ids), message_ids=tuple(ids),
                                  error=f"{description} (http {status})", attempts=attempts)
            if not delivered:
                return PageResult(ok=False, chunks=len(ids), message_ids=tuple(ids),
                                  error="not delivered", attempts=attempts)
        return PageResult(ok=True, chunks=len(ids), message_ids=tuple(ids), attempts=attempts)
