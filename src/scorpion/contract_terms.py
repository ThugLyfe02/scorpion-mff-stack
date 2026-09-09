from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ContractTerms:
    contract_key: str
    valid_from_ns: int
    valid_to_ns: int | None
    multiplier: Decimal
    deliverable: str
    adjusted: bool
    source: str

    def __post_init__(self) -> None:
        if not self.contract_key.strip():
            raise ValueError("contract_key is required")
        if self.valid_from_ns < 0:
            raise ValueError("valid_from_ns cannot be negative")
        if self.valid_to_ns is not None and self.valid_to_ns <= self.valid_from_ns:
            raise ValueError("valid_to_ns must be greater than valid_from_ns")
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive")
        if not self.source.strip():
            raise ValueError("source is required")

    def contains(self, ts_ns: int) -> bool:
        return self.valid_from_ns <= ts_ns and (
            self.valid_to_ns is None or ts_ns < self.valid_to_ns
        )

    @property
    def standard_equity_option(self) -> bool:
        return not self.adjusted and self.multiplier == Decimal("100")

    @property
    def fingerprint(self) -> str:
        material = "|".join(
            (
                self.contract_key,
                str(self.valid_from_ns),
                str(self.valid_to_ns or ""),
                str(self.multiplier),
                self.deliverable,
                "1" if self.adjusted else "0",
                self.source,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ContractTermsCoverage:
    requested: int
    covered: int
    missing: tuple[str, ...]
    ambiguous: tuple[str, ...]
    adjusted: tuple[str, ...]
    nonstandard_multiplier: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.covered == self.requested and not self.missing and not self.ambiguous

    @property
    def standard_only(self) -> bool:
        return self.complete and not self.adjusted and not self.nonstandard_multiplier


class ContractTermsRegistry:
    """Point-in-time option contract economics.

    Contract terms are deliberately separate from ticker/strike/expiry identity. A corporate
    action can change deliverables or multiplier without making the historical premium itself
    invalid. Research should therefore resolve the economic terms that were effective at the
    event timestamp rather than blindly multiplying every option premium by 100.
    """

    def __init__(self, terms: tuple[ContractTerms, ...]) -> None:
        grouped: dict[str, list[ContractTerms]] = {}
        for item in terms:
            grouped.setdefault(item.contract_key, []).append(item)
        self._terms = {
            key: tuple(sorted(items, key=lambda item: item.valid_from_ns))
            for key, items in grouped.items()
        }

    @classmethod
    def from_jsonl(cls, path: str | Path) -> ContractTermsRegistry:
        rows: list[ContractTerms] = []
        for line_number, line in enumerate(Path(path).read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                rows.append(
                    ContractTerms(
                        contract_key=str(row["contract_key"]),
                        valid_from_ns=int(row.get("valid_from_ns", 0)),
                        valid_to_ns=(
                            int(row["valid_to_ns"])
                            if row.get("valid_to_ns") is not None
                            else None
                        ),
                        multiplier=Decimal(str(row["multiplier"])),
                        deliverable=str(row.get("deliverable", "")),
                        adjusted=bool(row.get("adjusted", False)),
                        source=str(row.get("source", "UNKNOWN")),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid contract terms JSONL at line {line_number}") from exc
        return cls(tuple(rows))

    def resolve(self, contract_key: str, ts_ns: int) -> ContractTerms | None:
        matches = [item for item in self._terms.get(contract_key, ()) if item.contains(ts_ns)]
        if len(matches) != 1:
            return None
        return matches[0]

    def matching(self, contract_key: str, ts_ns: int) -> tuple[ContractTerms, ...]:
        return tuple(item for item in self._terms.get(contract_key, ()) if item.contains(ts_ns))

    def coverage(self, requests: tuple[tuple[str, int], ...]) -> ContractTermsCoverage:
        missing: set[str] = set()
        ambiguous: set[str] = set()
        adjusted: set[str] = set()
        nonstandard: set[str] = set()
        covered = 0
        for contract_key, ts_ns in requests:
            matches = self.matching(contract_key, ts_ns)
            if not matches:
                missing.add(contract_key)
                continue
            if len(matches) != 1:
                ambiguous.add(contract_key)
                continue
            covered += 1
            item = matches[0]
            if item.adjusted:
                adjusted.add(contract_key)
            if item.multiplier != Decimal("100"):
                nonstandard.add(contract_key)
        return ContractTermsCoverage(
            requested=len(requests),
            covered=covered,
            missing=tuple(sorted(missing)),
            ambiguous=tuple(sorted(ambiguous)),
            adjusted=tuple(sorted(adjusted)),
            nonstandard_multiplier=tuple(sorted(nonstandard)),
        )

    @property
    def fingerprint(self) -> str:
        material = "\n".join(
            item.fingerprint
            for key in sorted(self._terms)
            for item in self._terms[key]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
