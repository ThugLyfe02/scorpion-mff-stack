from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from .config import ALLOWED_CHANNEL_IDS, EXCLUDED_TICKERS, GUILD_ID, MARKET_TZ
from .domain import EventKind, RawDiscordMessage, SignalEvent

PARSER_VERSION = "v1"

ENTRY_RE = re.compile(
    r"\b(?P<ticker>[A-Z]{1,6})\b.*?"
    r"(?P<strike>\d+(?:\.\d+)?)\s*(?P<cp>C|P|CALL|PUT)\b"
    r".*?(?:@|at|bid|premium)?\s*\$?(?P<price>\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
ENTRY_ALT_RE = re.compile(
    r"\b(?P<cp>CALL|PUT)\b.*?\b(?P<ticker>[A-Z]{1,6})\b.*?"
    r"\$?(?P<strike>\d+(?:\.\d+)?)\b.*?"
    r"(?:@|at|bid|premium)?\s*\$?(?P<price>\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
PCT_RE = re.compile(r"(?P<pct>[+-]?\d+(?:\.\d+)?)\s*%")
EXPIRY_ISO_RE = re.compile(r"\b(?P<y>20\d{2})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})\b")
EXPIRY_MD_RE = re.compile(
    r"\b(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(?P<d>\d{1,2})(?:,\s*(?P<y>20\d{2}))?\b",
    re.IGNORECASE,
)

ADD_WORDS = ("added", "adding", "avg", "average", "averaged")
TRIM_WORDS = ("trim", "take some", "close half", "sold half", "lock some", "scale some")
EXIT_WORDS = ("all out", "full close", "closing runners", "close runners", "sold runners", "closed")
STOP_WORDS = ("stop loss", "stopped", "stop hit", "out at -")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _event_id(raw: RawDiscordMessage, kind: EventKind) -> str:
    payload = f"{raw.guild_id}|{raw.channel_id}|{raw.message_id}|{kind.value}|{PARSER_VERSION}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _base(raw: RawDiscordMessage, kind: EventKind, reason: str = "") -> SignalEvent:
    return SignalEvent(
        event_id=_event_id(raw, kind),
        message_id=raw.message_id,
        kind=kind,
        channel_id=raw.channel_id,
        author_id=raw.author_id,
        source_ts_utc=raw.source_ts_utc,
        received_ts_utc=raw.received_ts_utc,
        raw_text=raw.content,
        parser_version=PARSER_VERSION,
        reason=reason,
    )


def _parse_expiry(text: str, source_ts_utc: datetime) -> date | None:
    lower = text.lower()
    source_date = source_ts_utc.astimezone(MARKET_TZ).date()
    if "0dte" in lower or "today" in lower:
        return source_date
    m = EXPIRY_ISO_RE.search(text)
    if m:
        return date(int(m["y"]), int(m["m"]), int(m["d"]))
    m = EXPIRY_MD_RE.search(text)
    if m:
        year = int(m["y"]) if m["y"] else source_date.year
        month = _MONTHS[m["mon"][:3].lower()]
        candidate = date(year, month, int(m["d"]))
        if not m["y"] and candidate < source_date - timedelta(days=30):
            candidate = date(year + 1, month, int(m["d"]))
        return candidate
    return None


def _side(cp: str) -> str:
    return "CALL" if cp.upper() in {"C", "CALL"} else "PUT"


def parse_message(raw: RawDiscordMessage, allowed_author_ids: frozenset[str] | None = None) -> SignalEvent:
    if raw.guild_id != GUILD_ID:
        return _base(raw, EventKind.IGNORE, "wrong_guild")
    if raw.channel_id not in ALLOWED_CHANNEL_IDS:
        return _base(raw, EventKind.IGNORE, "channel_not_allowed")
    if allowed_author_ids is not None and raw.author_id not in allowed_author_ids:
        return _base(raw, EventKind.IGNORE, "author_not_allowed")

    text = " ".join(raw.content.split())
    lower = text.lower()

    entry_match = ENTRY_RE.search(text) or ENTRY_ALT_RE.search(text)
    if entry_match:
        ticker = entry_match["ticker"].upper()
        if ticker in EXCLUDED_TICKERS:
            return replace(_base(raw, EventKind.IGNORE, "excluded_ticker"), ticker=ticker)
        try:
            strike = Decimal(entry_match["strike"])
            price = Decimal(entry_match["price"])
        except InvalidOperation:
            return _base(raw, EventKind.AMBIGUOUS, "invalid_numeric_entry")
        expiry = _parse_expiry(text, raw.source_ts_utc)
        if expiry is None:
            return replace(
                _base(raw, EventKind.AMBIGUOUS, "entry_missing_expiry"),
                ticker=ticker,
                option_side=_side(entry_match["cp"]),
                strike=strike,
                referenced_price=price,
            )
        return replace(
            _base(raw, EventKind.ENTRY, "deterministic_entry"),
            ticker=ticker,
            option_side=_side(entry_match["cp"]),
            strike=strike,
            expiry=expiry,
            referenced_price=price,
        )

    pct_match = PCT_RE.search(text)
    pct = Decimal(pct_match["pct"]) if pct_match else None

    if any(word in lower for word in STOP_WORDS):
        return replace(_base(raw, EventKind.EXIT, "stop_language"), referenced_pct=pct)
    if any(word in lower for word in EXIT_WORDS):
        return replace(_base(raw, EventKind.EXIT, "exit_language"), referenced_pct=pct)
    if any(word in lower for word in TRIM_WORDS):
        return replace(_base(raw, EventKind.TRIM, "trim_language"), referenced_pct=pct)
    if any(word in lower for word in ADD_WORDS):
        return _base(raw, EventKind.ADD, "add_language")
    if pct is not None:
        return replace(_base(raw, EventKind.AMBIGUOUS, "percentage_without_explicit_action"), referenced_pct=pct)

    return _base(raw, EventKind.IGNORE, "no_supported_signal")
