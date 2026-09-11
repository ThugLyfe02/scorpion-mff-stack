from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCANNER_OBSERVATION_SCHEMA = "scorpion.scanner-observation.v1"
SCANNER_RESEARCH_AUTHORITY = "RESEARCH_ONLY"


class ScannerContextError(ValueError):
    """Raised when external scanner research context violates its contract."""


@dataclass(frozen=True, slots=True)
class ScannerContextObservation:
    """Validated, non-authoritative stock-finder context.

    This type intentionally has no Effect/SignalEvent/order conversion and no
    dependency on the MFF reducer or execution modules.
    """

    observation_id: str
    run_id: str
    symbol: str
    instrument_id: str
    category: str
    observed_at_utc: datetime
    observation_time_precision: str
    market_data_as_of_utc: datetime | None
    market_data_source: str | None
    session: str
    ross_boxes_hit: int
    ross_boxes_known: int
    rvol: float | None
    rvol_basis: str
    tape_flag: str
    policy_fingerprint: str | None
    quality_blocking_reasons: tuple[str, ...]
    causal_blocking_reasons: tuple[str, ...]

    @property
    def research_only(self) -> bool:
        return True

    @property
    def causal_research_eligible(self) -> bool:
        return not self.causal_blocking_reasons


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_scanner_observation_hash(payload: Mapping[str, object]) -> str:
    material = dict(payload)
    material.pop("observation_id", None)
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ScannerContextError(f"{name} must be an object")
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ScannerContextError(f"{name} keys must be strings")
        normalized[key] = item
    return normalized


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ScannerContextError(f"{key} is required")
    return value.strip()


