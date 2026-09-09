from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from .association import associate_followup_with_evidence
from .domain import BookState, EventKind, RawDiscordMessage
from .history_archive import ArchivedDiscordMessage
from .microstructure import OptionMicrostructureTape, ns_from_datetime
from .parser import parse_message_with_evidence
from .policy_bundle import RuntimePolicyBundle
from .reducer import reduce_book

_ACTIONABLE = frozenset({EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT})


@dataclass(frozen=True, slots=True)
class AcquisitionWindow:
    contract_key: str
    starts_ts_utc: datetime
    ends_ts_utc: datetime
    warmup_starts_ts_utc: datetime
    event_ids: tuple[str, ...]
    message_ids: tuple[str, ...]

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.ends_ts_utc - self.starts_ts_utc).total_seconds())

    @property
    def query_duration_seconds(self) -> float:
        return max(0.0, (self.ends_ts_utc - self.warmup_starts_ts_utc).total_seconds())

    @property
    def warmup_seconds(self) -> float:
        return max(0.0, (self.starts_ts_utc - self.warmup_starts_ts_utc).total_seconds())


@dataclass(frozen=True, slots=True)
class AcquisitionPlan:
    windows: tuple[AcquisitionWindow, ...]
    actionable_events: int
    associated_events: int
    unassociated_events: int
    total_window_seconds: float
    total_query_seconds: float
    plan_sha256: str


@dataclass(frozen=True, slots=True)
class AcquisitionCoverage:
    windows: int
    warmup_state_available: int
    missing_warmup_contracts: tuple[str, ...]
    coverage_rate: float
    complete: bool


def _to_raw(message: ArchivedDiscordMessage) -> RawDiscordMessage:
    return RawDiscordMessage(
        message_id=message.message_id,
        guild_id=message.guild_id,
        channel_id=message.channel_id,
        author_id=message.author_id,
        content=message.content,
        source_ts_utc=message.source_ts_utc,
        received_ts_utc=message.source_ts_utc,
        edited_ts_utc=message.edited_ts_utc,
        referenced_message_id=message.referenced_message_id,
    )


