from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from .accuracy import percentile
from .association import associate_followup_with_evidence
from .config import DEFAULT_POLICY, Policy
from .counterfactual import EvidenceParser
from .domain import BookState, EventKind, RawDiscordMessage
from .invariants import assert_valid_book
from .parser import parse_message_with_evidence
from .reducer import reduce_book
from .replay import state_fingerprint


@dataclass(frozen=True, slots=True)
class StageSample:
    message_id: str
    kind: EventKind
    parse_us: int
    association_us: int
    reduce_us: int
    total_us: int


@dataclass(frozen=True, slots=True)
class StagePercentiles:
    p50_us: float
    p95_us: float
    p99_us: float


@dataclass(frozen=True, slots=True)
class PipelineProfile:
    count: int
    parse: StagePercentiles
    association: StagePercentiles
    reduce: StagePercentiles
    total: StagePercentiles
    final_state_fingerprint: str
    samples: tuple[StageSample, ...]


def _summary(values: Sequence[int]) -> StagePercentiles:
    return StagePercentiles(
        p50_us=percentile(values, 0.50),
        p95_us=percentile(values, 0.95),
        p99_us=percentile(values, 0.99),
    )


def profile_messages(
    messages: Sequence[RawDiscordMessage],
    *,
    parser: EvidenceParser = parse_message_with_evidence,
    allowed_author_ids: frozenset[str] | None = None,
    policy: Policy = DEFAULT_POLICY,
) -> PipelineProfile:
    ordered = sorted(
        messages,
        key=lambda raw: (raw.source_ts_utc, raw.received_ts_utc, raw.revision_id),
    )
    state = BookState()
    message_contracts: dict[str, str] = {}
    samples: list[StageSample] = []

    for raw in ordered:
        total_started = time.perf_counter_ns()
        parse_started = time.perf_counter_ns()
        parsed = parser(raw, allowed_author_ids)
        parse_us = max(0, (time.perf_counter_ns() - parse_started) // 1_000)

        association_started = time.perf_counter_ns()
        referenced_key = (
            message_contracts.get(raw.referenced_message_id)
            if raw.referenced_message_id is not None
            else None
        )
        associated = associate_followup_with_evidence(parsed.event, state, referenced_key)
        association_us = max(0, (time.perf_counter_ns() - association_started) // 1_000)

        reduce_started = time.perf_counter_ns()
        state, _ = reduce_book(state, associated.event, policy)
        assert_valid_book(state, max_open_positions=policy.max_open_positions)
        reduce_us = max(0, (time.perf_counter_ns() - reduce_started) // 1_000)

        if associated.event.contract_key is not None:
            message_contracts[associated.event.message_id] = associated.event.contract_key
        samples.append(
            StageSample(
                message_id=raw.message_id,
                kind=associated.event.kind,
                parse_us=parse_us,
                association_us=association_us,
                reduce_us=reduce_us,
                total_us=max(0, (time.perf_counter_ns() - total_started) // 1_000),
            )
        )

    return PipelineProfile(
        count=len(samples),
        parse=_summary([sample.parse_us for sample in samples]),
        association=_summary([sample.association_us for sample in samples]),
        reduce=_summary([sample.reduce_us for sample in samples]),
        total=_summary([sample.total_us for sample in samples]),
        final_state_fingerprint=state_fingerprint(state),
        samples=tuple(samples),
    )
