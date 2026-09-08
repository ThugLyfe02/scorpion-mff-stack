from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Sequence

from .domain import BookState, EventKind, PositionStatus, SignalEvent


class SequenceSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class SequenceFinding:
    code: str
    severity: SequenceSeverity
    detail: str


@dataclass(frozen=True, slots=True)
class SequenceAssessment:
    findings: tuple[SequenceFinding, ...]

    @property
    def requires_review(self) -> bool:
        return any(
            finding.severity in {SequenceSeverity.WARNING, SequenceSeverity.CRITICAL}
            for finding in self.findings
        )

    @property
    def critical(self) -> bool:
        return any(finding.severity is SequenceSeverity.CRITICAL for finding in self.findings)


def _position_status(state: BookState, contract_key: str | None) -> PositionStatus | None:
    if contract_key is None:
        return None
    position = state.positions.get(contract_key)
    return position.status if position is not None else None


def assess_sequence(
    event: SignalEvent,
    state: BookState,
    *,
    recent_events: Sequence[SignalEvent] = (),
    stale_tolerance: timedelta = timedelta(seconds=2),
    rapid_reversal_window: timedelta = timedelta(seconds=3),
) -> SequenceAssessment:
    findings: list[SequenceFinding] = []
    status = _position_status(state, event.contract_key)

    if event.kind in {EventKind.ADD, EventKind.TRIM, EventKind.EXIT}:
        if event.contract_key is None:
            findings.append(
                SequenceFinding(
                    "followup_without_contract",
                    SequenceSeverity.CRITICAL,
                    f"{event.kind.value} has no resolved contract",
                )
            )
        elif status not in {PositionStatus.PENDING_ENTRY, PositionStatus.OPEN, PositionStatus.CLOSING}:
            findings.append(
                SequenceFinding(
                    "orphan_followup",
                    SequenceSeverity.CRITICAL,
                    f"{event.kind.value} targets position status={status}",
                )
            )

    if event.kind is EventKind.ENTRY and status in {
        PositionStatus.PENDING_ENTRY,
        PositionStatus.OPEN,
        PositionStatus.CLOSING,
    }:
        findings.append(
            SequenceFinding(
                "entry_while_live",
                SequenceSeverity.WARNING,
                f"entry targets already-live position status={status.value}",
            )
        )

    same_source = [
        prior
        for prior in recent_events
        if prior.author_id == event.author_id and prior.channel_id == event.channel_id
    ]
    if same_source:
        newest = max(same_source, key=lambda prior: prior.source_ts_utc)
        if event.source_ts_utc + stale_tolerance < newest.source_ts_utc:
            lag = (newest.source_ts_utc - event.source_ts_utc).total_seconds()
            findings.append(
                SequenceFinding(
                    "source_timestamp_regression",
                    SequenceSeverity.WARNING,
                    f"event is {lag:.3f}s older than newer source event",
                )
            )

    same_contract = [
        prior
        for prior in recent_events
        if event.contract_key is not None and prior.contract_key == event.contract_key
    ]
    if same_contract:
        newest_contract = max(same_contract, key=lambda prior: prior.source_ts_utc)
        elapsed = event.source_ts_utc - newest_contract.source_ts_utc
        if (
            elapsed >= timedelta(0)
            and elapsed <= rapid_reversal_window
            and newest_contract.kind is EventKind.ENTRY
            and event.kind is EventKind.EXIT
        ):
            findings.append(
                SequenceFinding(
                    "rapid_entry_exit_reversal",
                    SequenceSeverity.WARNING,
                    f"entry→exit reversal in {elapsed.total_seconds():.3f}s",
                )
            )
        if newest_contract.kind is EventKind.EXIT and event.kind in {EventKind.ADD, EventKind.TRIM}:
            findings.append(
                SequenceFinding(
                    "post_exit_followup",
                    SequenceSeverity.CRITICAL,
                    f"{event.kind.value} follows EXIT for the same contract",
                )
            )

    return SequenceAssessment(tuple(findings))
