from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scorpion.scanner_context import (
    canonical_scanner_observation_hash,
    parse_scanner_observation,
)
from scorpion.scanner_context_audit import (
    MffContextProbe,
    audit_scanner_context_coverage,
)


def _observation(
    symbol: str,
    *,
    observed: datetime,
    received: datetime,
    blockers: list[str] | None = None,
):
    payload: dict[str, object] = {
        "schema_version": "scorpion.scanner-observation.v1",
        "authority": "RESEARCH_ONLY",
        "observation_id": "0" * 64,
        "run_id": "run-audit",
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
        "selection_disposition": "SELECTED",
        "observed_at_utc": observed.isoformat(),
        "received_at_utc": received.isoformat(),
        "observation_time_precision": "EXACT",
        "market_data_as_of_utc": (observed - timedelta(seconds=1)).isoformat(),
        "market_data_source": "fixture",
        "session": "RTH",
        "last_price": 10.0,
        "prior_close": 9.0,
        "gap_pct": 11.1,
        "day_change_pct": 11.1,
        "today_volume": 1_000_000,
        "average_prior_volume": 200_000.0,
        "rvol": 5.0,
        "rvol_basis": "EXTERNAL_PROVIDED",
        "rvol_window": "external",
        "float_shares": None,
        "float_source": None,
        "float_as_of_utc": None,
        "float_point_in_time_verified": False,
        "catalyst": {
            "label": "none found",
            "confirmed": False,
            "source": "fixture",
            "source_id": None,
            "headline": None,
            "published_at_utc": None,
            "evidence_hash": None,
            "point_in_time_verified": False,
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
                "price": True,
                "rvol": True,
                "float": None,
                "news": None,
                "gap": True,
            },
            "boxes_hit": 3,
            "boxes_known": 3,
            "boxes_total": 5,
            "boxes_pass": False,
            "missed": [],
            "unchecked": ["float", "news"],
            "scorecard_line": "3/5",
        },
        "tape_flag": "MIXED",
        "tape_reason": "fixture",
        "confidence": 0.5,
        "confidence_semantics": "RANKING_HEURISTIC",
        "source_fingerprint": "a" * 64,
        "candidate_fingerprint": "b" * 64,
        "quality": {
            "observation_time_exact": True,
            "availability_time_verified": True,
            "market_data_timestamped": True,
            "rvol_definition_known": True,
            "float_vintage_verified": False,
            "catalyst_publication_time_verified": True,
            "corporate_action_verified": True,
            "source_fingerprint_verified": True,
            "policy_fingerprint_present": True,
            "blocking_reasons": blockers or [],
        },
    }
    payload["observation_id"] = canonical_scanner_observation_hash(payload)
    return parse_scanner_observation(payload)


def test_audit_separates_source_context_causal_safety_and_live_availability() -> None:
    base = datetime(2026, 9, 11, 16, 0, tzinfo=UTC)
    observations = (
        _observation(
            "NVDA",
            observed=base - timedelta(minutes=5),
            received=base - timedelta(minutes=4),
        ),
        _observation(
            "AAPL",
            observed=base + timedelta(minutes=10),
            received=base + timedelta(minutes=20),
        ),
        _observation(
            "AMD",
            observed=base + timedelta(minutes=30),
            received=base + timedelta(minutes=31),
            blockers=["float_vintage_unverified"],
        ),
    )
    probes = (
        MffContextProbe("e-nvda", "NVDA", base, base + timedelta(seconds=2)),
        MffContextProbe(
            "e-aapl",
            "AAPL",
            base + timedelta(minutes=15),
            base + timedelta(minutes=15, seconds=2),
        ),
        MffContextProbe(
            "e-amd",
            "AMD",
            base + timedelta(minutes=35),
            base + timedelta(minutes=35, seconds=2),
        ),
        MffContextProbe(
            "e-qqq",
            "QQQ",
            base + timedelta(minutes=40),
            base + timedelta(minutes=40, seconds=2),
        ),
    )

    report = audit_scanner_context_coverage(observations, probes)
    assert report.events == 4
    assert report.source_context_events == 3
    assert report.causal_safe_events == 2
    assert report.operational_available_events == 1
    assert report.backfill_only_events == 1
    assert report.source_context_rate == 0.75
    assert report.causal_safe_rate == 0.5
    assert report.operational_available_rate == 0.25
    assert ("float_vintage_unverified", 1) in report.blocker_counts

    by_event = {row.event_id: row for row in report.rows}
    assert by_event["e-aapl"].backfill_only is True
    assert by_event["e-aapl"].operational_observation_id is None
    assert by_event["e-amd"].source_context_observation_id is not None
    assert by_event["e-amd"].causal_safe_observation_id is None
    assert by_event["e-qqq"].source_context_observation_id is None


def test_probe_rejects_impossible_receive_clock() -> None:
    source = datetime(2026, 9, 11, 16, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="cannot precede"):
        MffContextProbe(
            "bad",
            "NVDA",
            source,
            source - timedelta(seconds=1),
        )
