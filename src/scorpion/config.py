from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

GUILD_ID = "912747256736800838"
MARKET_TZ = ZoneInfo("America/New_York")

CHANNELS: dict[str, str] = {
    "options-king": "968352649437126676",
    "high-confidence-options": "1448448931116748993",
    "etf-options": "1231301953972207667",
    "platinum-options": "946975031399972884",
}

ALLOWED_CHANNEL_IDS = frozenset(CHANNELS.values())
EXCLUDED_TICKERS = frozenset({"NKE"})


@dataclass(frozen=True, slots=True)
class Policy:
    max_open_positions: int = 2
    max_dislocation_from_reference: Decimal = Decimal("0.25")
    price_limit_markup: Decimal = Decimal("0.15")
    first_entry_quantity: int = 1
    skip_fridays: bool = True
    live_submission_enabled: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    database_path: str
    discord_token: str
    allowed_author_ids: frozenset[str]

    @classmethod
    def from_env(cls) -> RuntimeSettings:
        token = os.environ.get("SCORPION_DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError("SCORPION_DISCORD_TOKEN is required")
        authors = frozenset(
            part.strip()
            for part in os.environ.get("SCORPION_SIGNAL_AUTHOR_IDS", "").split(",")
            if part.strip()
        )
        if not authors:
            raise RuntimeError(
                "SCORPION_SIGNAL_AUTHOR_IDS is required in runtime mode; "
                "the money-path must fail closed without an author allowlist"
            )
        return cls(
            database_path=os.environ.get("SCORPION_DB", "scorpion.db"),
            discord_token=token,
            allowed_author_ids=authors,
        )


DEFAULT_POLICY = Policy()


# --------------------------------------------------------------------------------------------
# Paper execution core (exec-core-v1). Single source for stale-data policy, cost cap and the
# frozen forward rules. Latency targets are milliseconds: the strategy follows human Discord
# posts and oos132 shows the edge survives up to a 15-minute follow delay.
# --------------------------------------------------------------------------------------------

EXECUTION_COMPONENT = "paper-exec-core"
DATABENTO_COST_CAP_USD = Decimal("30")
FORWARD_RULES_PATH = Path(__file__).resolve().parents[2] / "live" / "RULES.lock.md"
# Canonical SHA-256 of the forward-rules JSON block in live/RULES.lock.md. Changing a rule means
# changing this constant in the same reviewed PR; preflight refuses to start on a mismatch.
FORWARD_RULES_SHA256 = "870b699f93e2b55532b117317ba72f5667055338a0c0e731d61f05f5b5b64324"

_RULES_BEGIN = "<!-- forward-rules:begin -->"
_RULES_END = "<!-- forward-rules:end -->"


@dataclass(frozen=True, slots=True)
class StalePolicy:
    """Every stale-data threshold used by the paper session, in milliseconds."""

    quote_max_age_ms: int = 3_000
    feed_silence_ms: int = 10_000
    symbol_silence_ms: int = 60_000
    clock_skew_ms: int = 2_000
    clock_skew_persist_ms: int = 30_000

    def __post_init__(self) -> None:
        for name in (
            "quote_max_age_ms",
            "feed_silence_ms",
            "symbol_silence_ms",
            "clock_skew_ms",
            "clock_skew_persist_ms",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


STALE_POLICY = StalePolicy()


def _hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


@dataclass(frozen=True, slots=True)
class ForwardRules:
    rules_version: str
    channel: str
    side: str
    instrument: str
    excluded_tickers: frozenset[str]
    entry_delay_min: int
    limit_window_min: int
    chase_once: bool
    skip_if_seen_after_min: int
    hold_min: int
    session_open_et: time
    session_last_bar_et: time
    exit_cap_et: time
    latest_post_et: time
    min_session_bars: int
    trade_through_bps: int
    cost_bps_round_trip: int
    shadow_track_from_et: time
    main_tier_below_usd: Decimal
    sub10_below_usd: Decimal
    hold_to_close_shadow_tier: str
    paper_notional_usd: Decimal
    account_track_start_equity_usd: Decimal
    sha256: str

    @property
    def cost_pct(self) -> float:
        return self.cost_bps_round_trip / 100.0

    @property
    def trade_through_factor(self) -> float:
        return 1.0 - self.trade_through_bps / 10_000.0


def forward_rules_block(text: str) -> dict[str, object]:
    start = text.find(_RULES_BEGIN)
    end = text.find(_RULES_END)
    if start < 0 or end < 0 or end < start:
        raise ValueError("forward-rules block not found in rules lock")
    body = text[start + len(_RULES_BEGIN) : end].strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1]
        body = body.rsplit("```", 1)[0]
    loaded = json.loads(body)
    if not isinstance(loaded, dict):
        raise ValueError("forward-rules block must be a JSON object")
    return loaded


def canonical_rules_sha256(block: dict[str, object]) -> str:
    canonical = json.dumps(block, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_forward_rules(path: str | Path | None = None) -> ForwardRules:
    """Load the frozen forward rules from the rules lock (the only source of rule values)."""

    raw = forward_rules_block(Path(path or FORWARD_RULES_PATH).read_text(encoding="utf-8"))
    excluded = raw["excluded_tickers"]
    if not isinstance(excluded, list):
        raise ValueError("excluded_tickers must be a list")
    for fixed in ("stop", "target", "trail", "hedge", "qqq_filter"):
        if raw.get(fixed) != "none":
            raise ValueError(f"forward rules forbid a {fixed}; the lock says {raw.get(fixed)!r}")

    def text(key: str) -> str:
        return str(raw[key])

    def integer(key: str) -> int:
        value = raw[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{key} must be an integer")
        return value

    return ForwardRules(
        rules_version=text("rules_version"),
        channel=text("channel"),
        side=text("side"),
        instrument=text("instrument"),
        excluded_tickers=frozenset(str(item).upper() for item in excluded),
        entry_delay_min=integer("entry_delay_min"),
        limit_window_min=integer("limit_window_min"),
        chase_once=bool(raw["chase_once"]),
        skip_if_seen_after_min=integer("skip_if_seen_after_min"),
        hold_min=integer("hold_min"),
        session_open_et=_hhmm(text("session_open_et")),
        session_last_bar_et=_hhmm(text("session_last_bar_et")),
        exit_cap_et=_hhmm(text("exit_cap_et")),
        latest_post_et=_hhmm(text("latest_post_et")),
        min_session_bars=integer("min_session_bars"),
        trade_through_bps=integer("trade_through_bps"),
        cost_bps_round_trip=integer("cost_bps_round_trip"),
        shadow_track_from_et=_hhmm(text("shadow_track_from_et")),
        main_tier_below_usd=Decimal(text("main_tier_below_usd")),
        sub10_below_usd=Decimal(text("sub10_below_usd")),
        hold_to_close_shadow_tier=text("hold_to_close_shadow_tier"),
        paper_notional_usd=Decimal(text("paper_notional_usd")),
        account_track_start_equity_usd=Decimal(text("account_track_start_equity_usd")),
        sha256=canonical_rules_sha256(raw),
    )


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Safety caps for paper order prices (not strategy rules).

    max_chase_bps bounds the single chase above the first limit. 150 bps covers every historical
    chase in the 197-call parity replay (max 102 bps, p95 71 bps), so the cap does not change a
    backtested fill while still refusing a runaway print.
    """

    max_chase_bps: int = 150

    def __post_init__(self) -> None:
        if not 0 <= self.max_chase_bps <= 1_000:
            raise ValueError("max_chase_bps must be within 0..1000")


EXECUTION_LIMITS = ExecutionLimits()
