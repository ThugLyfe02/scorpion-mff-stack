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
EXPECTED_SCANNER_CONTRACT_FINGERPRINT = (
    "e314fa3a571debc97e54e7aa1c8e810fe4766c64ad0e98d49e3f49781ef89f7c"
)


@dataclass(frozen=True, slots=True)
class ScannerBatchRowMetadata:
    observation_id: str
    symbol: str
    selection_disposition: str
    confidence: float
    confidence_semantics: str


@dataclass(frozen=True, slots=True)
class ScannerContextBatch:
    manifest_id: str
    run_id: str
    contract_fingerprint: str
    row_count: int
    selected_symbol_count: int
    ordered_observation_ids: tuple[str, ...]
    batch_sha256: str
    selection_scope: str
    universe_fingerprint: str | None
    symbols_attempted_count: int | None
    symbols_with_market_data_count: int | None
    error_count: int
    coverage_complete: bool
    policy_fingerprint: str | None
    code_revision: str | None
    generated_at_utc: datetime
    observations: tuple[ScannerContextObservation, ...]
    row_metadata: tuple[ScannerBatchRowMetadata, ...]

    @property
    def absence_is_interpretable(self) -> bool:
        return self.selection_scope == "FULL_UNIVERSE" and self.coverage_complete

    def metadata_for(self, observation_id: str) -> ScannerBatchRowMetadata | None:
        return next(
            (item for item in self.row_metadata if item.observation_id == observation_id),
            None,
        )


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _symbol_universe_fingerprint(symbols: list[str]) -> str:
    normalized = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
    return _sha256_text(_canonical_json(normalized))


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ScannerContextError(f"scanner batch {key} is required")
    return value.strip()


