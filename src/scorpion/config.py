from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
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
