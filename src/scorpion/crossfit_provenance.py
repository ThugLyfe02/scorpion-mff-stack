from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime


def _json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(payload: object) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CrossFitFoldCertificate:
    fold_id: str
    heldout_assignment_ids: tuple[str, ...]
    training_assignment_ids_hash: str
    training_truth_snapshot_hash: str

    def __post_init__(self) -> None:
        if not self.fold_id.strip():
            raise ValueError("fold_id is required")
        if not self.heldout_assignment_ids:
            raise ValueError("heldout_assignment_ids cannot be empty")
        normalized = tuple(sorted(set(self.heldout_assignment_ids)))
        if len(normalized) != len(self.heldout_assignment_ids):
            raise ValueError("heldout assignment ids must be unique")
        object.__setattr__(self, "heldout_assignment_ids", normalized)
        if not self.training_assignment_ids_hash.strip():
            raise ValueError("training_assignment_ids_hash is required")
        if not self.training_truth_snapshot_hash.strip():
            raise ValueError("training_truth_snapshot_hash is required")


@dataclass(frozen=True, slots=True)
class CrossFitProvenanceManifest:
    model_fingerprint: str
    dataset_fingerprint: str
    truth_snapshot_hash: str
    split_plan_hash: str
    assignment_universe_hash: str
    folds: tuple[CrossFitFoldCertificate, ...]
    created_ts_utc: datetime
    manifest_hash: str


