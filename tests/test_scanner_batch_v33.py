from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scorpion.scanner_batch import load_scanner_observation_bundle
from scorpion.scanner_context import (
    ScannerContextError,
    canonical_scanner_observation_hash,
)


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _observation(symbol: str, run_id: str) -> dict[str, object]:
    observed = datetime(2026, 9, 11, 16, 0, tzinfo=UTC)
    payload: dict[str, object] = {
        "schema_version": "scorpion.scanner-observation.v1",
        "authority": "RESEARCH_ONLY",
        "observation_id": "0" * 64,
        "run_id": run_id,
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
            "boxes": {"price": True, "rvol": True, "float": None, "news": None, "gap": True},
            "boxes_hit": 3,
            "boxes_known": 3,
            "boxes_total": 5,
            "boxes_pass": False,
            "missed": [],
            "unchecked": ["float", "news"],
            "scorecard_line": "3/5 — unchecked float, news",
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
            "float_vintage_verified": False,
            "catalyst_publication_time_verified": True,
            "corporate_action_verified": True,
            "source_fingerprint_verified": True,
            "policy_fingerprint_present": True,
            "blocking_reasons": [],
        },
    }
    payload["observation_id"] = canonical_scanner_observation_hash(payload)
    return payload


def _write_bundle(tmp_path: Path) -> tuple[Path, Path]:
    rows = [_observation("AENT", "run-1"), _observation("TNON", "run-1")]
    batch_text = "".join(_canonical(row) + "\n" for row in rows)
    batch = tmp_path / "run-1.jsonl"
    batch.write_text(batch_text, encoding="utf-8")

    manifest: dict[str, object] = {
        "schema_version": "scorpion.scanner-batch.v1",
        "authority": "RESEARCH_ONLY",
        "manifest_id": "0" * 64,
        "run_id": "run-1",
        "observation_schema": "scorpion.scanner-observation.v1",
        "row_count": 2,
        "ordered_observation_ids": [row["observation_id"] for row in rows],
        "batch_sha256": hashlib.sha256(batch_text.encode("utf-8")).hexdigest(),
        "policy_fingerprint": "p" * 64,
        "code_revision": "fixture",
        "generated_at_utc": datetime(2026, 9, 11, 16, 1, tzinfo=UTC).isoformat(),
    }
    material = dict(manifest)
    material.pop("manifest_id")
    manifest["manifest_id"] = hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()
    manifest_path = tmp_path / "run-1.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return batch, manifest_path


def test_bundle_verifies_complete_ordered_run(tmp_path: Path) -> None:
    batch, manifest = _write_bundle(tmp_path)
    loaded = load_scanner_observation_bundle(batch_path=batch, manifest_path=manifest)
    assert loaded.row_count == 2
    assert [row.symbol for row in loaded.observations] == ["AENT", "TNON"]
    assert loaded.policy_fingerprint == "p" * 64


def test_bundle_rejects_truncation(tmp_path: Path) -> None:
    batch, manifest = _write_bundle(tmp_path)
    first = batch.read_text(encoding="utf-8").splitlines()[0]
    batch.write_text(first + "\n", encoding="utf-8")
    with pytest.raises(ScannerContextError, match="batch hash mismatch"):
        load_scanner_observation_bundle(batch_path=batch, manifest_path=manifest)


def test_bundle_rejects_manifest_tampering(tmp_path: Path) -> None:
    batch, manifest = _write_bundle(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["row_count"] = 1
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ScannerContextError, match="manifest hash mismatch"):
        load_scanner_observation_bundle(batch_path=batch, manifest_path=manifest)
