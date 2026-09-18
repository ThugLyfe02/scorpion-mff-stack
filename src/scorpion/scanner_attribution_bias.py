from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path

from .scanner_confluence_ledger import (
    AttributionMode,
    ContextBindingStatus,
    FrozenConfluenceSnapshot,
    load_confluence_snapshots,
)


class AttributionBiasStatus(StrEnum):
    CLEAN = "CLEAN"
    INFLATED = "INFLATED"
    INCONSISTENT = "INCONSISTENT"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class AttributionBiasPolicy:
    min_paired_entries: int = 20
    max_availability_distortion_fraction: float = 0.05

    def __post_init__(self) -> None:
        if self.min_paired_entries <= 0:
            raise ValueError("min_paired_entries must be positive")
        if not 0.0 <= self.max_availability_distortion_fraction <= 1.0:
            raise ValueError(
                "max_availability_distortion_fraction must be in [0,1]"
            )


@dataclass(frozen=True, slots=True)
class AttributionBiasEntry:
    entry_event_id: str
    symbol: str
    source_snapshot_id: str
    operational_snapshot_id: str
    source_status: ContextBindingStatus
    operational_status: ContextBindingStatus
    source_observation_id: str | None
    operational_observation_id: str | None
    backfill_only: bool
    context_substitution: bool
    operational_only: bool


@dataclass(frozen=True, slots=True)
class AttributionBiasReport:
    report_id: str
    authority: str
    execution_authority: bool
    status: AttributionBiasStatus
    paired_entries: int
    source_bound_entries: int
    operational_bound_entries: int
    backfill_only_entries: int
    context_substitution_entries: int
    operational_only_entries: int
    availability_distortion_entries: int
    source_coverage_rate: float
    operational_coverage_rate: float
    coverage_inflation_fraction: float
    availability_distortion_fraction: float
    rows: tuple[AttributionBiasEntry, ...]
    findings: tuple[str, ...]

    def canonical_payload(self) -> dict[str, object]:
        return {
            "authority": self.authority,
            "execution_authority": self.execution_authority,
            "status": self.status.value,
            "paired_entries": self.paired_entries,
            "source_bound_entries": self.source_bound_entries,
            "operational_bound_entries": self.operational_bound_entries,
            "backfill_only_entries": self.backfill_only_entries,
            "context_substitution_entries": self.context_substitution_entries,
            "operational_only_entries": self.operational_only_entries,
            "availability_distortion_entries": self.availability_distortion_entries,
            "source_coverage_rate": self.source_coverage_rate,
            "operational_coverage_rate": self.operational_coverage_rate,
            "coverage_inflation_fraction": self.coverage_inflation_fraction,
            "availability_distortion_fraction": self.availability_distortion_fraction,
            "rows": [asdict(row) for row in self.rows],
            "findings": self.findings,
        }

    def verify_report_id(self) -> bool:
        return self.report_id == _hash(self.canonical_payload())


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _by_entry_and_mode(
    snapshots: tuple[FrozenConfluenceSnapshot, ...],
) -> dict[str, dict[AttributionMode, FrozenConfluenceSnapshot]]:
    indexed: dict[str, dict[AttributionMode, FrozenConfluenceSnapshot]] = {}
    for snapshot in snapshots:
        modes = indexed.setdefault(snapshot.entry_event_id, {})
        prior = modes.get(snapshot.mode)
        if prior is not None and prior != snapshot:
            raise ValueError("conflicting frozen snapshot for entry/mode")
        modes[snapshot.mode] = snapshot
    return indexed


