from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from .accuracy import DecisionEvidence
from .config import ALLOWED_CHANNEL_IDS, EXCLUDED_TICKERS, GUILD_ID, MARKET_TZ
from .domain import EventKind, RawDiscordMessage, SignalEvent

PARSER_VERSION = "v2"

ENTRY_RE = re.compile(
    r"\b(?P<ticker>[A-Z]{1,6})\b.*?"
    r"(?P<strike>\d+(?:\.\d+)?)\s*(?P<cp>C|P|CALL|PUT)\b",
    re.IGNORECASE,
)
ENTRY_ALT_RE = re.compile(
    r"\b(?P<cp>CALL|PUT)\b.*?\b(?P<ticker>[A-Z]{1,6})\b.*?"
    r"\$?(?P<strike>\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
PRICE_ANCHOR_RE = re.compile(
    r"(?:@|\bat\b|\bbid\b|\bpremium\b)\s*\$?(?P<price>\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
FALLBACK_PRICE_RE = re.compile(r"\$?(?P<price>\d+\.\d{1,4})\b")
PCT_RE = re.compile(r"(?P<pct>[+-]?\d+(?:\.\d+)?)\s*%")
EXPIRY_ISO_RE = re.compile(r"\b(?P<y>20\d{2})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})\b")
EXPIRY_MD_RE = re.compile(
    r"\b(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*"
    r"(?P<d>\d{1,2})(?:,?\s*(?P<y>20\d{2}))?\b",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_ACTION_PATTERNS: dict[EventKind, tuple[tuple[str, re.Pattern[str]], ...]] = {
    EventKind.ADD: (
        ("added", re.compile(r"\badded\b", re.IGNORECASE)),
        ("adding", re.compile(r"\badding\b", re.IGNORECASE)),
        ("averaged", re.compile(r"\baveraged\b", re.IGNORECASE)),
        ("avg", re.compile(r"\bavg\b", re.IGNORECASE)),
        ("average", re.compile(r"\baverage(?:d|ing)?\b", re.IGNORECASE)),
        ("new_avg", re.compile(r"\bnew\s+avg\b", re.IGNORECASE)),
    ),
    EventKind.TRIM: (
        ("trim", re.compile(r"\btrimm?(?:ed|ing)?\b", re.IGNORECASE)),
        ("take_some", re.compile(r"\btak(?:e|ing)\s+some\b", re.IGNORECASE)),
        ("close_half", re.compile(r"\bclos(?:e|ed|ing)\s+half\b", re.IGNORECASE)),
        ("sold_half", re.compile(r"\bsold\s+half\b", re.IGNORECASE)),
        ("lock_some", re.compile(r"\block(?:ing)?\s+some\b", re.IGNORECASE)),
        ("scale_some", re.compile(r"\bscal(?:e|ing)\s+(?:some|out)\b", re.IGNORECASE)),
        ("take_profit", re.compile(r"\btak(?:e|ing)\s+profit(?:s)?\b", re.IGNORECASE)),
    ),
    EventKind.EXIT: (
        ("all_out", re.compile(r"\ball\s+out\b", re.IGNORECASE)),
        ("full_close", re.compile(r"\bfull(?:y)?\s+clos(?:e|ed|ing)\b", re.IGNORECASE)),
        ("closing_runners", re.compile(r"\bclos(?:e|ed|ing)\s+runners?\b", re.IGNORECASE)),
        ("sold_runners", re.compile(r"\bsold\s+runners?\b", re.IGNORECASE)),
        ("sold_all", re.compile(r"\bsold\s+all\b", re.IGNORECASE)),
        ("out_here", re.compile(r"\bout\s+here\b", re.IGNORECASE)),
        ("all_closed", re.compile(r"\ball\s+closed\b", re.IGNORECASE)),
        ("flatten", re.compile(r"\bflatten(?:ed|ing)?\b", re.IGNORECASE)),
        ("flat_now", re.compile(r"\bflat\s+now\b", re.IGNORECASE)),
        ("close_rest", re.compile(r"\bclos(?:e|ed|ing)\s+(?:the\s+)?rest\b", re.IGNORECASE)),
        ("close_at", re.compile(r"\bclose\s+at\s+[+-]?\d+(?:\.\d+)?\s*%", re.IGNORECASE)),
        ("pct_close", re.compile(r"[+-]?\d+(?:\.\d+)?\s*%\s+close\b", re.IGNORECASE)),
        ("closed", re.compile(r"\bclosed\b", re.IGNORECASE)),
    ),
}

_STOP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("stop_loss", re.compile(r"\bstop\s+loss\b", re.IGNORECASE)),
    ("stopped", re.compile(r"\bstopped\b", re.IGNORECASE)),
    ("stop_hit", re.compile(r"\bstop\s+hit\b", re.IGNORECASE)),
    ("out_at_loss", re.compile(r"\bout\s+at\s+-\s*\d+(?:\.\d+)?\s*%", re.IGNORECASE)),
)

_CONDITIONAL_RE = re.compile(
    r"\b(?:if|would|could|might|maybe|looking\s+to|ready\s+to|plan(?:ning)?\s+to)\b",
    re.IGNORECASE,
)
_GENERIC_CLOSE_RE = re.compile(r"\bclos(?:e|ing)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ParseDecision:
    event: SignalEvent
    evidence: DecisionEvidence


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
    return " ".join(normalized.split())


def _event_id(raw: RawDiscordMessage, kind: EventKind) -> str:
    revision = raw.edited_ts_utc.isoformat() if raw.edited_ts_utc else "create"
    normalized_digest = hashlib.sha256(normalize_text(raw.content).encode("utf-8")).hexdigest()
    payload = (
        f"{raw.guild_id}|{raw.channel_id}|{raw.message_id}|{revision}|"
        f"{normalized_digest}|{kind.value}|{PARSER_VERSION}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    match = EXPIRY_ISO_RE.search(text)
    if match:
        return date(int(match["y"]), int(match["m"]), int(match["d"]))
    match = EXPIRY_MD_RE.search(text)
    if match:
        year = int(match["y"]) if match["y"] else source_date.year
        month = _MONTHS[match["mon"][:3].lower()]
        candidate = date(year, month, int(match["d"]))
        if not match["y"] and candidate < source_date - timedelta(days=30):
            candidate = date(year + 1, month, int(match["d"]))
        return candidate
    return None


def _side(cp: str) -> Literal["CALL", "PUT"]:
    return "CALL" if cp.upper() in {"C", "CALL"} else "PUT"


def _action_hits(text: str) -> dict[EventKind, tuple[str, ...]]:
    hits: dict[EventKind, tuple[str, ...]] = {}
    for kind, rules in _ACTION_PATTERNS.items():
        matched = tuple(rule_id for rule_id, pattern in rules if pattern.search(text))
        if matched:
            hits[kind] = matched

    stop_hits = tuple(rule_id for rule_id, pattern in _STOP_PATTERNS if pattern.search(text))
    if stop_hits:
        hits[EventKind.EXIT] = tuple(sorted(set(hits.get(EventKind.EXIT, ()) + stop_hits)))

    trim_hits = hits.get(EventKind.TRIM, ())
    if not trim_hits and _GENERIC_CLOSE_RE.search(text):
        if _CONDITIONAL_RE.search(text):
            return {EventKind.AMBIGUOUS: ("conditional_close",)}
        hits[EventKind.EXIT] = tuple(
            sorted(set(hits.get(EventKind.EXIT, ()) + ("generic_close",)))
        )
    return hits


def parse_message_with_evidence(
    raw: RawDiscordMessage,
    allowed_author_ids: frozenset[str] | None = None,
) -> ParseDecision:
    started_ns = time.perf_counter_ns()
    normalized = normalize_text(raw.content)

    def finish(
        event: SignalEvent,
        rule_id: str,
        confidence: float,
        *,
        matched_terms: tuple[str, ...] = (),
        conflicts: tuple[str, ...] = (),
    ) -> ParseDecision:
        latency_us = max(0, (time.perf_counter_ns() - started_ns) // 1_000)
        evidence = DecisionEvidence(
            rule_id=rule_id,
            confidence=confidence,
            matched_terms=matched_terms,
            conflicts=conflicts,
            normalized_text=normalized,
            latency_us=latency_us,
        )
        return ParseDecision(event, evidence)

    if raw.guild_id != GUILD_ID:
        return finish(_base(raw, EventKind.IGNORE, "wrong_guild"), "gate.wrong_guild", 1.0)
    if raw.channel_id not in ALLOWED_CHANNEL_IDS:
        return finish(
            _base(raw, EventKind.IGNORE, "channel_not_allowed"),
            "gate.channel_not_allowed",
            1.0,
        )
    if allowed_author_ids is not None and raw.author_id not in allowed_author_ids:
        return finish(
            _base(raw, EventKind.IGNORE, "author_not_allowed"),
            "gate.author_not_allowed",
            1.0,
        )

    entry_match = ENTRY_RE.search(normalized) or ENTRY_ALT_RE.search(normalized)
    if entry_match:
        ticker = entry_match["ticker"].upper()
        if ticker in EXCLUDED_TICKERS:
            event = replace(_base(raw, EventKind.IGNORE, "excluded_ticker"), ticker=ticker)
            return finish(event, "entry.excluded_ticker", 1.0, matched_terms=(ticker,))
        tail = normalized[entry_match.end():]
        price_match = PRICE_ANCHOR_RE.search(tail)
        anchored_price = price_match is not None
        if price_match is None:
            fallback_matches = list(FALLBACK_PRICE_RE.finditer(tail))
            price_match = fallback_matches[-1] if fallback_matches else None
        try:
            strike = Decimal(entry_match["strike"])
            price = Decimal(price_match["price"]) if price_match else None
        except InvalidOperation:
            return finish(
                _base(raw, EventKind.AMBIGUOUS, "invalid_numeric_entry"),
                "entry.invalid_numeric",
                0.1,
            )
        if price is None:
            event = replace(
                _base(raw, EventKind.AMBIGUOUS, "entry_missing_price"),
                ticker=ticker,
                option_side=_side(entry_match["cp"]),
                strike=strike,
            )
            return finish(
                event,
                "entry.missing_price",
                0.35,
                matched_terms=(ticker, entry_match["cp"], entry_match["strike"]),
            )
        expiry = _parse_expiry(normalized, raw.source_ts_utc)
        if expiry is None:
            event = replace(
                _base(raw, EventKind.AMBIGUOUS, "entry_missing_expiry"),
                ticker=ticker,
                option_side=_side(entry_match["cp"]),
                strike=strike,
                referenced_price=price,
            )
            return finish(
                event,
                "entry.missing_expiry",
                0.45,
                matched_terms=(ticker, entry_match["cp"], entry_match["strike"]),
            )
        event = replace(
            _base(raw, EventKind.ENTRY, "deterministic_entry"),
            ticker=ticker,
            option_side=_side(entry_match["cp"]),
            strike=strike,
            expiry=expiry,
            referenced_price=price,
        )
        confidence = 0.998 if anchored_price else 0.96
        terms = (ticker, entry_match["cp"], entry_match["strike"], str(price))
        return finish(event, "entry.complete", confidence, matched_terms=terms)

    pct_match = PCT_RE.search(normalized)
    pct = Decimal(pct_match["pct"]) if pct_match else None
    action_hits = _action_hits(normalized)

    if EventKind.AMBIGUOUS in action_hits:
        event = replace(
            _base(raw, EventKind.AMBIGUOUS, "conditional_action_language"),
            referenced_pct=pct,
        )
        return finish(
            event,
            "action.conditional",
            0.25,
            matched_terms=action_hits[EventKind.AMBIGUOUS],
        )

    families = tuple(sorted(action_hits, key=lambda kind: kind.value))
    if len(families) > 1:
        conflicts = tuple(kind.value for kind in families)
        matched = tuple(term for kind in families for term in action_hits[kind])
        event = replace(
            _base(raw, EventKind.AMBIGUOUS, "conflicting_action_language"),
            referenced_pct=pct,
        )
        return finish(
            event,
            "action.conflict",
            0.15,
            matched_terms=matched,
            conflicts=conflicts,
        )

    if len(families) == 1:
        kind = families[0]
        matched = action_hits[kind]
        confidence = 0.99 if kind is EventKind.EXIT else 0.98
        event = replace(
            _base(raw, kind, f"{kind.value.lower()}_language"),
            referenced_pct=pct,
        )
        return finish(event, f"action.{kind.value.lower()}", confidence, matched_terms=matched)

    if pct is not None:
        event = replace(
            _base(raw, EventKind.AMBIGUOUS, "percentage_without_explicit_action"),
            referenced_pct=pct,
        )
        matched_pct = pct_match.group(0) if pct_match is not None else str(pct)
        return finish(
            event,
            "action.percentage_only",
            0.30,
            matched_terms=(matched_pct,),
        )

    return finish(
        _base(raw, EventKind.IGNORE, "no_supported_signal"),
        "ignore.no_supported_signal",
        0.995,
    )


def parse_message(
    raw: RawDiscordMessage,
    allowed_author_ids: frozenset[str] | None = None,
) -> SignalEvent:
    return parse_message_with_evidence(raw, allowed_author_ids).event