def _canonical_payload(windows: tuple[AcquisitionWindow, ...]) -> str:
    payload = [
        {
            "contract_key": window.contract_key,
            "starts_ts_utc": window.starts_ts_utc.astimezone(UTC).isoformat(),
            "ends_ts_utc": window.ends_ts_utc.astimezone(UTC).isoformat(),
            "warmup_starts_ts_utc": window.warmup_starts_ts_utc.astimezone(UTC).isoformat(),
            "event_ids": list(window.event_ids),
            "message_ids": list(window.message_ids),
        }
        for window in windows
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def build_acquisition_plan(
    messages: tuple[ArchivedDiscordMessage, ...],
    *,
    allowed_author_ids: frozenset[str],
    pre_context: timedelta = timedelta(seconds=2),
    post_context: timedelta = timedelta(seconds=10),
    quote_warmup: timedelta = timedelta(seconds=60),
    merge_gap: timedelta = timedelta(seconds=1),
    runtime_policy: RuntimePolicyBundle | None = None,
) -> AcquisitionPlan:
    """Derive contract-scoped microstructure windows before purchasing/querying market data.

    The analysis window remains tight around the Discord action. ``quote_warmup`` extends only
    the provider-query boundary so a quote that became active before the analysis window can be
    reconstructed. This prevents narrow-window data acquisition from preferentially keeping only
    highly active contracts that happened to update immediately before an alert.
    """
    for name, value in (
        ("pre_context", pre_context),
        ("post_context", post_context),
        ("quote_warmup", quote_warmup),
        ("merge_gap", merge_gap),
    ):
        if value < timedelta(0):
            raise ValueError(f"{name} cannot be negative")
    policy = runtime_policy or RuntimePolicyBundle()
    ordered = sorted(messages, key=lambda item: (item.source_ts_utc, item.message_id))
    state = BookState()
    message_contract: dict[str, str] = {}
    raw_windows: list[AcquisitionWindow] = []
    actionable = 0
    associated = 0
    unassociated = 0

    for archived in ordered:
        raw = _to_raw(archived)
        parsed = parse_message_with_evidence(raw, allowed_author_ids)
        referenced = (
            message_contract.get(raw.referenced_message_id)
            if raw.referenced_message_id is not None
            else None
        )
        result = associate_followup_with_evidence(parsed.event, state, referenced)
        event = result.event
        if event.contract_key is not None:
            message_contract[event.message_id] = event.contract_key
        if event.kind not in _ACTIONABLE:
            continue
        actionable += 1
        key = event.contract_key
        if key is None:
            unassociated += 1
            continue
        associated += 1
        source = event.source_ts_utc.astimezone(UTC)
        analysis_start = source - pre_context
        raw_windows.append(
            AcquisitionWindow(
                contract_key=key,
                starts_ts_utc=analysis_start,
                ends_ts_utc=source + post_context,
                warmup_starts_ts_utc=analysis_start - quote_warmup,
                event_ids=(event.event_id,),
                message_ids=(event.message_id,),
            )
        )
        state, _ = reduce_book(state, event, policy.base)

    merged: list[AcquisitionWindow] = []
    for window in sorted(
        raw_windows,
        key=lambda item: (item.contract_key, item.starts_ts_utc, item.ends_ts_utc),
    ):
        if not merged:
            merged.append(window)
            continue
        previous = merged[-1]
        if (
            previous.contract_key == window.contract_key
            and window.starts_ts_utc <= previous.ends_ts_utc + merge_gap
        ):
            merged[-1] = AcquisitionWindow(
                contract_key=previous.contract_key,
                starts_ts_utc=min(previous.starts_ts_utc, window.starts_ts_utc),
                ends_ts_utc=max(previous.ends_ts_utc, window.ends_ts_utc),
                warmup_starts_ts_utc=min(
                    previous.warmup_starts_ts_utc,
                    window.warmup_starts_ts_utc,
                ),
                event_ids=previous.event_ids + window.event_ids,
                message_ids=previous.message_ids + window.message_ids,
            )
        else:
            merged.append(window)

    windows = tuple(merged)
    material = _canonical_payload(windows)
    return AcquisitionPlan(
        windows=windows,
        actionable_events=actionable,
        associated_events=associated,
        unassociated_events=unassociated,
        total_window_seconds=sum(item.duration_seconds for item in windows),
        total_query_seconds=sum(item.query_duration_seconds for item in windows),
        plan_sha256=hashlib.sha256(material.encode("utf-8")).hexdigest(),
    )


def verify_acquisition_warmup(
    plan: AcquisitionPlan,
    tape: OptionMicrostructureTape,
) -> AcquisitionCoverage:
    missing: list[str] = []
    available = 0
    for window in plan.windows:
        analysis_start_ns = ns_from_datetime(window.starts_ts_utc)
        warmup_span = window.starts_ts_utc - window.warmup_starts_ts_utc
        quote = tape.market_quote_at(
            window.contract_key,
            analysis_start_ns,
            max_age=warmup_span,
        )
        if quote is None:
            missing.append(window.contract_key)
        else:
            available += 1
    total = len(plan.windows)
    return AcquisitionCoverage(
        windows=total,
        warmup_state_available=available,
        missing_warmup_contracts=tuple(sorted(set(missing))),
        coverage_rate=available / total if total else 0.0,
        complete=available == total and total > 0,
    )


def acquisition_plan_payload(plan: AcquisitionPlan) -> dict[str, object]:
    return {
        "actionable_events": plan.actionable_events,
        "associated_events": plan.associated_events,
        "unassociated_events": plan.unassociated_events,
        "total_window_seconds": plan.total_window_seconds,
        "total_query_seconds": plan.total_query_seconds,
        "plan_sha256": plan.plan_sha256,
        "windows": [
            {
                **asdict(window),
                "starts_ts_utc": window.starts_ts_utc.isoformat(),
                "ends_ts_utc": window.ends_ts_utc.isoformat(),
                "warmup_starts_ts_utc": window.warmup_starts_ts_utc.isoformat(),
                "warmup_seconds": window.warmup_seconds,
                "query_duration_seconds": window.query_duration_seconds,
            }
            for window in plan.windows
        ],
    }