def _optional_str(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScannerContextError(f"{name} must be a string or null")
    stripped = value.strip()
    return stripped or None


def _optional_float(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScannerContextError(f"{name} must be numeric or null")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise ScannerContextError(f"{name} must be finite")
    return result


def _required_int(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 5,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScannerContextError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ScannerContextError(f"{name} out of range")
    return value


def _parse_aware_utc(value: object, name: str) -> datetime:
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


def _optional_aware_utc(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_aware_utc(value, name)


def _required_bool(mapping: Mapping[str, object], key: str, *, default: bool = False) -> bool:
    value = mapping.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ScannerContextError(f"{key} must be boolean")
    return value


def _quality_blockers(payload: Mapping[str, object]) -> tuple[str, ...]:
    quality_value = payload.get("quality")
    if quality_value is None:
        return ()
    quality = _mapping(quality_value, "quality")
    raw = quality.get("blocking_reasons")
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ScannerContextError("quality.blocking_reasons must be an array")
    blockers: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ScannerContextError("quality.blocking_reasons entries must be strings")
        blockers.append(item)
    return tuple(blockers)


def _derive_causal_blockers(
    payload: Mapping[str, object],
    *,
    observed_at: datetime,
    market_at: datetime | None,
) -> tuple[str, ...]:
    """Independently derive the minimum evidence gates MFF will trust."""

    blockers: set[str] = set(_quality_blockers(payload))

    if payload.get("observation_time_precision") != "EXACT":
        blockers.add("observation_time_not_exact")
    if market_at is None:
        blockers.add("market_data_timestamp_missing")

    rvol = payload.get("rvol")
    rvol_basis = payload.get("rvol_basis")
    if rvol is not None and (not isinstance(rvol_basis, str) or rvol_basis == "UNKNOWN"):
        blockers.add("rvol_definition_unknown")

    producer = _mapping(payload.get("producer"), "producer")
    policy_fingerprint = producer.get("policy_fingerprint")
    if not isinstance(policy_fingerprint, str) or not policy_fingerprint.strip():
        blockers.add("policy_fingerprint_missing")

    source_fingerprint = payload.get("source_fingerprint")
    quality_value = payload.get("quality")
    quality = _mapping(quality_value, "quality") if quality_value is not None else {}
    source_verified = _required_bool(quality, "source_fingerprint_verified", default=False)
    if not isinstance(source_fingerprint, str) or not source_fingerprint.strip() or not source_verified:
        blockers.add("source_fingerprint_unverified")

    float_shares = payload.get("float_shares")
    float_as_of = _optional_aware_utc(payload.get("float_as_of_utc"), "float_as_of_utc")
    float_verified = payload.get("float_point_in_time_verified") is True
    if float_shares is not None:
        if not float_verified or float_as_of is None:
            blockers.add("float_vintage_unverified")
        elif float_as_of > observed_at:
            raise ScannerContextError("future float evidence relative to scanner observation")

    catalyst_value = payload.get("catalyst")
    if catalyst_value is not None:
        catalyst = _mapping(catalyst_value, "catalyst")
        confirmed = catalyst.get("confirmed") is True
        verified = catalyst.get("point_in_time_verified") is True
        published_at = _optional_aware_utc(
            catalyst.get("published_at_utc"),
            "catalyst.published_at_utc",
        )
        if confirmed and (not verified or published_at is None):
            blockers.add("catalyst_publication_time_unverified")
        if verified and published_at is None:
            raise ScannerContextError("verified catalyst requires publication timestamp")
        if verified and published_at is not None and published_at > observed_at:
            raise ScannerContextError("future catalyst evidence relative to scanner observation")

    corporate_value = payload.get("corporate_action")
    if corporate_value is None:
        blockers.add("corporate_action_not_verified")
    else:
        corporate = _mapping(corporate_value, "corporate_action")
        verified = corporate.get("point_in_time_verified") is True
        checked_through = _optional_aware_utc(
            corporate.get("checked_through_utc"),
            "corporate_action.checked_through_utc",
        )
        if not verified or checked_through is None or checked_through < observed_at:
            blockers.add("corporate_action_not_verified")

    return tuple(sorted(blockers))


def parse_scanner_observation(payload: Mapping[str, object]) -> ScannerContextObservation:
    """Validate one stock-finder event for research/context use only."""

    schema = _required_str(payload, "schema_version")
    if schema != SCANNER_OBSERVATION_SCHEMA:
        raise ScannerContextError(f"unsupported scanner schema: {schema}")

    authority = _required_str(payload, "authority")
    if authority != SCANNER_RESEARCH_AUTHORITY:
        raise ScannerContextError("scanner context must be RESEARCH_ONLY")

    observation_id = _required_str(payload, "observation_id").lower()
    invalid_hex = any(ch not in "0123456789abcdef" for ch in observation_id)
    if len(observation_id) != 64 or invalid_hex:
        raise ScannerContextError("observation_id must be a SHA-256 hex digest")
    expected_hash = canonical_scanner_observation_hash(payload)
    if observation_id != expected_hash:
        raise ScannerContextError("scanner observation hash mismatch")

    symbol = _required_str(payload, "symbol").upper()
    instrument_id = _required_str(payload, "instrument_id")
    expected_instrument = f"US_EQUITY:{symbol}"
    if instrument_id != expected_instrument:
        raise ScannerContextError(f"instrument_id must be {expected_instrument}")

    observed_at = _parse_aware_utc(payload.get("observed_at_utc"), "observed_at_utc")
    market_at = _optional_aware_utc(
        payload.get("market_data_as_of_utc"),
        "market_data_as_of_utc",
    )
    if market_at is not None and market_at > observed_at:
        raise ScannerContextError("future market evidence relative to scanner observation")

    ross = _mapping(payload.get("ross"), "ross")
    boxes_hit = _required_int(ross.get("boxes_hit"), "ross.boxes_hit")
    boxes_known = _required_int(ross.get("boxes_known"), "ross.boxes_known")
    if boxes_hit > boxes_known:
        raise ScannerContextError("ross.boxes_hit cannot exceed ross.boxes_known")

    producer = _mapping(payload.get("producer"), "producer")
    if producer.get("producer") != "scorpion-stock-finder":
        raise ScannerContextError("unexpected scanner producer")
    package_version = producer.get("package_version")
    if not isinstance(package_version, str) or not package_version.strip():
        raise ScannerContextError("producer.package_version is required")
    policy_fingerprint = _optional_str(producer.get("policy_fingerprint"), "policy_fingerprint")

    precision_value = payload.get("observation_time_precision")
    precision = precision_value if isinstance(precision_value, str) else "UNKNOWN"
    session_value = payload.get("session")
    session = session_value if isinstance(session_value, str) else "UNKNOWN"
    rvol_basis_value = payload.get("rvol_basis")
    rvol_basis = rvol_basis_value if isinstance(rvol_basis_value, str) else "UNKNOWN"
    tape_value = payload.get("tape_flag")
    tape_flag = tape_value if isinstance(tape_value, str) else "UNKNOWN"

    causal_blockers = _derive_causal_blockers(
        payload,
        observed_at=observed_at,
        market_at=market_at,
    )

    return ScannerContextObservation(
        observation_id=observation_id,
        run_id=_required_str(payload, "run_id"),
        symbol=symbol,
        instrument_id=instrument_id,
        category=_required_str(payload, "category"),
        observed_at_utc=observed_at,
        observation_time_precision=precision,
        market_data_as_of_utc=market_at,
        market_data_source=_optional_str(payload.get("market_data_source"), "market_data_source"),
        session=session,
        ross_boxes_hit=boxes_hit,
        ross_boxes_known=boxes_known,
        rvol=_optional_float(payload.get("rvol"), "rvol"),
        rvol_basis=rvol_basis,
        tape_flag=tape_flag,
        policy_fingerprint=policy_fingerprint,
        quality_blocking_reasons=_quality_blockers(payload),
        causal_blocking_reasons=causal_blockers,
    )


def load_scanner_observations(path: str | Path) -> tuple[ScannerContextObservation, ...]:
    """Load and validate an immutable stock-finder JSONL research batch."""

    rows: list[ScannerContextObservation] = []
    seen: set[str] = set()
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            decoded: object = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ScannerContextError(f"invalid scanner JSONL at line {line_number}") from exc
        if not isinstance(decoded, dict):
            raise ScannerContextError(f"scanner JSONL line {line_number} must be an object")
        payload: dict[str, object] = {}
        for key, value in decoded.items():
            if not isinstance(key, str):
                raise ScannerContextError(
                    f"scanner JSONL line {line_number} has non-string key"
                )
            payload[key] = value
        try:
            parsed = parse_scanner_observation(payload)
        except ScannerContextError as exc:
            raise ScannerContextError(f"scanner JSONL line {line_number}: {exc}") from exc
        if parsed.observation_id in seen:
            raise ScannerContextError(f"duplicate scanner observation at line {line_number}")
        seen.add(parsed.observation_id)
        rows.append(parsed)
    return tuple(rows)


def latest_scanner_context_before(
    observations: Iterable[ScannerContextObservation],
    *,
    symbol: str,
    source_ts_utc: datetime,
    require_causal_safe: bool = True,
) -> ScannerContextObservation | None:
    """Return latest past-only context; causal-safe evidence is required by default."""

    if source_ts_utc.tzinfo is None or source_ts_utc.utcoffset() is None:
        raise ScannerContextError("MFF source_ts_utc must be timezone-aware")
    cutoff = source_ts_utc.astimezone(UTC)
    instrument_id = f"US_EQUITY:{symbol.strip().upper()}"
    eligible = (
        item
        for item in observations
        if item.instrument_id == instrument_id
        and item.observed_at_utc <= cutoff
        and (item.causal_research_eligible or not require_causal_safe)
    )
    return max(
        eligible,
        key=lambda item: (item.observed_at_utc, item.observation_id),
        default=None,
    )
