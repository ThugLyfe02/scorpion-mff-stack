from __future__ import annotations

import json
from collections.abc import Mapping
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


def _payload(
    *,
    observed_at: datetime | None = None,
    symbol: str = "NVDA",
) -> dict[str, object]:
    observed = observed_at or datetime(2026, 9, 11, 16, 0, tzinfo=UTC)
    payload: dict[str, object] = {
        "schema_version": "scorpion.scanner-observation.v1",
        "authority": "RESEARCH_ONLY",
        "observation_id": "0" * 64,
        "run_id": "scanner-run-1",
        "producer": {
            "producer": "scorpion-stock-finder",
            "package_version": "3.6.0",
            "code_revision": "fixture",
            "policy_fingerprint": "p" * 64,
        },
        "symbol": symbol,
        "instrument_id": f"US_EQUITY:{symbol}",
        "category": "gapper_mobility",
        "source_kind": "EXTERNAL_BRIEF",
        "observed_at_utc": observed.isoformat(),
        "observation_time_precision": "EXACT",
        "market_data_as_of_utc": (observed - timedelta(seconds=1)).isoformat(),
        "market_data_source": "fixture-feed",
        "session": "RTH",
        "last_price": 177.0,
        "prior_close": 170.0,
        "gap_pct": 4.1,
        "day_change_pct": 4.2,
        "today_volume": 5_000_000,
        "average_prior_volume": 1_000_000.0,
        "rvol": 5.0,
        "rvol_basis": "EXTERNAL_PROVIDED",
        "rvol_window": "external",
        "float_shares": 1_000_000,
        "float_source": "fixture",
        "float_as_of_utc": (observed - timedelta(hours=1)).isoformat(),
        "float_point_in_time_verified": True,
        "catalyst": {
            "label": "contract",
            "confirmed": True,
            "source": "fixture",
            "source_id": "story-1",
            "headline": "fixture",
            "published_at_utc": (observed - timedelta(minutes=10)).isoformat(),
            "evidence_hash": "f" * 64,
            "point_in_time_verified": True,
        },
        "corporate_action": {
            "status": "CLEAR",
            "source": "fixture-actions",
            "checked_through_utc": observed.isoformat(),
            "latest_effective_at_utc": None,
            "split_factor": None,
            "evidence_hash": "e" * 64,
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
            "observation_time_exact": True,
            "market_data_timestamped": True,
            "rvol_definition_known": True,
            "float_vintage_verified": True,
            "catalyst_publication_time_verified": True,
            "corporate_action_verified": True,
            "source_fingerprint_verified": True,
            "policy_fingerprint_present": True,
            "blocking_reasons": [],
        },
    }
    payload["observation_id"] = canonical_scanner_observation_hash(payload)
    return payload


def _rehash(payload: dict[str, object]) -> None:
    payload["observation_id"] = canonical_scanner_observation_hash(payload)


def test_valid_scanner_context_is_research_only_and_causal_safe() -> None:
    parsed = parse_scanner_observation(_payload())
    assert parsed.research_only is True
    assert parsed.instrument_id == "US_EQUITY:NVDA"
    assert parsed.quality_blocking_reasons == ()
    assert parsed.causal_blocking_reasons == ()
    assert parsed.causal_research_eligible is True
    assert parsed.policy_fingerprint == "p" * 64


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
    raw_catalyst = payload["catalyst"]
    assert isinstance(raw_catalyst, Mapping)
    catalyst = dict(raw_catalyst)
    catalyst["published_at_utc"] = (observed + timedelta(seconds=1)).isoformat()
    payload["catalyst"] = catalyst
    _rehash(payload)
    with pytest.raises(ScannerContextError, match="future catalyst evidence"):
        parse_scanner_observation(payload)


def test_future_float_evidence_is_rejected() -> None:
    payload = _payload()
    observed = datetime.fromisoformat(str(payload["observed_at_utc"]))
    payload["float_as_of_utc"] = (observed + timedelta(seconds=1)).isoformat()
    _rehash(payload)
    with pytest.raises(ScannerContextError, match="future float evidence"):
        parse_scanner_observation(payload)


def test_asof_selector_never_selects_future_context() -> None:
    event_time = datetime(2026, 9, 11, 16, 1, tzinfo=UTC)
    old = parse_scanner_observation(
        _payload(observed_at=event_time - timedelta(minutes=2))
    )
    latest = parse_scanner_observation(
        _payload(observed_at=event_time - timedelta(seconds=5))
    )
    future = parse_scanner_observation(
        _payload(observed_at=event_time + timedelta(seconds=1))
    )

    selected = latest_scanner_context_before(
        (old, future, latest),
        symbol="nvda",
        source_ts_utc=event_time,
    )
    assert selected is not None
    assert selected.observation_id == latest.observation_id


def test_default_selector_skips_date_only_or_unattested_context() -> None:
    event_time = datetime(2026, 9, 11, 16, 1, tzinfo=UTC)
    payload = _payload(observed_at=event_time - timedelta(seconds=5))
    payload["observation_time_precision"] = "DATE_ONLY"
    quality = dict(payload["quality"])  # type: ignore[arg-type]
    quality["observation_time_exact"] = False
    quality["blocking_reasons"] = ["observation_time_not_exact"]
    payload["quality"] = quality
    _rehash(payload)
    unsafe = parse_scanner_observation(payload)
    assert unsafe.causal_research_eligible is False

    assert (
        latest_scanner_context_before(
            (unsafe,),
            symbol="NVDA",
            source_ts_utc=event_time,
        )
        is None
    )
    exploratory = latest_scanner_context_before(
        (unsafe,),
        symbol="NVDA",
        source_ts_utc=event_time,
        require_causal_safe=False,
    )
    assert exploratory is not None


def test_consumer_does_not_trust_empty_producer_blockers() -> None:
    payload = _payload()
    producer = dict(payload["producer"])  # type: ignore[arg-type]
    producer["policy_fingerprint"] = None
    payload["producer"] = producer
    quality = dict(payload["quality"])  # type: ignore[arg-type]
    quality["blocking_reasons"] = []
    payload["quality"] = quality
    _rehash(payload)

    parsed = parse_scanner_observation(payload)
    assert parsed.causal_research_eligible is False
    assert "policy_fingerprint_missing" in parsed.causal_blocking_reasons


def test_jsonl_loader_validates_every_row_and_rejects_duplicates(tmp_path: Path) -> None:
    payload = _payload()
    path = tmp_path / "scanner.jsonl"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    rows = load_scanner_observations(path)
    assert len(rows) == 1
    assert rows[0].symbol == "NVDA"

    path.write_text(json.dumps(payload) + "\n" + json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ScannerContextError, match="duplicate scanner observation"):
        load_scanner_observations(path)


def test_unknown_schema_is_rejected() -> None:
    payload = _payload()
    payload["schema_version"] = "scorpion.scanner-observation.v999"
    _rehash(payload)
    with pytest.raises(ScannerContextError, match="unsupported scanner schema"):
        parse_scanner_observation(payload)
