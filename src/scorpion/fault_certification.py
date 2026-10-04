from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .causal_features import verify_feature_store
from .config import Policy
from .domain import BookState, Effect, SignalEvent
from .invariants import assert_valid_book
from .policy_bundle import RuntimePolicyBundle
from .processing_order import inspect_processing_order, load_signals_in_processing_order
from .reducer import reduce_book
from .replay import ReplayOrder, replay, state_fingerprint
from .state_checkpoint import restore_state


@dataclass(frozen=True, slots=True)
class FaultProbe:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class FaultCertificationReport:
    probes: tuple[FaultProbe, ...]
    signal_count: int
    baseline_fingerprint: str
    source_time_fingerprint: str
    source_time_differs: bool
    process_order_fingerprint: str
    passed: bool


def _apply_tail(
    state: BookState,
    tail: tuple[SignalEvent, ...],
    policy: Policy,
) -> tuple[BookState, tuple[Effect, ...]]:
    effects: list[Effect] = []
    for event in tail:
        state, produced = reduce_book(state, event, policy)
        effects.extend(produced)
    return state, tuple(effects)


def _split_points(total: int) -> tuple[int, ...]:
    if total <= 1:
        return (0, total)
    candidates = {0, total, total // 4, total // 2, (3 * total) // 4}
    return tuple(sorted(candidates))


def certify_replay_faults(
    path: str | Path,
    *,
    runtime_policy: RuntimePolicyBundle | None = None,
) -> FaultCertificationReport:
    """Run deterministic fault probes against the real durable event history.

    These probes never alter the database and never interact with a broker. They test whether
    retry/recovery mechanics preserve the canonical state under the durable process-order contract.
    """
    policy = runtime_policy or RuntimePolicyBundle()
    signals = tuple(load_signals_in_processing_order(path))
    baseline_state, _ = replay(signals, policy.base, order=ReplayOrder.INPUT)
    assert_valid_book(baseline_state, max_open_positions=policy.base.max_open_positions)
    baseline_fingerprint = state_fingerprint(baseline_state)
    processing_order = inspect_processing_order(path)
    feature_store = verify_feature_store(path)
    probes: list[FaultProbe] = [
        FaultProbe(
            "durable_processing_order_complete",
            processing_order.complete,
            (
                f"signals={processing_order.signal_count};"
                f"ordered={processing_order.process_ordered_count};"
                f"fingerprint={processing_order.process_fingerprint}"
            ),
        )
    ]

    duplicated = tuple(event for item in signals for event in (item, item))
    duplicate_state, _ = replay(duplicated, policy.base, order=ReplayOrder.INPUT)
    probes.append(
        FaultProbe(
            "duplicate_delivery_idempotency",
            state_fingerprint(duplicate_state) == baseline_fingerprint,
            "every normalized event duplicated adjacent to its original",
        )
    )

    for cut in _split_points(len(signals)):
        prefix_state, _ = replay(signals[:cut], policy.base, order=ReplayOrder.INPUT)
        reconstructed, _ = _apply_tail(prefix_state, signals[cut:], policy.base)
        probes.append(
            FaultProbe(
                f"prefix_tail_recovery_at_{cut}",
                state_fingerprint(reconstructed) == baseline_fingerprint,
                f"prefix={cut};tail={len(signals) - cut}",
            )
        )

    restored = restore_state(path, list(signals), runtime_policy=policy)
    probes.append(
        FaultProbe(
            "checkpoint_or_full_recovery_equivalence",
            state_fingerprint(restored.state) == baseline_fingerprint,
            (
                f"checkpoint={restored.checkpoint_id};tail={restored.tail_events};"
                f"mode={restored.reason}"
            ),
        )
    )
    probes.append(
        FaultProbe(
            "causal_feature_store_integrity",
            feature_store.valid,
            (
                f"snapshots={feature_store.snapshots};"
                f"hash_mismatches={feature_store.hash_mismatches};"
                f"invalid_seq={feature_store.invalid_process_sequences}"
            ),
        )
    )

    source_time_state, _ = replay(signals, policy.base, order=ReplayOrder.SOURCE_TIME)
    source_time_fingerprint = state_fingerprint(source_time_state)
    source_time_differs = source_time_fingerprint != baseline_fingerprint
    probes.append(
        FaultProbe(
            "source_time_order_is_diagnostic_only",
            True,
            (
                "source-time replay diverges from durable live processing order"
                if source_time_differs
                else "source-time and durable processing order currently converge"
            ),
        )
    )
    return FaultCertificationReport(
        probes=tuple(probes),
        signal_count=len(signals),
        baseline_fingerprint=baseline_fingerprint,
        source_time_fingerprint=source_time_fingerprint,
        source_time_differs=source_time_differs,
        process_order_fingerprint=processing_order.process_fingerprint,
        passed=all(probe.passed for probe in probes),
    )