@dataclass(frozen=True, slots=True)
class CertifiedCrossFittedOutcomePrediction:
    assignment_id: str
    model_fingerprint: str
    manifest_hash: str
    fold_id: str
    predicted_values: dict[str, float]

    def __post_init__(self) -> None:
        for name in ("assignment_id", "model_fingerprint", "manifest_hash", "fold_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if not self.predicted_values:
            raise ValueError("predicted_values cannot be empty")
        for treatment, value in self.predicted_values.items():
            if not treatment.strip():
                raise ValueError("prediction treatment key cannot be blank")
            if not isinstance(value, (int, float)):
                raise ValueError("prediction values must be numeric")


@dataclass(frozen=True, slots=True)
class CrossFittedCensoringPrediction:
    assignment_id: str
    model_fingerprint: str
    manifest_hash: str
    fold_id: str
    resolution_probability: float

    def __post_init__(self) -> None:
        for name in ("assignment_id", "model_fingerprint", "manifest_hash", "fold_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if not 0 < self.resolution_probability <= 1:
            raise ValueError("resolution_probability must be in (0,1]")


def _assignment_ids_hash(ids: tuple[str, ...]) -> str:
    return _hash({"version": "assignment-id-set-v1", "assignment_ids": list(ids)})


def build_crossfit_provenance_manifest(
    *,
    model_fingerprint: str,
    dataset_fingerprint: str,
    truth_snapshot_hash: str,
    assignment_to_fold: dict[str, str],
    training_truth_snapshot_hash_by_fold: dict[str, str],
    created_ts_utc: datetime | None = None,
) -> CrossFitProvenanceManifest:
    for name, value in (
        ("model_fingerprint", model_fingerprint),
        ("dataset_fingerprint", dataset_fingerprint),
        ("truth_snapshot_hash", truth_snapshot_hash),
    ):
        if not value.strip():
            raise ValueError(f"{name} is required")
    if not assignment_to_fold:
        raise ValueError("assignment_to_fold cannot be empty")
    if any(not assignment.strip() or not fold.strip() for assignment, fold in assignment_to_fold.items()):
        raise ValueError("assignment and fold ids must be non-empty")
    fold_ids = tuple(sorted(set(assignment_to_fold.values())))
    if len(fold_ids) < 2:
        raise ValueError("cross-fitting requires at least two folds")
    if set(training_truth_snapshot_hash_by_fold) != set(fold_ids):
        raise ValueError("training truth snapshot mapping must cover every fold")
    universe = tuple(sorted(assignment_to_fold))
    assignment_universe_hash = _assignment_ids_hash(universe)
    split_plan_hash = _hash(
        {
            "version": "crossfit-split-plan-v1",
            "assignment_to_fold": [[assignment, assignment_to_fold[assignment]] for assignment in universe],
        }
    )
    folds: list[CrossFitFoldCertificate] = []
    for fold_id in fold_ids:
        heldout = tuple(sorted(
            assignment for assignment, assigned_fold in assignment_to_fold.items()
            if assigned_fold == fold_id
        ))
        training = tuple(sorted(set(universe) - set(heldout)))
        if not heldout or not training:
            raise ValueError("every cross-fit fold must have training and held-out assignments")
        folds.append(
            CrossFitFoldCertificate(
                fold_id=fold_id,
                heldout_assignment_ids=heldout,
                training_assignment_ids_hash=_assignment_ids_hash(training),
                training_truth_snapshot_hash=training_truth_snapshot_hash_by_fold[fold_id],
            )
        )
    created = (created_ts_utc or datetime.now(UTC)).astimezone(UTC)
    material = {
        "version": "crossfit-provenance-manifest-v1",
        "model_fingerprint": model_fingerprint,
        "dataset_fingerprint": dataset_fingerprint,
        "truth_snapshot_hash": truth_snapshot_hash,
        "split_plan_hash": split_plan_hash,
        "assignment_universe_hash": assignment_universe_hash,
        "folds": [
            {
                "fold_id": fold.fold_id,
                "heldout_assignment_ids": list(fold.heldout_assignment_ids),
                "training_assignment_ids_hash": fold.training_assignment_ids_hash,
                "training_truth_snapshot_hash": fold.training_truth_snapshot_hash,
            }
            for fold in folds
        ],
        "created_ts_utc": created.isoformat(),
    }
    manifest_hash = _hash(material)
    return CrossFitProvenanceManifest(
        model_fingerprint=model_fingerprint,
        dataset_fingerprint=dataset_fingerprint,
        truth_snapshot_hash=truth_snapshot_hash,
        split_plan_hash=split_plan_hash,
        assignment_universe_hash=assignment_universe_hash,
        folds=tuple(folds),
        created_ts_utc=created,
        manifest_hash=manifest_hash,
    )


def verify_crossfit_manifest(manifest: CrossFitProvenanceManifest) -> tuple[str, ...]:
    failures: list[str] = []
    if manifest.created_ts_utc.tzinfo is None or manifest.created_ts_utc.utcoffset() is None:
        failures.append("crossfit_manifest_timestamp_naive")
    seen: set[str] = set()
    for fold in manifest.folds:
        overlap = seen.intersection(fold.heldout_assignment_ids)
        if overlap:
            failures.append("crossfit_heldout_assignment_duplicate")
        seen.update(fold.heldout_assignment_ids)
    if not seen:
        failures.append("crossfit_manifest_empty")
    expected_universe = _assignment_ids_hash(tuple(sorted(seen)))
    if expected_universe != manifest.assignment_universe_hash:
        failures.append("crossfit_assignment_universe_hash_mismatch")
    assignment_to_fold = {
        assignment: fold.fold_id
        for fold in manifest.folds
        for assignment in fold.heldout_assignment_ids
    }
    expected_split = _hash(
        {
            "version": "crossfit-split-plan-v1",
            "assignment_to_fold": [
                [assignment, assignment_to_fold[assignment]]
                for assignment in sorted(assignment_to_fold)
            ],
        }
    )
    if expected_split != manifest.split_plan_hash:
        failures.append("crossfit_split_plan_hash_mismatch")
    material = {
        "version": "crossfit-provenance-manifest-v1",
        "model_fingerprint": manifest.model_fingerprint,
        "dataset_fingerprint": manifest.dataset_fingerprint,
        "truth_snapshot_hash": manifest.truth_snapshot_hash,
        "split_plan_hash": manifest.split_plan_hash,
        "assignment_universe_hash": manifest.assignment_universe_hash,
        "folds": [
            {
                "fold_id": fold.fold_id,
                "heldout_assignment_ids": list(fold.heldout_assignment_ids),
                "training_assignment_ids_hash": fold.training_assignment_ids_hash,
                "training_truth_snapshot_hash": fold.training_truth_snapshot_hash,
            }
            for fold in manifest.folds
        ],
        "created_ts_utc": manifest.created_ts_utc.astimezone(UTC).isoformat(),
    }
    if _hash(material) != manifest.manifest_hash:
        failures.append("crossfit_manifest_hash_mismatch")
    return tuple(dict.fromkeys(failures))


def require_prediction_heldout(
    manifest: CrossFitProvenanceManifest,
    *,
    assignment_id: str,
    fold_id: str,
    model_fingerprint: str,
    manifest_hash: str,
) -> None:
    failures = verify_crossfit_manifest(manifest)
    if failures:
        raise ValueError("crossfit manifest invalid: " + ",".join(failures))
    if model_fingerprint != manifest.model_fingerprint:
        raise ValueError("crossfit prediction model fingerprint mismatch")
    if manifest_hash != manifest.manifest_hash:
        raise ValueError("crossfit prediction manifest hash mismatch")
    matching = [fold for fold in manifest.folds if fold.fold_id == fold_id]
    if len(matching) != 1:
        raise ValueError("crossfit prediction fold is not present in manifest")
    if assignment_id not in matching[0].heldout_assignment_ids:
        raise ValueError("crossfit prediction assignment is not held out from its model fold")
