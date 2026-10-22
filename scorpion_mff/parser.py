"""Deterministic Discord post parser. Pure functions, no I/O, no LLM.

Grammar harvested from the 23 real posts in mff-backtest/ocr_raw/*.webp.txt and
the fields recorded in live/misses/2026-09-08.json:

    CALL TICKER NVDA STRIKE $180 Exp Dec 5 Current BID Price $3.25
    PUT  TICKER AMD  STRIKE 240  Exp Nov   Current BID Price $1.80 @PLATINUM
    CALL/ ODTE LOTTO TICKER TSLA STRIKE $265 Exp Today Current BID Price $1.90
    ... reply-quote noise:  Adding at 3.00 / Close at 2.65 / Runners at $3.00

Live traffic arrives as CLEAN text from the Gateway; the OCR corpus is noisier
than production (it turns "$240" into "5240"), so the parser is deliberately
tolerant of a missing "$" and glues, and records what it could not read rather
than guessing a number.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .types import AlertKind, IgnoreReason, RawMessage, Side, Signal

# ── channels: the locked include list (live/RULES.lock.md) ───────────────────
CHANNEL_IDS: dict[str, str] = {
    "options-king": "968352649437126676",
    "high-confidence-options": "1448448931116748993",
    "etf-options": "1231301953972207667",
    "platinum-options": "946975031399972884",
}
EXCLUDED_CHANNEL_IDS: dict[str, str] = {
    "lotto-options": "1374221522159272076",
    "lotto-friday": "1385605849531154522",
}
EXCLUDED_TICKERS: frozenset[str] = frozenset({"NKE"})

_ID_TO_NAME = {**{v: k for k, v in CHANNEL_IDS.items()},
               **{v: k for k, v in EXCLUDED_CHANNEL_IDS.items()}}

# Words that must never be mistaken for a ticker when they follow "TICKER".
_NON_TICKERS = frozenset({
    "STRIKE", "EXP", "EXPIRY", "CURRENT", "BID", "PRICE", "LOTTO", "ODTE",
    "CALL", "PUT", "ER", "FOMC", "RISKY", "TODAY", "SL", "LIGHT", "NEW",
    "CALLS", "PUTS",
})

# An exit post almost never restates the contract ("QQQ puts closed 2.85"). Without
# a ticker the engine can only resolve by channel, so two positions in one channel
# make every exit ambiguous and the book never empties - see BACKTEST.md. These are
# the English words that would otherwise be mistaken for a bare symbol.
_BARE_TICKER_STOPWORDS = _NON_TICKERS | frozenset({
    "AT", "ON", "IN", "OUT", "ALL", "AND", "THE", "FOR", "NOT", "NOW", "OFF", "UP",
    "DOWN", "SOLD", "SALE", "CLOSE", "CLOSED", "CLOSING", "EXIT", "EXITING",
    "FLATTEN", "FLAT", "TRIM", "TRIMMED", "TRIMMING", "RUNNER", "RUNNERS", "HALF",
    "PART", "PARTIAL", "FULL", "FULLY", "BE", "BREAKEVEN", "ATM", "OTM", "ITM",
    "STILL", "HOLD", "HOLDING", "NICE", "GOOD", "GREAT", "YES", "MORE", "MOST",
    "LAST", "NEXT", "DAY", "WEEK", "PROFIT", "LOSS", "WIN", "WINNER", "WINNERS",
    "STOP", "STOPPED", "POSITION", "POSITIONS", "CONTRACT", "CONTRACTS", "TAKE",
    "TAKEN", "ADD", "ADDED", "AVG", "AVERAGE", "OLD", "HEAVY", "SIZE", "LOCK",
    "LOCKED", "SAFE", "RISK", "FREE", "OPEN", "GAP", "LESS", "MUCH", "VERY",
    "JUST", "THANKS", "WOW", "PLUS", "EVEN", "BACK", "HERE", "THERE", "THIS",
    "THAT", "FROM", "WITH", "WILL", "WONT", "CAN", "COULD", "SHOULD", "WOULD",
    "MAY", "MIGHT", "MUST", "DONT", "DOESNT", "STOPLOSS", "ROLLING", "ROLLED",
    "MOVE", "MOVED", "MOVING", "PLAY", "PLAYS", "TRADE", "TRADES", "TRADED",
    "CASH", "BAG", "BAGS", "CHART", "LEVEL", "LEVELS", "ZONE", "FLOW", "REST",
    "ELSE", "SOME", "ANY", "EVERY", "BOTH", "EACH", "OVER", "UNDER", "ABOVE",
})
_BARE_SYMBOL_RE = re.compile(r"\b([A-Z]{2,5})\b")

_SIDE_RE = re.compile(r"\b(CALLS?|PUTS?)\b")
_TICKER_RE = re.compile(r"\bTICKER\s+\$?([A-Z][A-Z0-9]{0,5}(?:\.[A-Z])?)\b")
_STRIKE_RE = re.compile(r"\bSTRIKE\s*[:.]?\s*\$?\s*([0-9]{1,6}(?:\.[0-9]{1,2})?)\b")
_EXP_RE = re.compile(r"\bEXP(?:IRY|IRATION)?\s*[:.]?\s*([A-Z0-9/ ,.:-]{0,28})")
_BID_RE = re.compile(
    r"\b(?:CURRENT\s+BID\s+PRICE|BID\s+PRICE|CURRENT\s+BID)\s*[:.]?\s*\$?\s*([0-9]{0,5}(?:\.[0-9]{1,2})?)"
)
_PCT_RE = re.compile(r"([0-9]{1,4}(?:\.[0-9]{1,2})?)\s*%")
# A bare number in a price position: not a percentage, not the head of a longer
# number. Without the lookahead, "Close at 19%" reads 19.00 as a PRICE and the
# engine sells a $3.13 contract for $19 - the replay harness caught exactly that.
_NUM = r"([0-9]{0,5}(?:\.[0-9]{1,2})?)"
_NOT_PCT_OR_DIGIT = r"(?![0-9.%])"
_AT_PRICE_RE = re.compile(rf"\bAT\b\s*\$?\s*{_NUM}{_NOT_PCT_OR_DIGIT}")
_AVG_PRICE_RE = re.compile(
    rf"\b(?:NEW\s+)?AVG(?:ERAGE)?\s+(?:AT\s+)?\$?\s*{_NUM}{_NOT_PCT_OR_DIGIT}")
# "NVDA STRIKE $180 CALLS CLOSED" names a strike, not an exit price. A bare
# dollar amount followed by option language is a contract term, and an amount
# equal to the parsed strike is never a premium. Without this, a close that
# omitted "AT 2.65" sold a $1 contract at $180 and booked a phantom profit.
_DOLLAR_RE = re.compile(
    rf"\$\s*{_NUM}{_NOT_PCT_OR_DIGIT}"
    r"(?!\s*(?:STRIKE|CALLS?|PUTS?|EXP(?:IRY|IRATION)?|CONTRACT)\b)"
)
_TAG_RE = re.compile(r"@(PLATINUM|DIAMOND)\b")
_EXP_STOPWORDS = (
    "CURRENT", "BID", "PRICE", "SL", "LIGHT", "YOU", "JUMP", "MONEYFORFUN",
    "EVERYONE", "@", "TRADED", "RUNNER", "ADD", "AVG", "CLOSE", "EXIT", "IN",
    "THE", "OLD", "MESSAGES", "PRINTING",
)

_FINAL_PHRASES = ("CLOSED FULLY", "FULLY CLOSED", "FLATTEN", "FLATTENED", "ALL OUT")
_TRIM_WORDS = ("TRIM", "RUNNER", "RUNNERS", "SCALE OUT", "SCALING OUT", "HALF", "PARTIAL",
               "SET SL", "SL AT", "BREAKEVEN", "STOP LOSS", "SET STOP")
# "scaling in" BUILDS a position; only "scale out" cuts one. Getting this
# backwards cuts a position when he says he is adding.
_ADD_WORDS = ("ADDING", "ADDED", "ADD MORE", "NEW AVG", "AVG AT", "AVERAGE", "AVG ",
              "SCALING IN", "SCALE IN", "SCALE HERE", "SCALING UP")
_STANDDOWN_WORDS = ("STAND DOWN", "STAND-DOWN", "STANDDOWN", "DO NOT BUY", "HALT", "KILL SWITCH")
_EXPLICIT_CLOSE_RE = re.compile(r"\b(CLOSE|CLOSING|CLOSED|SOLD|EXIT|FLATTEN)\b")
# "don't add", "do not close", "not closing yet": a negated verb is not an
# instruction to do the verb. A keyword-only classifier fires on these.
_NEGATED_VERB_RE = re.compile(
    r"\b(?:DON'?T|DO\s+NOT|NOT|NO)\s+(?:ADD|ADDING|CLOSE|CLOSING|EXIT|EXITING|TRIM|TRIMMING|SELL|SELLING|BUY|BUYING)\b")


def normalise(text: str) -> str:
    """Uppercase, drop zero-width/odd unicode, collapse whitespace.

    Also repairs the two OCR-isms that appear in the corpus and that a human
    would read correctly: "PUTI"/"CALLI" (a stray pipe) and "IDTE"/"ODTE".
    """
    t = (text or "")
    for junk in ("\u200b", "\u200c", "\ufeff", "\u00a0"):
        t = t.replace(junk, " ")
    t = t.replace("PUTI", "PUT ").replace("CALLI", "CALL ")
    t = re.sub(r"\bIDTE\b", "ODTE", t)
    t = re.sub(r"\s+", " ", t).strip().upper()
    return t


def channel_name(channel: str) -> str:
    """Accept either a channel id or a human name; return the human name."""
    c = (channel or "").strip()
    return _ID_TO_NAME.get(c, c)


def _clean_number(raw: str) -> float | None:
    """'' and '.' mean the post had no readable number - never guess one."""
    if not raw or raw in {".", "$"}:
        return None
    try:
        value = float(raw.lstrip("$"))
    except ValueError:
        return None
    return value if value > 0 else None


def _expiry(t: str) -> str:
    m = _EXP_RE.search(t)
    if not m:
        return ""
    rest = m.group(1)
    for stop in _EXP_STOPWORDS:
        idx = rest.find(stop)
        if idx > 0:
            rest = rest[:idx]
    rest = rest.strip(" .,")
    if not rest:
        return ""
    flat = rest.replace(" ", "")
    if any(tok in flat for tok in ("TODAY", "ODTE", "0DTE")):
        return "0DTE"
    return rest


def _kind(t: str, side: Side | None, strike: float | None) -> AlertKind:
    """Classify the post, mirroring live/RULES.lock.md's EXIT line:

        Discord close/trim/%/stop/sold runners -> trim to 1 + BE or flatten last

    Precedence, and why (an independent review caught the first version getting
    this backwards):

      1. stand-down            -> STOP (a halt, not an exit)
      2. a negated verb        -> IGNORE ("don't add", "not closing yet")
      3. an unambiguous final  -> CLOSE ("closed fully", "flatten", "all out")
      4. any scale-out word    -> TRIM. "SOLD RUNNERS" belongs HERE, not in (3):
                                  the lock lists "sold runners" under trim-to-1+BE.
      5. an explicit close verb-> CLOSE ("close at 2.65", "close remaining for 10%")
      6. a bare "%"            -> TRIM (a level, so protect; see HOTPATH.md for
                                  the commentary trade-off this accepts)
      7. add/avg words         -> ADD ("scaling in" is an add, never a trim)
      8. side + strike         -> ENTRY
      9. otherwise             -> IGNORE
    """
    if any(w in t for w in _STANDDOWN_WORDS) or _NEGATED_VERB_RE.search(t):
        return AlertKind.STOP if any(w in t for w in _STANDDOWN_WORDS) else AlertKind.IGNORE
    if any(p in t for p in _FINAL_PHRASES):
        return AlertKind.CLOSE         # "Runners at $3.00 closed fully"
    if any(w in t for w in _TRIM_WORDS):
        return AlertKind.TRIM          # "Sold runners at 3.90", "Runners @ 25%"
    if _EXPLICIT_CLOSE_RE.search(t):
        return AlertKind.CLOSE         # "Close at 2.65", "Close remaining for 10%"
    if _PCT_RE.search(t):
        return AlertKind.TRIM          # "75% if you're still holding"
    if any(w in t for w in _ADD_WORDS):
        return AlertKind.ADD
    if side is not None and strike is not None:
        return AlertKind.ENTRY
    return AlertKind.IGNORE


def parse(message: RawMessage | str, channel: str = "") -> Signal:
    """One post -> one typed Signal. Never raises.

    `channel` may be an id or a name; when a RawMessage is passed its own
    channel wins.
    """
    if isinstance(message, RawMessage):
        raw_text = message.text
        chan = message.channel or message.channel_id
        chan_id = message.channel_id
    else:
        raw_text = message
        chan = channel
        chan_id = channel

    t = normalise(raw_text)
    name = channel_name(chan or chan_id)
    if not chan_id or chan_id == name:
        chan_id = CHANNEL_IDS.get(name, EXCLUDED_CHANNEL_IDS.get(name, ""))

    side_m = _SIDE_RE.search(t)
    side = None
    if side_m:
        token = side_m.group(1).rstrip("Ss")
        try:
            side = Side(token)
        except ValueError:            # a side token we do not model is not a crash
            side = None

    ticker = ""
    tick_m = _TICKER_RE.search(t)
    if tick_m and tick_m.group(1) not in _NON_TICKERS:
        ticker = tick_m.group(1)

    strike_m = _STRIKE_RE.search(t)
    strike = _clean_number(strike_m.group(1)) if strike_m else None
    bid_m = _BID_RE.search(t)
    his_bid = _clean_number(bid_m.group(1)) if bid_m else None
    pct_m = _PCT_RE.search(t)
    pct = _clean_number(pct_m.group(1)) if pct_m else None
    price = None
    for rx in (_AT_PRICE_RE, _AVG_PRICE_RE, _DOLLAR_RE):
        for match in rx.finditer(t):
            candidate = _clean_number(match.group(1))
            if candidate is None:
                continue
            # A dollar amount equal to the strike is the contract, not the fill.
            if rx is _DOLLAR_RE and strike is not None and abs(candidate - strike) < 1e-9:
                continue
            price = candidate
            break
        if price is not None:
            break

    tags = tuple(sorted(set(_TAG_RE.findall(t))))
    # A LOTTO flag only means "lottery play" in the HEAD of an ENTRY post, before
    # the ticker: "CALL/ ODTE LOTTO TICKER TSLA". In "keep as lotto" (an exit
    # note) the word means "leave a runner", and in a post with no TICKER at all
    # there is no head - so neither can silence a legitimate exit.
    head = t.split("TICKER")[0] if "TICKER" in t else ""
    flags = tuple(
        f for f, present in (
            ("LOTTO", bool(re.search(r"\bLOTTO\b", head))),
            ("ODTE", "ODTE" in t or "0DTE" in t),
            ("ER", bool(re.search(r"\bER\b", head))),
            ("FOMC", "FOMC" in head),
            ("RISKY", "RISKY" in head),
        ) if present
    )

    kind = _kind(t, side, strike)

    # Recover a bare symbol on exit-side posts, so an exit can be attributed.
    # Exactly one candidate or nothing: guessing between two symbols is worse than
    # an alert, and this only ever runs when the post did not say "TICKER X".
    if not ticker and kind is not AlertKind.ENTRY:
        candidates = {m for m in _BARE_SYMBOL_RE.findall(t)
                      if m not in _BARE_TICKER_STOPWORDS}
        if len(candidates) == 1:
            ticker = candidates.pop()

    base: dict[str, Any] = dict(
        channel=name, channel_id=chan_id, ticker=ticker, side=side, strike=strike,
        expiry=_expiry(t), his_bid=his_bid, pct=pct, price=price, tags=tags,
        flags=flags, raw=raw_text,
    )

    # Ignore policy, in the order the locked rules state it.
    if name in EXCLUDED_CHANNEL_IDS or (chan_id in EXCLUDED_CHANNEL_IDS.values()):
        return Signal(kind=AlertKind.IGNORE, ignore_reason=IgnoreReason.EXCLUDED_CHANNEL, **base)
    if ticker in EXCLUDED_TICKERS:
        return Signal(kind=AlertKind.IGNORE, ignore_reason=IgnoreReason.TICKER_EXCLUDED, **base)
    if "LOTTO" in flags:
        # Policy, not fact: the hone list names lotto-* CHANNELS. Whether a
        # LOTTO-flagged post inside options-king is also out is a house call -
        # see hotpath.ignore_lotto in config. Default is to ignore it.
        return Signal(kind=AlertKind.IGNORE, ignore_reason=IgnoreReason.LOTTO_FLAG, **base)
    if kind is AlertKind.IGNORE:
        return Signal(kind=AlertKind.IGNORE, ignore_reason=IgnoreReason.NO_SIGNAL, **base)
    return Signal(kind=kind, **base)


def parse_all(messages: Iterable[RawMessage]) -> list[Signal]:
    return [parse(m) for m in messages]
