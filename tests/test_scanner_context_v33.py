from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scorpion.scanner_context import (
    ScannerContextError,
    canonical_scanner_observation_hash,
    latest_scanner_context_before,
    load_scanner_observations,
    parse_scanner_observation,
)


def _payload(*, observed_at: datetime | None = None, symbol: str = "NVDA") -> dict[str, object]:
    observed = observed_at or datetime(2026, 9, 11, 16, 0, tzinfo=UTC)
    payload: dict[str, object] = {
        "schema_version": "scorpion.scanner-observation.v1",
        "authority": "RESEARCH_ONLY",
        "observation_id": "0" * 64,
        "run_id": "scanner-run-1",
        "symbol": symbol,
        "instrument_id": f"US_EQUITY:{symbol}",
        "category": "gapper_mobility",
        "observed_at_utc": observed.isoformat(),
        "market_data_as_of_utc": (observed - timedelta(seconds=1)).isoformat(),
        "session": "RTH",
        "last_price": 177.0,
        "prior_close": 170.0,
        "gap_pct": 4.1,
        "day_change_pct": 4.2,
        "today_volume": 5_000_000,
        "average_prior_volume": 1_000_000.0,
        "rvol": 5.0,
        "rvol_basis": "EXTERNAL_PROVIDED",
        "float_shares": 1_000_000,
        "float_source": "fixture",
        "catalyst": {
            "label": "contract",
            "confirmed": True,
            "source": "fixture",
            "headline": "fixture",
            "published_at_utc": (observed - timedelta(minutes=10)).isoformat(),
            "evidence_hash": "f" * 64,
            "point_in_time_verified": True,
        },
        "ross": {
            "boxes": {
                "price": False,
                "rvol": True,
                "float": True,
                "news": True,
                "gap": False,
            },
            "boxes_hit": 3,
            "boxes_known": 5,
            "boxes_total": 5,
            "boxes_pass": False,
            "missed": ["price", "gap"],
            "unchecked": [],
            "scorecard_line": "3/5 — missed price, gap",
        },
        "tape_flag": "MIXED",
        "tape_reason": "fixture",
        "confidence": 0.5,
        "source_fingerprint": "a" * 64,
        "candidate_fingerprint": "b" * 64,
        "quality": {
            "market_data_timestamped": True,
            "rvol_definition_known": True,
            "fundamentals_vintage_verified": False,
            "catalyst_publication_time_verified": True,
            "corporate_action_checked": False,
            "source_fingerprint_present": True,
            "blocking_reasons": ["float_vintage_unverified"],
        },
    }
    payload["observation_id"] = canonical_scanner_observation_hash(payload)
    return payload


def _rehash(payload: dict[str, object]) -> None:
    payload["observation_id"] = canonical_scanner_observation_hash(payload)


def test_valid_scanner_context_is_research_only() -> None:
    parsed = parse_scanner_observation(_payload())
    assert parsed.research_only is True
    assert parsed.instrument_id == "US_EQUITY:NVDA"
    assert parsed.quality_blocking_reasons == ("float_vintage_unverified",)


def test_live_authority_is_rejected_even_with_valid_hash() -> None:
    payload = _payload()
    payload["authority"] = "LIVE"
    _rehash(payload)
    with pytest.raises(ScannerContextError, match="RESEARCH_ONLY"):
        parse_scanner_observation(payload)


def test_tampering_is_rejected_by_hash() -> None:
    payload = _payload()
    payload["rvol"] = 99.0
    with pytest.raises(ScannerContextError, match="hash mismatch"):
        parse_scanner_observation(payload)


def test_future_market_evidence_is_rejected() -> None:
    payload = _payload()
    observed = datetime.fromisoformat(str(payload["observed_at_utc"]))
    payload["market_data_as_of_utc"] = (observed + timedelta(seconds=1)).isoformat()
    _rehash(payload)
    with pytest.raises(ScannerContextError, match="future market evidence"):
        parse_scanner_observation(payload)


def test_future_verified_catalyst_is_rejected() -> None:
    payload = _payload()
    observed = datetime.fromisoformat(str(payload["observed_at_utc"]))
    catalyst = dict(payload["catalyst"])  # type: ignore[arg-type]
    catalyst["published_at_utc"] = (observed + timedelta(seconds=1)).isoformat()
    payload["catalyst"] = catalyst
    _rehash(payload)
    with pytest.raises(ScannerContextError, match="future catalyst evidence"):
        parse_scanner_observation(payload)


def test_asof_selector_never_selects_future_context() -> None:
    event_time = datetime(2026, 9, 11, 16, 1, tzinfo=UTC)
    old = parse_scanner_observation(_payload(observed_at=event_time - timedelta(minutes=2)))
    latest = parse_scanner_observation(_payload(observed_at=event_time - timedelta(seconds=5)))
    future = parse_scanner_observation(_payload(observed_at=event_time + timedelta(seconds=1)))

    selected = latest_scanner_context_before(
        (old, future, latest),
        symbol="nvda",
        source_ts_utc=event_time,
    )
    assert selected is not None
    assert selected.observation_id == latest.observation_id


def test_jsonl_loader_validates_every_row(tmp_path: Path) -> None:
    payload = _payload()
    path = tmp_path / "scanner.jsonl"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    rows = load_scanner_observations(path)
    assert len(rows) == 1
    assert rows[0].symbol == "NVDA"


def test_unknown_schema_is_rejected() -> None:
    payload = _payload()
    payload["schema_version"] = "scorpion.scanner-observation.v999"
    _rehash(payload)
    with pytest.raises(ScannerContextError, match="unsupported scanner schema"):
        parse_scanner_observation(payload)
