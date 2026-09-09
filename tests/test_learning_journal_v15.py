import sqlite3

import pytest

from scorpion.adaptive_ensemble import AdaptiveEnsemblePolicy, fingerprint_adaptive_snapshot
from scorpion.domain import EventKind
from scorpion.learning_journal import (
    apply_and_record_adaptive_update,
    verify_learning_journal,
)


def _distribution(truth: EventKind, *, strong: bool) -> dict[EventKind, float]:
    if truth is EventKind.ENTRY:
        return (
            {EventKind.ENTRY: 0.95, EventKind.IGNORE: 0.05}
            if strong
            else {EventKind.ENTRY: 0.20, EventKind.IGNORE: 0.80}
        )
    return (
        {EventKind.ENTRY: 0.02, EventKind.IGNORE: 0.98}
        if strong
        else {EventKind.ENTRY: 0.75, EventKind.IGNORE: 0.25}
    )


def test_learning_journal_replays_exact_adaptive_state_and_freeze(tmp_path):
    path = tmp_path / "learning.db"
    policy = AdaptiveEnsemblePolicy(
        learning_rate=0.4,
        forgetting_factor=0.99,
        minimum_updates=5,
        maximum_single_model_weight=1.0,
        minimum_effective_models=1.0,
    )
    snapshot = None
    for index in range(8):
        truth = EventKind.ENTRY if index % 4 == 0 else EventKind.IGNORE
        snapshot = apply_and_record_adaptive_update(
            path,
            ensemble_id="shadow-v1",
            event_id=f"event-{index}",
            previous=snapshot,
            model_probabilities={
                "strong": _distribution(truth, strong=True),
                "weak": _distribution(truth, strong=False),
            },
            truth=truth,
            policy=policy,
        )
    assert snapshot is not None
    frozen = apply_and_record_adaptive_update(
        path,
        ensemble_id="shadow-v1",
        event_id="drift-event",
        previous=snapshot,
        model_probabilities={
            "strong": _distribution(EventKind.ENTRY, strong=False),
            "weak": _distribution(EventKind.ENTRY, strong=True),
        },
        truth=EventKind.ENTRY,
        drift_active=True,
        policy=policy,
    )
    assert frozen.weights == snapshot.weights

    verification = verify_learning_journal(path, "shadow-v1")
    assert verification.valid is True
    assert verification.records == 9
    assert verification.final_snapshot_hash == fingerprint_adaptive_snapshot(frozen)


def test_learning_journal_rejects_duplicate_event_and_detects_tampering(tmp_path):
    path = tmp_path / "tamper.db"
    policy = AdaptiveEnsemblePolicy(
        minimum_updates=1,
        maximum_single_model_weight=1.0,
        minimum_effective_models=1.0,
    )
    snapshot = apply_and_record_adaptive_update(
        path,
        ensemble_id="shadow-v1",
        event_id="event-1",
        previous=None,
        model_probabilities={
            "a": {EventKind.ENTRY: 0.9, EventKind.IGNORE: 0.1},
            "b": {EventKind.ENTRY: 0.6, EventKind.IGNORE: 0.4},
        },
        truth=EventKind.ENTRY,
        policy=policy,
    )
    with pytest.raises(ValueError, match="already journaled"):
        apply_and_record_adaptive_update(
            path,
            ensemble_id="shadow-v1",
            event_id="event-1",
            previous=snapshot,
            model_probabilities={
                "a": {EventKind.ENTRY: 0.9, EventKind.IGNORE: 0.1},
                "b": {EventKind.ENTRY: 0.6, EventKind.IGNORE: 0.4},
            },
            truth=EventKind.ENTRY,
            policy=policy,
        )

    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE adaptive_learning_journal SET truth='IGNORE' WHERE event_id='event-1'"
        )
        db.commit()
    verification = verify_learning_journal(path, "shadow-v1")
    assert verification.valid is False
    assert any("record_hash_mismatch" in failure for failure in verification.failures)
