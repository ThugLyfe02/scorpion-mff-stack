from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .microstructure import OptionMarketEvent


class FeedContinuityMode(StrEnum):
    PROVIDER_CERTIFIED = "PROVIDER_CERTIFIED"
    FILTERED_UNPROVEN = "FILTERED_UNPROVEN"
    UNKNOWN = "UNKNOWN"


class FeedIntegrityStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class FeedIntegrityPolicy:
    maximum_negative_capture_latency_events: int = 0
    maximum_sequence_regressions: int = 0
    maximum_conflicting_sequence_ids: int = 0
    maximum_provider_gap_events: int = 0
    maximum_capture_latency_ms: float = 1000.0
    require_provider_continuity_proof: bool = True

    def __post_init__(self) -> None:
        for value in (
            self.maximum_negative_capture_latency_events,
            self.maximum_sequence_regressions,
            self.maximum_conflicting_sequence_ids,
            self.maximum_provider_gap_events,
        ):
            if value < 0:
                raise ValueError("integrity thresholds cannot be negative")
        if self.maximum_capture_latency_ms < 0:
            raise ValueError("maximum_capture_latency_ms cannot be negative")


@dataclass(frozen=True, slots=True)
class PublisherSequenceReport:
    publisher_id: int
    events: int
    sequence_regressions: int
    conflicting_sequence_ids: int


@dataclass(frozen=True, slots=True)
class FeedIntegrityReport:
    events: int
    publishers: tuple[PublisherSequenceReport, ...]
    continuity_mode: FeedContinuityMode
    provider_gap_events: int
    negative_capture_latency_events: int
    maximum_capture_latency_ms: float
    warmup_complete: bool | None
    status: FeedIntegrityStatus
    failures: tuple[str, ...]

    @property
    def certified(self) -> bool:
        return self.status is FeedIntegrityStatus.PASS


def _publisher_sequence_reports(
    events: tuple[OptionMarketEvent, ...],
) -> tuple[PublisherSequenceReport, ...]:
    grouped: dict[int, list[OptionMarketEvent]] = {}
    for event in events:
        grouped.setdefault(event.publisher_id, []).append(event)
    reports: list[PublisherSequenceReport] = []
    for publisher_id in sorted(grouped):
        ordered = sorted(
            grouped[publisher_id],
            key=lambda event: (event.ts_recv_ns, event.ts_event_ns, event.sequence),
        )
        regressions = 0
        seen: dict[int, str] = {}
        conflicts = 0
        previous: int | None = None
        for event in ordered:
            if previous is not None and event.sequence < previous:
                regressions += 1
            previous = max(previous or event.sequence, event.sequence)
            prior_id = seen.get(event.sequence)
            if prior_id is not None and prior_id != event.stable_id:
                conflicts += 1
            else:
                seen[event.sequence] = event.stable_id
        reports.append(
            PublisherSequenceReport(
                publisher_id=publisher_id,
                events=len(ordered),
                sequence_regressions=regressions,
                conflicting_sequence_ids=conflicts,
            )
        )
    return tuple(reports)


def evaluate_feed_integrity(
    events: tuple[OptionMarketEvent, ...],
    *,
    continuity_mode: FeedContinuityMode = FeedContinuityMode.UNKNOWN,
    provider_gap_events: int = 0,
    warmup_complete: bool | None = None,
    policy: FeedIntegrityPolicy | None = None,
) -> FeedIntegrityReport:
    """Certify what can actually be proven about a filtered options event stream.

    Sequence *gaps* are not inferred from filtered symbol data because publisher sequence numbers
    may advance for messages that were intentionally excluded by a symbol/window query. True gap
    counts must come from provider transport/gap evidence. We only infer sequence regressions and
    conflicting reuse of the same publisher sequence identifier from the records themselves.
    """

    policy = policy or FeedIntegrityPolicy()
    if provider_gap_events < 0:
        raise ValueError("provider_gap_events cannot be negative")
    reports = _publisher_sequence_reports(events)
    negative = sum(event.capture_latency_ns < 0 for event in events)
    maximum_latency_ms = max(
        (event.capture_latency_ns / 1_000_000.0 for event in events if event.capture_latency_ns >= 0),
        default=0.0,
    )
    regressions = sum(report.sequence_regressions for report in reports)
    conflicts = sum(report.conflicting_sequence_ids for report in reports)
    failures: list[str] = []
    if not events:
        failures.append("event_stream_empty")
    if (
        policy.require_provider_continuity_proof
        and continuity_mode is not FeedContinuityMode.PROVIDER_CERTIFIED
    ):
        failures.append("provider_continuity_not_certified")
    if provider_gap_events > policy.maximum_provider_gap_events:
        failures.append(
            f"provider_gap_events:{provider_gap_events}>{policy.maximum_provider_gap_events}"
        )
    if negative > policy.maximum_negative_capture_latency_events:
        failures.append(
            "negative_capture_latency_events:"
            f"{negative}>{policy.maximum_negative_capture_latency_events}"
        )
    if regressions > policy.maximum_sequence_regressions:
        failures.append(
            f"sequence_regressions:{regressions}>{policy.maximum_sequence_regressions}"
        )
    if conflicts > policy.maximum_conflicting_sequence_ids:
        failures.append(
            "conflicting_sequence_ids:"
            f"{conflicts}>{policy.maximum_conflicting_sequence_ids}"
        )
    if maximum_latency_ms > policy.maximum_capture_latency_ms:
        failures.append(
            f"capture_latency_ms:{maximum_latency_ms:.3f}>{policy.maximum_capture_latency_ms:.3f}"
        )
    if warmup_complete is False:
        failures.append("acquisition_warmup_incomplete")

    if not events or (
        policy.require_provider_continuity_proof
        and continuity_mode is FeedContinuityMode.UNKNOWN
    ):
        status = FeedIntegrityStatus.INSUFFICIENT
    elif failures:
        status = FeedIntegrityStatus.FAIL
    else:
        status = FeedIntegrityStatus.PASS
    return FeedIntegrityReport(
        events=len(events),
        publishers=reports,
        continuity_mode=continuity_mode,
        provider_gap_events=provider_gap_events,
        negative_capture_latency_events=negative,
        maximum_capture_latency_ms=maximum_latency_ms,
        warmup_complete=warmup_complete,
        status=status,
        failures=tuple(failures),
    )
