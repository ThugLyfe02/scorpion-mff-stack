from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .config import CHANNELS
from .domain import EventKind, SignalEvent


class StrategyBucket(StrEnum):
    CORE_SINGLE_NAME = "CORE_SINGLE_NAME"
    ETF = "ETF"
    LOTTO = "LOTTO"
    EXCLUDED = "EXCLUDED"
    NON_ACTIONABLE = "NON_ACTIONABLE"
    UNKNOWN = "UNKNOWN"


class EligibilityDisposition(StrEnum):
    ALLOW_REVIEW = "ALLOW_REVIEW"
    RESEARCH_ONLY = "RESEARCH_ONLY"


DEFAULT_ETF_SYMBOLS = frozenset(
    {
        "ARKK",
        "DIA",
        "IWM",
        "QQQ",
        "SOXL",
        "SPY",
        "TQQQ",
        "TSLL",
        "UPRO",
        "XLF",
        "XLK",
    }
)
_LOTTO_TERMS = ("lotto", "0dte lotto", "er lotto", "fomc lotto")


@dataclass(frozen=True, slots=True)
class StrategyEligibilityPolicy:
    """Execution-eligibility overlay, intentionally separate from Ryan's locked source rules.

    Alerts remain ingested, parsed, persisted, replayable, and available to research even when
    this overlay marks them research-only. This prevents strategy experimentation from erasing
    source truth or silently mutating the original channel contract.
    """

    research_only_channel_ids: frozenset[str] = frozenset({CHANNELS["etf-options"]})
    etf_symbols: frozenset[str] = DEFAULT_ETF_SYMBOLS
    block_lotto_language: bool = True


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    bucket: StrategyBucket
    disposition: EligibilityDisposition
    reason: str

    @property
    def allowed(self) -> bool:
        return self.disposition is EligibilityDisposition.ALLOW_REVIEW


def _ticker_from_contract_key(contract_key: str | None) -> str | None:
    if not contract_key:
        return None
    ticker = contract_key.split("|", 1)[0].strip().upper()
    return ticker or None


def classify_eligibility(
    event: SignalEvent,
    policy: StrategyEligibilityPolicy | None = None,
) -> EligibilityDecision:
    policy = policy or StrategyEligibilityPolicy()
    if event.kind in {EventKind.IGNORE, EventKind.AMBIGUOUS, EventKind.STOP}:
        return EligibilityDecision(
            StrategyBucket.NON_ACTIONABLE,
            EligibilityDisposition.ALLOW_REVIEW,
            "non_actionable_event",
        )

    ticker = (event.ticker or _ticker_from_contract_key(event.contract_key) or "").upper()
    text = event.raw_text.lower()

    if policy.block_lotto_language and any(term in text for term in _LOTTO_TERMS):
        return EligibilityDecision(
            StrategyBucket.LOTTO,
            EligibilityDisposition.RESEARCH_ONLY,
            "lotto_language_research_only",
        )

    if event.channel_id in policy.research_only_channel_ids:
        return EligibilityDecision(
            StrategyBucket.ETF,
            EligibilityDisposition.RESEARCH_ONLY,
            "etf_channel_research_only",
        )

    if ticker and ticker in policy.etf_symbols:
        return EligibilityDecision(
            StrategyBucket.ETF,
            EligibilityDisposition.RESEARCH_ONLY,
            "etf_underlying_research_only",
        )

    if ticker:
        return EligibilityDecision(
            StrategyBucket.CORE_SINGLE_NAME,
            EligibilityDisposition.ALLOW_REVIEW,
            "core_single_name_candidate",
        )

    return EligibilityDecision(
        StrategyBucket.UNKNOWN,
        EligibilityDisposition.RESEARCH_ONLY,
        "unknown_instrument_research_only",
    )