def evaluate_attribution_availability_bias(
    path: Path | str,
    *,
    policy: AttributionBiasPolicy | None = None,
) -> AttributionBiasReport:
    """Quantify retrospective source-time context inflation.

    SOURCE_TIME answers what scanner evidence can be joined by source clock.
    OPERATIONAL answers what had actually arrived by the MFF receive clock.
    Comparing both frozen views exposes backfill-only coverage and subtler cases
    where both modes are populated but bind different scanner observations.
    """

    policy = policy or AttributionBiasPolicy()
    snapshots = load_confluence_snapshots(path)
    indexed = _by_entry_and_mode(snapshots)

    rows: list[AttributionBiasEntry] = []
    source_bound = 0
    operational_bound = 0
    backfill_only = 0
    substitutions = 0
    operational_only = 0

    for entry_event_id in sorted(indexed):
        modes = indexed[entry_event_id]
        source = modes.get(AttributionMode.SOURCE_TIME)
        operational = modes.get(AttributionMode.OPERATIONAL)
        if source is None or operational is None:
            continue
        if source.symbol != operational.symbol:
            raise ValueError("paired confluence snapshots disagree on symbol")
        if source.contract_key != operational.contract_key:
            raise ValueError("paired confluence snapshots disagree on contract_key")
        if source.mff_source_ts_utc != operational.mff_source_ts_utc:
            raise ValueError("paired confluence snapshots disagree on source clock")

        source_is_bound = source.binding_status is ContextBindingStatus.BOUND
        operational_is_bound = (
            operational.binding_status is ContextBindingStatus.BOUND
        )
        source_bound += int(source_is_bound)
        operational_bound += int(operational_is_bound)

        is_backfill_only = source_is_bound and not operational_is_bound
        is_operational_only = operational_is_bound and not source_is_bound
        is_substitution = (
            source_is_bound
            and operational_is_bound
            and source.observation_id != operational.observation_id
        )
        backfill_only += int(is_backfill_only)
        operational_only += int(is_operational_only)
        substitutions += int(is_substitution)

        rows.append(
            AttributionBiasEntry(
                entry_event_id=entry_event_id,
                symbol=source.symbol,
                source_snapshot_id=source.snapshot_id,
                operational_snapshot_id=operational.snapshot_id,
                source_status=source.binding_status,
                operational_status=operational.binding_status,
                source_observation_id=source.observation_id,
                operational_observation_id=operational.observation_id,
                backfill_only=is_backfill_only,
                context_substitution=is_substitution,
                operational_only=is_operational_only,
            )
        )

    paired = len(rows)
    source_rate = source_bound / paired if paired else 0.0
    operational_rate = operational_bound / paired if paired else 0.0
    coverage_inflation = source_rate - operational_rate
    distortion = backfill_only + substitutions
    distortion_fraction = distortion / paired if paired else 0.0

    findings: list[str] = []
    if operational_only:
        findings.append(
            "operational_only_context:"
            f"{operational_only} paired entries violate source-time dominance"
        )
    if backfill_only:
        findings.append(
            f"backfill_only_context:{backfill_only}/{paired}"
        )
    if substitutions:
        findings.append(
            f"context_substitution:{substitutions}/{paired}"
        )
    if (
        paired >= policy.min_paired_entries
        and distortion_fraction > policy.max_availability_distortion_fraction
    ):
        findings.append(
            "availability_distortion:"
            f"{distortion_fraction:.4f}>"
            f"{policy.max_availability_distortion_fraction:.4f}"
        )

    if paired < policy.min_paired_entries:
        status = AttributionBiasStatus.INSUFFICIENT
    elif operational_only:
        status = AttributionBiasStatus.INCONSISTENT
    elif distortion_fraction > policy.max_availability_distortion_fraction:
        status = AttributionBiasStatus.INFLATED
    else:
        status = AttributionBiasStatus.CLEAN

    provisional = AttributionBiasReport(
        report_id="0" * 64,
        authority="RESEARCH_ONLY",
        execution_authority=False,
        status=status,
        paired_entries=paired,
        source_bound_entries=source_bound,
        operational_bound_entries=operational_bound,
        backfill_only_entries=backfill_only,
        context_substitution_entries=substitutions,
        operational_only_entries=operational_only,
        availability_distortion_entries=distortion,
        source_coverage_rate=source_rate,
        operational_coverage_rate=operational_rate,
        coverage_inflation_fraction=coverage_inflation,
        availability_distortion_fraction=distortion_fraction,
        rows=tuple(rows),
        findings=tuple(findings),
    )
    return replace(
        provisional,
        report_id=_hash(provisional.canonical_payload()),
    )
