from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, cast

MANIFEST_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ResearchManifest:
    manifest_version: str
    code_revision: str
    package_version: str
    parser_version: str
    policy_hashes: tuple[tuple[str, str], ...]
    dataset_hashes: tuple[tuple[str, str], ...]
    parameters: tuple[tuple[str, str], ...]
    created_ts_utc: datetime
    parent_manifest_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.code_revision.strip():
            raise ValueError("code_revision is required")
        if self.created_ts_utc.tzinfo is None or self.created_ts_utc.utcoffset() is None:
            raise ValueError("created_ts_utc must be timezone-aware")
        object.__setattr__(self, "created_ts_utc", self.created_ts_utc.astimezone(UTC))

    def payload(self) -> dict[str, object]:
        return {
            "manifest_version": self.manifest_version,
            "code_revision": self.code_revision,
            "package_version": self.package_version,
            "parser_version": self.parser_version,
            "policy_hashes": dict(self.policy_hashes),
            "dataset_hashes": dict(self.dataset_hashes),
            "parameters": dict(self.parameters),
            "created_ts_utc": self.created_ts_utc.isoformat(),
            "parent_manifest_hash": self.parent_manifest_hash,
        }

    @property
    def manifest_hash(self) -> str:
        return stable_hash(self.payload())


def _normalize(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("non-finite floats cannot be canonicalized")
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime cannot be canonicalized")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize(asdict(cast(Any, value)))
    if isinstance(value, Mapping):
        return {
            str(key): _normalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize(item) for item in value]
        return sorted(normalized, key=canonical_json)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize(item) for item in value]
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def stable_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def fingerprint_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> FileFingerprint:
    file_path = Path(path)
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    size = 0
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return FileFingerprint(str(file_path), size, digest.hexdigest())


def parameter_pairs(values: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(key), canonical_json(value))
        for key, value in sorted(values.items(), key=lambda item: str(item[0]))
    )


def hash_pairs(values: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(key), stable_hash(value))
        for key, value in sorted(values.items(), key=lambda item: str(item[0]))
    )


def build_research_manifest(
    *,
    code_revision: str,
    package_version: str,
    parser_version: str,
    policies: Mapping[str, object],
    datasets: Mapping[str, str],
    parameters: Mapping[str, object],
    created_ts_utc: datetime | None = None,
    parent_manifest_hash: str | None = None,
) -> ResearchManifest:
    return ResearchManifest(
        manifest_version=MANIFEST_VERSION,
        code_revision=code_revision,
        package_version=package_version,
        parser_version=parser_version,
        policy_hashes=hash_pairs(policies),
        dataset_hashes=tuple(sorted((str(key), value) for key, value in datasets.items())),
        parameters=parameter_pairs(parameters),
        created_ts_utc=(created_ts_utc or datetime.now(UTC)),
        parent_manifest_hash=parent_manifest_hash,
    )


def verify_manifest(manifest: ResearchManifest, expected_hash: str) -> bool:
    return manifest.manifest_hash == expected_hash