def _optional_str(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScannerContextError(f"scanner batch {name} must be string or null")
    stripped = value.strip()
    return stripped or None


def _required_nonnegative_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScannerContextError(
            f"scanner batch {key} must be a nonnegative integer"
        )
    return value


def _optional_nonnegative_int(
    payload: Mapping[str, object],
    key: str,
) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScannerContextError(
            f"scanner batch {key} must be a nonnegative integer or null"
        )
    return value


def _required_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ScannerContextError(f"scanner batch {key} must be boolean")
    return value


def _required_confidence(payload: Mapping[str, object]) -> float:
    value = payload.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScannerContextError("scanner confidence must be numeric")
    confidence = float(value)
    if confidence < 0.0 or confidence > 1.0:
        raise ScannerContextError("scanner confidence must be in [0,1]")
    return confidence


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


def _decode_batch_rows(batch_text: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(batch_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            decoded: object = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ScannerContextError(
                f"invalid scanner batch row JSON at line {line_number}"
            ) from exc
        if not isinstance(decoded, dict):
            raise ScannerContextError(
                f"scanner batch row {line_number} must be an object"
            )
        row: dict[str, object] = {}
        for key, value in decoded.items():
            if not isinstance(key, str):
                raise ScannerContextError(
                    f"scanner batch row {line_number} has non-string key"
                )
            row[key] = value
        rows.append(row)
    return rows


def _validate_selection_semantics(
    raw_rows: list[dict[str, object]],
    *,
    selection_scope: str,
    coverage_complete: bool,
    attempted_count: int | None,
    universe_fingerprint: str | None,
) -> tuple[int, tuple[ScannerBatchRowMetadata, ...]]:
    selected_symbols: set[str] = set()
    symbols: list[str] = []
    metadata: list[ScannerBatchRowMetadata] = []
    for row in raw_rows:
        observation_id = _required_str(row, "observation_id")
        symbol = _required_str(row, "symbol").upper()
        disposition = _required_str(row, "selection_disposition")
        if disposition not in {"SELECTED", "NOT_SELECTED", "UNKNOWN"}:
            raise ScannerContextError("unsupported scanner selection_disposition")
        confidence_semantics = _required_str(row, "confidence_semantics")
        if confidence_semantics != "RANKING_HEURISTIC":
            raise ScannerContextError(
                "scanner confidence must remain RANKING_HEURISTIC"
            )
        confidence = _required_confidence(row)
        metadata.append(
            ScannerBatchRowMetadata(
                observation_id=observation_id,
                symbol=symbol,
                selection_disposition=disposition,
                confidence=confidence,
                confidence_semantics=confidence_semantics,
            )
        )
        symbols.append(symbol)
        if disposition == "SELECTED":
            selected_symbols.add(symbol)
        if selection_scope == "HITS_ONLY" and disposition != "SELECTED":
            raise ScannerContextError(
                "HITS_ONLY scanner batch may contain only SELECTED observations"
            )
        if selection_scope == "FULL_UNIVERSE" and disposition == "UNKNOWN":
            raise ScannerContextError(
                "FULL_UNIVERSE scanner batch cannot contain UNKNOWN selection disposition"
            )

    if selection_scope == "FULL_UNIVERSE" and coverage_complete:
        if attempted_count is None:
            raise ScannerContextError(
                "complete FULL_UNIVERSE batch requires attempted count"
            )
        if len(symbols) != attempted_count:
            raise ScannerContextError(
                "complete FULL_UNIVERSE row count must equal attempted count"
            )
        if len(symbols) != len(set(symbols)):
            raise ScannerContextError(
                "complete FULL_UNIVERSE requires exactly one row per symbol"
            )
        if universe_fingerprint is None:
            raise ScannerContextError(
                "complete FULL_UNIVERSE requires universe_fingerprint"
            )
        if _symbol_universe_fingerprint(symbols) != universe_fingerprint:
            raise ScannerContextError("scanner batch universe fingerprint mismatch")

    return len(selected_symbols), tuple(metadata)


def load_scanner_observation_bundle(
    *,
    batch_path: str | Path,
    manifest_path: str | Path,
) -> ScannerContextBatch:
    """Verify semantic contract, bytes, completeness, then every observation."""

    batch_file = Path(batch_path)
    manifest_file = Path(manifest_path)
    try:
        decoded: object = json.loads(
            manifest_file.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as exc:
        raise ScannerContextError(
            "invalid scanner batch manifest JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise ScannerContextError("scanner batch manifest must be an object")
    payload: dict[str, object] = {}
    for key, value in decoded.items():
        if not isinstance(key, str):
            raise ScannerContextError(
                "scanner batch manifest keys must be strings"
            )
        payload[key] = value

    if _required_str(payload, "schema_version") != SCANNER_BATCH_SCHEMA:
        raise ScannerContextError("unsupported scanner batch schema")
    if _required_str(payload, "authority") != SCANNER_RESEARCH_AUTHORITY:
        raise ScannerContextError("scanner batch must be RESEARCH_ONLY")
    observation_schema = _required_str(payload, "observation_schema")
    if observation_schema != SCANNER_OBSERVATION_SCHEMA:
        raise ScannerContextError(
            "scanner batch observation schema mismatch"
        )
    contract_fingerprint = _required_str(payload, "contract_fingerprint").lower()
    if contract_fingerprint != EXPECTED_SCANNER_CONTRACT_FINGERPRINT:
        raise ScannerContextError(
            "scanner semantic contract fingerprint mismatch"
        )

    manifest_id = _required_str(payload, "manifest_id").lower()
    material = dict(payload)
    material.pop("manifest_id", None)
    expected_manifest_id = _sha256_text(_canonical_json(material))
    if manifest_id != expected_manifest_id:
        raise ScannerContextError("scanner batch manifest hash mismatch")

    row_count = _required_nonnegative_int(payload, "row_count")
    selected_count = _required_nonnegative_int(
        payload,
        "selected_symbol_count",
    )
    if selected_count > row_count:
        raise ScannerContextError(
            "scanner batch selected_symbol_count cannot exceed row_count"
        )

    raw_ids = payload.get("ordered_observation_ids")
    if not isinstance(raw_ids, list) or not all(
        isinstance(item, str) for item in raw_ids
    ):
        raise ScannerContextError(
            "ordered_observation_ids must be a string array"
        )
    ordered_ids = tuple(raw_ids)
    if len(ordered_ids) != row_count:
        raise ScannerContextError(
            "scanner batch manifest row count mismatch"
        )
    if len(set(ordered_ids)) != len(ordered_ids):
        raise ScannerContextError(
            "scanner batch manifest contains duplicate observation IDs"
        )

    selection_scope = _required_str(payload, "selection_scope")
    if selection_scope not in {"HITS_ONLY", "FULL_UNIVERSE"}:
        raise ScannerContextError("unsupported scanner selection_scope")
    attempted_count = _optional_nonnegative_int(
        payload,
        "symbols_attempted_count",
    )
    with_data_count = _optional_nonnegative_int(
        payload,
        "symbols_with_market_data_count",
    )
    error_count = _required_nonnegative_int(payload, "error_count")
    coverage_complete = _required_bool(payload, "coverage_complete")
    universe_fingerprint = _optional_str(
        payload.get("universe_fingerprint"),
        "universe_fingerprint",
    )
    if (
        attempted_count is not None
        and with_data_count is not None
        and with_data_count > attempted_count
    ):
        raise ScannerContextError(
            "scanner market-data count cannot exceed attempted count"
        )
    if coverage_complete:
        if selection_scope != "FULL_UNIVERSE":
            raise ScannerContextError(
                "coverage_complete is valid only for FULL_UNIVERSE batches"
            )
        if attempted_count is None or with_data_count is None:
            raise ScannerContextError(
                "complete scanner coverage requires explicit counts"
            )
        if attempted_count != with_data_count or error_count != 0:
            raise ScannerContextError(
                "scanner coverage_complete contradicts counts/errors"
            )
        if row_count != attempted_count:
            raise ScannerContextError(
                "complete FULL_UNIVERSE row count must equal attempted count"
            )

    batch_text = batch_file.read_text(encoding="utf-8")
    expected_batch_hash = _required_str(payload, "batch_sha256").lower()
    if _sha256_text(batch_text) != expected_batch_hash:
        raise ScannerContextError("scanner batch hash mismatch")

    raw_rows = _decode_batch_rows(batch_text)
    actual_selected, row_metadata = _validate_selection_semantics(
        raw_rows,
        selection_scope=selection_scope,
        coverage_complete=coverage_complete,
        attempted_count=attempted_count,
        universe_fingerprint=universe_fingerprint,
    )
    if actual_selected != selected_count:
        raise ScannerContextError(
            "scanner batch selected_symbol_count mismatch"
        )

    observations = load_scanner_observations(batch_file)
    if len(observations) != row_count:
        raise ScannerContextError("scanner batch row count mismatch")
    actual_ids = tuple(item.observation_id for item in observations)
    if actual_ids != ordered_ids:
        raise ScannerContextError(
            "scanner batch observation identity/order mismatch"
        )
    if tuple(item.observation_id for item in row_metadata) != actual_ids:
        raise ScannerContextError("scanner row metadata identity/order mismatch")

    run_id = _required_str(payload, "run_id")
    if any(item.run_id != run_id for item in observations):
        raise ScannerContextError("scanner batch run_id mismatch")

    return ScannerContextBatch(
        manifest_id=manifest_id,
        run_id=run_id,
        contract_fingerprint=contract_fingerprint,
        row_count=row_count,
        selected_symbol_count=selected_count,
        ordered_observation_ids=ordered_ids,
        batch_sha256=expected_batch_hash,
        selection_scope=selection_scope,
        universe_fingerprint=universe_fingerprint,
        symbols_attempted_count=attempted_count,
        symbols_with_market_data_count=with_data_count,
        error_count=error_count,
        coverage_complete=coverage_complete,
        policy_fingerprint=_optional_str(
            payload.get("policy_fingerprint"),
            "policy_fingerprint",
        ),
        code_revision=_optional_str(
            payload.get("code_revision"),
            "code_revision",
        ),
        generated_at_utc=_parse_utc(
            payload.get("generated_at_utc"),
            "generated_at_utc",
        ),
        observations=observations,
        row_metadata=row_metadata,
    )
