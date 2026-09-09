from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from .association import associate_followup_with_evidence
from .domain import BookState, EventKind, RawDiscordMessage
from .history_archive import ArchivedDiscordMessage
from .parser import parse_message_with_evidence
from .policy_bundle import RuntimePolicyBundle
from .reducer import reduce_book

_ACTIONABLE = frozenset({EventKind.ENTRY, EventKind.ADD, EventKind.TRIM, EventKind.EXIT})


@dataclass(frozen=True, slots=True)
class AcquisitionWindow:
    contract_key: str
    starts_ts_utc: datetime
    ends_ts_utc: datetime
    event_ids: tuple[str, ...]
    message_ids: tuple[str, ...]

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.ends_ts_utc - self.starts_ts_utc).total_seconds())


@dataclass(frozen=True, slots=True)
class AcquisitionPlan:
    windows: tuple[AcquisitionWindow, ...]
    actionable_events: int
    associated_events: int
    unassociated_events: int
    total_window_seconds: float
    plan_sha256: str


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
    merge_gap: timedelta = timedelta(seconds=1),
    runtime_policy: RuntimePolicyBundle | None = None,
) -> AcquisitionPlan:
    """Derive contract-scoped microstructure windows before purchasing/querying market data.

    Windows are driven by the same deterministic parser/association/reducer lineage as runtime.
    This lets a data provider request be restricted to the seconds surrounding source actions,
    while the resulting plan remains reproducible and cryptographically fingerprinted.
    """
    for name, value in (
        ("pre_context", pre_context),
        ("post_context", post_context),
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
        raw_windows.append(
            AcquisitionWindow(
                contract_key=key,
                starts_ts_utc=source - pre_context,
                ends_ts_utc=source + post_context,
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
        plan_sha256=hashlib.sha256(material.encode("utf-8")).hexdigest(),
    )


def acquisition_plan_payload(plan: AcquisitionPlan) -> dict[str, object]:
    return {
        "actionable_events": plan.actionable_events,
        "associated_events": plan.associated_events,
        "unassociated_events": plan.unassociated_events,
        "total_window_seconds": plan.total_window_seconds,
        "plan_sha256": plan.plan_sha256,
        "windows": [
            {
                **asdict(window),
                "starts_ts_utc": window.starts_ts_utc.isoformat(),
                "ends_ts_utc": window.ends_ts_utc.isoformat(),
            }
            for window in plan.windows
        ],
    }
