from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .accepted_label_truth import AcceptedTruthSnapshot, require_truth_binding
from .contextual_ensemble import (
    ContextualEnsembleEvaluation,
    ContextualEnsembleFit,
    ContextualEnsembleObservation,
    ContextualEnsemblePolicy,
    evaluate_contextual_ensemble,
    fit_contextual_ensemble,
)
from .hard_example_curriculum import (
    CurriculumExample,
    HardExampleCurriculum,
    HardExampleCurriculumPolicy,
    build_hard_example_curriculum,
)
from .trainable_model import FeatureSchema, TrainingExample, dataset_fingerprint


@dataclass(frozen=True, slots=True)
class TruthBoundArtifactIdentity:
    stage: str
    artifact_hash: str
    truth_snapshot_hash: str
    truth_binding_hash: str
    parent_lineage_hash: str
    event_count: int
    lineage_hash: str


@dataclass(frozen=True, slots=True)
class TruthBoundCurriculum:
    curriculum: HardExampleCurriculum
    identity: TruthBoundArtifactIdentity


@dataclass(frozen=True, slots=True)
class TruthBoundDataset:
    dataset_fingerprint: str
    identity: TruthBoundArtifactIdentity


@dataclass(frozen=True, slots=True)
class TruthBoundContextualFit:
    fit: ContextualEnsembleFit
    identity: TruthBoundArtifactIdentity


@dataclass(frozen=True, slots=True)
class TruthBoundContextualEvaluation:
    evaluation: ContextualEnsembleEvaluation
    identity: TruthBoundArtifactIdentity


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def bind_artifact_to_truth(
    *,
    stage: str,
    artifact_hash: str,
    event_ids: tuple[str, ...],
    snapshot: AcceptedTruthSnapshot,
    parent_lineage_hash: str = "",
) -> TruthBoundArtifactIdentity:
    if not stage.strip() or not artifact_hash.strip():
        raise ValueError("stage and artifact_hash are required")
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("truth-bound artifact event ids must be unique")
    by_event = snapshot.by_event
    missing = sorted(set(event_ids) - set(by_event))
    if missing:
        raise ValueError(f"truth snapshot missing artifact events: {missing}")
    bindings = [
        {
            "event_id": event_id,
            "revision_id": by_event[event_id].revision_id,
            "revision_number": by_event[event_id].revision_number,
            "source_revision_id": by_event[event_id].source_revision_id,
            "label": by_event[event_id].label.value,
            "confidence": round(by_event[event_id].confidence, 12),
        }
        for event_id in sorted(event_ids)
    ]
    truth_binding_hash = _hash(
        {
            "version": "truth-binding-set-v1",
            "truth_snapshot_hash": snapshot.snapshot_hash,
            "bindings": bindings,
        }
    )
    lineage_hash = _hash(
        {
            "version": "truth-bound-artifact-v1",
            "stage": stage,
            "artifact_hash": artifact_hash,
            "truth_snapshot_hash": snapshot.snapshot_hash,
            "truth_binding_hash": truth_binding_hash,
            "parent_lineage_hash": parent_lineage_hash,
            "event_count": len(event_ids),
        }
    )
    return TruthBoundArtifactIdentity(
        stage=stage,
        artifact_hash=artifact_hash,
        truth_snapshot_hash=snapshot.snapshot_hash,
        truth_binding_hash=truth_binding_hash,
        parent_lineage_hash=parent_lineage_hash,
        event_count=len(event_ids),
        lineage_hash=lineage_hash,
    )


def build_truth_bound_curriculum(
    examples: tuple[CurriculumExample, ...],
    *,
    snapshot: AcceptedTruthSnapshot,
    source_dataset_fingerprint: str,
    holdout_event_ids: frozenset[str] = frozenset(),
    policy: HardExampleCurriculumPolicy | None = None,
    parent_lineage_hash: str = "",
) -> TruthBoundCurriculum:
    """Build a curriculum only from exact accepted revisions in one truth snapshot."""
    for example in examples:
        binding = require_truth_binding(
            snapshot,
            event_id=example.event_id,
            label=example.label,
            minimum_confidence=example.label_confidence,
        )
        if example.label_source_id != binding.revision_id:
            raise ValueError(
                f"curriculum label source is not accepted truth revision for {example.event_id}"
            )
    curriculum = build_hard_example_curriculum(
        examples,
        source_dataset_fingerprint=source_dataset_fingerprint,
        holdout_event_ids=holdout_event_ids,
        policy=policy,
    )
    event_ids = tuple(item.event_id for item in curriculum.selected)
    identity = bind_artifact_to_truth(
        stage="hard-example-curriculum",
        artifact_hash=curriculum.curriculum_fingerprint,
        event_ids=event_ids,
        snapshot=snapshot,
        parent_lineage_hash=parent_lineage_hash,
    )
    return TruthBoundCurriculum(curriculum=curriculum, identity=identity)


def fingerprint_truth_bound_training_dataset(
    examples: tuple[TrainingExample, ...],
    *,
    schema: FeatureSchema,
    snapshot: AcceptedTruthSnapshot,
    parent_lineage_hash: str,
) -> TruthBoundDataset:
    """Bind a trainable dataset to accepted event truth; sample_id must be event_id."""
    for example in examples:
        binding = snapshot.by_event.get(example.sample_id)
        if binding is None:
            raise ValueError(f"training sample {example.sample_id} is absent from truth snapshot")
        target = str(example.target)
        if target != binding.label.value:
            raise ValueError(f"training target disagrees with accepted truth for {example.sample_id}")
    fingerprint = dataset_fingerprint(examples, schema)
    identity = bind_artifact_to_truth(
        stage="training-dataset",
        artifact_hash=fingerprint,
        event_ids=tuple(item.sample_id for item in examples),
        snapshot=snapshot,
        parent_lineage_hash=parent_lineage_hash,
    )
    return TruthBoundDataset(dataset_fingerprint=fingerprint, identity=identity)


def fit_truth_bound_contextual_ensemble(
    rows: tuple[ContextualEnsembleObservation, ...],
    *,
    snapshot: AcceptedTruthSnapshot,
    policy: ContextualEnsemblePolicy,
    parent_lineage_hash: str,
) -> TruthBoundContextualFit:
    for row in rows:
        require_truth_binding(snapshot, event_id=row.event_id, label=row.truth)
    fit = fit_contextual_ensemble(rows, policy=policy)
    identity = bind_artifact_to_truth(
        stage="contextual-oof-fit",
        artifact_hash=fit.fit_hash,
        event_ids=tuple(row.event_id for row in rows),
        snapshot=snapshot,
        parent_lineage_hash=parent_lineage_hash,
    )
    return TruthBoundContextualFit(fit=fit, identity=identity)


def evaluate_truth_bound_contextual_ensemble(
    bound_fit: TruthBoundContextualFit,
    rows: tuple[ContextualEnsembleObservation, ...],
    *,
    snapshot: AcceptedTruthSnapshot,
    policy: ContextualEnsemblePolicy,
) -> TruthBoundContextualEvaluation:
    for row in rows:
        require_truth_binding(snapshot, event_id=row.event_id, label=row.truth)
    evaluation = evaluate_contextual_ensemble(bound_fit.fit, rows, policy=policy)
    identity = bind_artifact_to_truth(
        stage="contextual-holdout-evaluation",
        artifact_hash=evaluation.report_hash,
        event_ids=tuple(row.event_id for row in rows),
        snapshot=snapshot,
        parent_lineage_hash=bound_fit.identity.lineage_hash,
    )
    return TruthBoundContextualEvaluation(evaluation=evaluation, identity=identity)
