from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class InstrumentIdentity:
    raw_symbol: str
    contract_key: str
    valid_from_ns: int
    valid_to_ns: int | None
    source: str
    instrument_id: int | None = None
    adjusted: bool = False

    def __post_init__(self) -> None:
        if not self.raw_symbol.strip() or not self.contract_key.strip():
            raise ValueError("raw_symbol and contract_key are required")
        if self.valid_from_ns < 0:
            raise ValueError("valid_from_ns cannot be negative")
        if self.valid_to_ns is not None and self.valid_to_ns <= self.valid_from_ns:
            raise ValueError("valid_to_ns must be greater than valid_from_ns")
        if not self.source.strip():
            raise ValueError("source is required")

    def contains(self, ts_ns: int) -> bool:
        return self.valid_from_ns <= ts_ns and (
            self.valid_to_ns is None or ts_ns < self.valid_to_ns
        )

    @property
    def fingerprint(self) -> str:
        material = "|".join(
            (
                self.raw_symbol,
                self.contract_key,
                str(self.valid_from_ns),
                str(self.valid_to_ns or ""),
                str(self.instrument_id or ""),
                "1" if self.adjusted else "0",
                self.source,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IdentityCoverage:
    requested: int
    resolved: int
    missing: tuple[str, ...]
    ambiguous: tuple[str, ...]
    adjusted: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.resolved == self.requested and not self.missing and not self.ambiguous

    @property
    def standard_only(self) -> bool:
        return self.complete and not self.adjusted


class InstrumentIdentityRegistry:
    """Point-in-time mapping from vendor symbols to Scorpion canonical contracts.

    Vendor/OCC roots are evidence, not an assumption that the root always equals the Discord
    underlying. Adjusted/special series can use different roots while still referring to an
    underlying that Scorpion identifies by the canonical ``ticker|side|strike|expiry`` key.
    """

    def __init__(self, identities: tuple[InstrumentIdentity, ...]) -> None:
        grouped: dict[str, list[InstrumentIdentity]] = {}
        for identity in identities:
            grouped.setdefault(identity.raw_symbol.strip().upper(), []).append(identity)
        self._identities = {
            symbol: tuple(sorted(items, key=lambda item: item.valid_from_ns))
            for symbol, items in grouped.items()
        }

    @classmethod
    def from_jsonl(cls, path: str | Path) -> InstrumentIdentityRegistry:
        identities: list[InstrumentIdentity] = []
        for line_number, line in enumerate(Path(path).read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                identities.append(
                    InstrumentIdentity(
                        raw_symbol=str(row["raw_symbol"]).strip().upper(),
                        contract_key=str(row["contract_key"]),
                        valid_from_ns=int(row.get("valid_from_ns", 0)),
                        valid_to_ns=(
                            int(row["valid_to_ns"])
                            if row.get("valid_to_ns") is not None
                            else None
                        ),
                        source=str(row.get("source", "UNKNOWN")),
                        instrument_id=(
                            int(row["instrument_id"])
                            if row.get("instrument_id") is not None
                            else None
                        ),
                        adjusted=bool(row.get("adjusted", False)),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid instrument identity JSONL at line {line_number}") from exc
        return cls(tuple(identities))

    def matching(self, raw_symbol: str, ts_ns: int) -> tuple[InstrumentIdentity, ...]:
        symbol = raw_symbol.strip().upper()
        return tuple(
            item for item in self._identities.get(symbol, ()) if item.contains(ts_ns)
        )

    def resolve(self, raw_symbol: str, ts_ns: int) -> InstrumentIdentity | None:
        matches = self.matching(raw_symbol, ts_ns)
        return matches[0] if len(matches) == 1 else None

    def coverage(self, requests: tuple[tuple[str, int], ...]) -> IdentityCoverage:
        missing: set[str] = set()
        ambiguous: set[str] = set()
        adjusted: set[str] = set()
        resolved = 0
        for raw_symbol, ts_ns in requests:
            matches = self.matching(raw_symbol, ts_ns)
            key = raw_symbol.strip().upper()
            if not matches:
                missing.add(key)
                continue
            if len(matches) != 1:
                ambiguous.add(key)
                continue
            resolved += 1
            if matches[0].adjusted:
                adjusted.add(key)
        return IdentityCoverage(
            requested=len(requests),
            resolved=resolved,
            missing=tuple(sorted(missing)),
            ambiguous=tuple(sorted(ambiguous)),
            adjusted=tuple(sorted(adjusted)),
        )

    @property
    def fingerprint(self) -> str:
        material = "\n".join(
            identity.fingerprint
            for symbol in sorted(self._identities)
            for identity in self._identities[symbol]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
