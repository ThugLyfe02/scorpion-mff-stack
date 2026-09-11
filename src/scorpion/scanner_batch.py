from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .scanner_context import (
    SCANNER_OBSERVATION_SCHEMA,
    SCANNER_RESEARCH_AUTHORITY,
    ScannerContextError,
    ScannerContextObservation,
    load_scanner_observations,
)

SCANNER_BATCH_SCHEMA = "scorpion.scanner-batch.v1"


@dataclass(frozen=True, slots=True)
class ScannerContextBatch:
    manifest_id: str
    run_id: str
    row_count: int
    ordered_observation_ids: tuple[str, ...]
    batch_sha256: str
    policy_fingerprint: str | None
    code_revision: str | None
    generated_at_utc: datetime
    observations: tuple[ScannerContextObservation, ...]


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ScannerContextError(f"scanner batch {key} is required")
    return value.strip()


def _parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ScannerContextError(f"{name} is required")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ScannerContextError(f"{name} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScannerContextError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def load_scanner_observation_bundle(
    *,
    batch_path: str | Path,
    manifest_path: str | Path,
) -> ScannerContextBatch:
    """Verify exact batch bytes + manifest completeness, then validate every row."""

    batch_file = Path(batch_path)
    manifest_file = Path(manifest_path)
    try:
        decoded: object = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScannerContextError("invalid scanner batch manifest JSON") from exc
    if not isinstance(decoded, dict):
        raise ScannerContextError("scanner batch manifest must be an object")
    payload: dict[str, object] = {}
    for key, value in decoded.items():
        if not isinstance(key, str):
            raise ScannerContextError("scanner batch manifest keys must be strings")
        payload[key] = value

    if _required_str(payload, "schema_version") != SCANNER_BATCH_SCHEMA:
        raise ScannerContextError("unsupported scanner batch schema")
    if _required_str(payload, "authority") != SCANNER_RESEARCH_AUTHORITY:
        raise ScannerContextError("scanner batch must be RESEARCH_ONLY")
    if _required_str(payload, "observation_schema") != SCANNER_OBSERVATION_SCHEMA:
        raise ScannerContextError("scanner batch observation schema mismatch")

    manifest_id = _required_str(payload, "manifest_id").lower()
    material = dict(payload)
    material.pop("manifest_id", None)
    expected_manifest_id = _sha256_text(_canonical_json(material))
    if manifest_id != expected_manifest_id:
        raise ScannerContextError("scanner batch manifest hash mismatch")

    raw_count = payload.get("row_count")
    if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
        raise ScannerContextError("scanner batch row_count must be a nonnegative integer")

    raw_ids = payload.get("ordered_observation_ids")
    if not isinstance(raw_ids, list) or not all(isinstance(item, str) for item in raw_ids):
        raise ScannerContextError("ordered_observation_ids must be a string array")
    ordered_ids = tuple(raw_ids)
    if len(ordered_ids) != raw_count:
        raise ScannerContextError("scanner batch manifest row count mismatch")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise ScannerContextError("scanner batch manifest contains duplicate observation IDs")

    batch_text = batch_file.read_text(encoding="utf-8")
    expected_batch_hash = _required_str(payload, "batch_sha256").lower()
    if _sha256_text(batch_text) != expected_batch_hash:
        raise ScannerContextError("scanner batch hash mismatch")

    observations = load_scanner_observations(batch_file)
    if len(observations) != raw_count:
        raise ScannerContextError("scanner batch row count mismatch")
    actual_ids = tuple(item.observation_id for item in observations)
    if actual_ids != ordered_ids:
        raise ScannerContextError("scanner batch observation identity/order mismatch")

    run_id = _required_str(payload, "run_id")
    if any(item.run_id != run_id for item in observations):
        raise ScannerContextError("scanner batch run_id mismatch")

    policy_value = payload.get("policy_fingerprint")
    policy_fingerprint = (
        policy_value.strip()
        if isinstance(policy_value, str) and policy_value.strip()
        else None
    )
    code_value = payload.get("code_revision")
    code_revision = (
        code_value.strip()
        if isinstance(code_value, str) and code_value.strip()
        else None
    )

    return ScannerContextBatch(
        manifest_id=manifest_id,
        run_id=run_id,
        row_count=raw_count,
        ordered_observation_ids=ordered_ids,
        batch_sha256=expected_batch_hash,
        policy_fingerprint=policy_fingerprint,
        code_revision=code_revision,
        generated_at_utc=_parse_utc(payload.get("generated_at_utc"), "generated_at_utc"),
        observations=observations,
    )
