"""Prospective paper research qualification. No execution, training or promotion authority."""
from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from pathlib import Path

from .execution_attribution import PaperExecutionCostReport, PaperRouteCost, _load_cost_policy
from .execution_meta_labels import (
    _bounded_bytes,
    _count,
    _fields,
    _hex,
    _parse_trust,
    _stamp,
    _text,
    _unique_json,
)
from .paper_session_statistics import (
    PaperCohortPolicy,
    PaperEndpointStatistics,
    PaperRegimeAxis,
    PaperSessionCohortReport,
    VerifiedPaperCohort,
    _concentration,
    _concentration_blockers,
    _Day,
    _endpoint,
    _load_cohort,
    _number,
    _regimes,
    _RouteScore,
    load_verified_paper_cohort,
)
from .provenance import canonical_json, stable_hash
from .research_hypothesis_memory import (
    ProspectiveFinalization,
    ProspectiveReservation,
    _finalize_prospective_study,
    _reserve_prospective_study,
    read_prospective_reservation,
)
from .sequential_evidence import BernoulliEPolicy, BernoulliEProcess, SequentialEvidenceStatus
from .session_calendar import session_hours
from .walk_forward import FrozenValidationWindow, verify_frozen_validation_windows

_RESERVE_DOMAIN = b"scorpion.prospective-study-approval.v1:reserve\x00"
_REVIEW_DOMAIN = b"scorpion.prospective-study-approval.v1:review\x00"
_ROSTER_DOMAIN = b"scorpion.common-entry-roster.v1\x00"


class PaperQualificationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Root:
    digest: str
    registered: datetime
    registrar_id: str
    registrar_key: bytes
    reviewer_id: str
    reviewer_key: bytes


@dataclass(frozen=True, slots=True)
class StudyArm:
    arm: str
    cohort_hash: str
    artifact_hash: str
    code_revision: str
    training_manifest_hash: str
    frozen_ts: datetime
    trained_through: datetime
    labels_through: datetime
    training_data_hashes: tuple[str, ...]
    artifact_kind: str
    validation_folds: int
    cohort_path: Path
    trust_path: Path
    cost_policy_path: Path
    inputs_path: Path
    cohort: PaperCohortPolicy
    account: str
    source: str
    rules: str
    market_source: str
    cost_horizon: int
    mark_max_age_ms: int
    outcome_horizon: int


@dataclass(frozen=True, slots=True)
class ProspectiveStudy:
    digest: str
    family_id: str
    kind: str
    registered: datetime
    alpha_units: int
    embargo_seconds: int
    sessions: tuple[date, ...]
    arms: tuple[StudyArm, ...]
    minimum_improvement_bps: float
    maximum_challenger_cost_bps: float
    soak_sessions: int
    adverse_session_null_rate: float
    adverse_session_alternative_rate: float
    upstream_source_id: str
    roster_paths: tuple[tuple[date, Path, Path, Path], ...]


@dataclass(frozen=True, slots=True)
class PairedSession:
    session_date: date
    status: str
    expected_routes: int | None
    matched_observed: int
    incumbent_cost_mean_bps: float | None
    challenger_cost_mean_bps: float | None
    raw_improvement_mean_bps: float | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperDegradationEvidence:
    observed_sessions: int
    confirmed_adverse_sessions: int
    unknown_sessions: int
    alpha: float
    anytime_p_value: float
    max_log_e_value: float
    status: str


@dataclass(frozen=True, slots=True)
class PairedEntryAssessment:
    sessions: tuple[PairedSession, ...]
    endpoint: PaperEndpointStatistics
    regime_axes: tuple[PaperRegimeAxis, ...]
    complete_sessions: int
    observed_pairs: int
    challenger_worst_route_cost_bps: float | None
    recent_soak_mean_improvement_bps: float | None
    recent_soak_mean_challenger_cost_bps: float | None
    degradation: PaperDegradationEvidence
    upstream_rosters: tuple[tuple[date, str, str], ...]
    upstream_revision_keys: tuple[str, ...]
    blockers: tuple[str, ...]
    estimand: str = "MATCHED_EXTERNAL_PAPER_ENTRY_COST_NOT_CAUSAL_POLICY_VALUE"


@dataclass(frozen=True, slots=True)
class PaperQualificationDossier:
    study_sha256: str
    root_sha256: str
    reservation_hash: str
    ledger_chain_hash: str
    family_id: str
    study_kind: str
    as_of_ts_utc: datetime
    alpha_units: int
    arm_reports: tuple[tuple[str, PaperSessionCohortReport], ...]
    paired_entry: PairedEntryAssessment | None
    evidence_identities: tuple[str, ...]
    blockers: tuple[str, ...]
    status: str
    dossier_sha256: str
    authority: str = "RESEARCH_ONLY"
    authorizes_training: bool = False
    authorizes_promotion: bool = False
    authorizes_live_orders: bool = False
    certifies_round_trip_profitability: bool = False


def _pin(path: Path, digest: str, *, limit: int = 65536) -> bytes:
    _hex(digest, 32)
    raw = _bounded_bytes(path, limit)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise PaperQualificationError("study_artifact_pin_mismatch")
    return raw


def _root(path: Path, digest: str) -> _Root:
    data = _unique_json(_pin(path, digest))
    _fields(data, {
        "schema", "authority", "registry_id", "registered_ts_utc", "registrar_id",
        "registrar_public_key", "reviewer_id", "reviewer_public_key",
    })
    if (
        data["schema"] != "scorpion.prospective-study-root.v1"
        or data["authority"] != "RESEARCH_ONLY"
    ):
        raise PaperQualificationError("study_root_scope")
    _text(data["registry_id"])
    registrar, reviewer = _text(data["registrar_id"]), _text(data["reviewer_id"])
    key_a = _hex(data["registrar_public_key"], 32)
    key_b = _hex(data["reviewer_public_key"], 32)
    if registrar == reviewer or key_a == key_b:
        raise PaperQualificationError("study_independent_reviewer_required")
    return _Root(digest, _stamp(data["registered_ts_utc"]), registrar, key_a, reviewer, key_b)


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise PaperQualificationError("naive_study_clock")
    return now.astimezone(UTC)


def _open_close(days: tuple[date, ...]) -> tuple[datetime, datetime]:
    first, last = session_hours(days[0]), session_hours(days[-1])
    if first is None or last is None:
        raise PaperQualificationError("study_calendar_invalid")
    return first[0].astimezone(UTC), last[1].astimezone(UTC)


def _input_arms(path: Path) -> dict[str, dict[str, Path]]:
    data = _unique_json(_bounded_bytes(path))
    fields = {"schema", "arms"}
    if "rosters" in data:
        fields.add("rosters")
    _fields(data, fields)
    if data["schema"] != "scorpion.prospective-study-inputs.v1":
        raise PaperQualificationError("study_input_schema")
    rows = data["arms"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise PaperQualificationError("study_input_arm_count")
    result: dict[str, dict[str, Path]] = {}
    fields = {"cohort", "trust", "cost_policy", "artifact", "training_manifest", "sessions"}
    for row in rows:
        if not isinstance(row, dict):
            raise PaperQualificationError("study_input_arm_invalid")
        _fields(row, fields | {"arm"})
        arm = _text(row["arm"])
        if arm in result:
            raise PaperQualificationError("study_input_duplicate_arm")
        result[arm] = {key: path.parent / _text(row[key]) for key in fields}
    return result


def _arm(
    row: object, paths: dict[str, dict[str, Path]], root: _Root,
    registered: datetime, embargo: int,
) -> StudyArm:
    if not isinstance(row, dict):
        raise PaperQualificationError("invalid_study_arm")
    _fields(row, {
        "arm", "cohort_sha256", "artifact_sha256", "code_revision",
        "training_manifest_sha256", "frozen_ts_utc",
    })
    arm = _text(row["arm"])
    if arm not in paths:
        raise PaperQualificationError("missing_study_arm_input")
    files = paths[arm]
    cohort_hash = _hex(row["cohort_sha256"], 32).hex()
    cohort = _load_cohort(_pin(files["cohort"], cohort_hash), cohort_hash)
    trust = _parse_trust(_pin(files["trust"], cohort.trust_sha256), cohort.trust_sha256)
    cost = _load_cost_policy(
        _pin(files["cost_policy"], cohort.cost_policy_sha256),
        cohort.cost_policy_sha256, cohort.trust_sha256,
    )
    if (
        {trust.collector_id, trust.reviewer_id} & {root.registrar_id, root.reviewer_id}
        or {trust.collector_key, trust.reviewer_key} & {root.registrar_key, root.reviewer_key}
    ):
        raise PaperQualificationError("study_operator_conflicts_with_evidence_signers")
    if max(trust.registered, cost.registered_ts_utc) > cohort.registered_ts_utc:
        raise PaperQualificationError("arm_trust_not_preregistered")
    artifact_hash = _hex(row["artifact_sha256"], 32).hex()
    if not _pin(files["artifact"], artifact_hash, limit=32 * 1024 * 1024):
        raise PaperQualificationError("empty_strategy_artifact")
    revision = _hex(row["code_revision"], 20).hex()
    manifest_hash = _hex(row["training_manifest_sha256"], 32).hex()
    manifest = _unique_json(_pin(files["training_manifest"], manifest_hash))
    _fields(manifest, {
        "schema", "authority", "artifact_sha256", "code_revision", "rules_sha256",
        "trained_through_ts_utc", "labels_available_through_ts_utc", "frozen_ts_utc",
        "training_data_sha256", "artifact_kind", "walk_forward_folds",
    })
    if (
        manifest["schema"] != "scorpion.frozen-training-lineage.v2"
        or manifest["authority"] != "RESEARCH_ONLY"
        or manifest["artifact_sha256"] != artifact_hash or manifest["code_revision"] != revision
        or manifest["rules_sha256"] != trust.rules_sha256
    ):
        raise PaperQualificationError("frozen_lineage_scope")
    frozen = _stamp(row["frozen_ts_utc"])
    trained = _stamp(manifest["trained_through_ts_utc"])
    labels = _stamp(manifest["labels_available_through_ts_utc"])
    first, _ = _open_close(cohort.sessions)
    if (
        _stamp(manifest["frozen_ts_utc"]) != frozen
        or not trained <= labels <= frozen <= cohort.registered_ts_utc <= registered < first
        or first - frozen < timedelta(seconds=embargo)
    ):
        raise PaperQualificationError("frozen_lineage_temporal_embargo")
    raw_data = manifest["training_data_sha256"]
    if not isinstance(raw_data, list) or not 1 <= len(raw_data) <= 10000:
        raise PaperQualificationError("training_lineage_data_missing")
    training = tuple(sorted(_hex(value, 32).hex() for value in raw_data))
    if len(set(training)) != len(training):
        raise PaperQualificationError("training_lineage_duplicate_data")
    artifact_kind = _text(manifest["artifact_kind"])
    if artifact_kind not in ("DETERMINISTIC_RULES", "TRAINED_MODEL"):
        raise PaperQualificationError("unknown_frozen_artifact_kind")
    raw_folds = manifest["walk_forward_folds"]
    if not isinstance(raw_folds, list) or len(raw_folds) > 1000:
        raise PaperQualificationError("invalid_declared_walk_forward_folds")
    windows: list[FrozenValidationWindow] = []
    for fold in raw_folds:
        if not isinstance(fold, dict):
            raise PaperQualificationError("invalid_declared_walk_forward_fold")
        _fields(fold, {
            "train_through_ts_utc", "training_labels_available_through_ts_utc",
            "validation_start_ts_utc", "validation_end_ts_utc",
            "validation_labels_available_through_ts_utc", "training_data_sha256",
            "validation_data_sha256",
        })
        fold_data = fold["training_data_sha256"]
        if not isinstance(fold_data, list) or len(fold_data) > 10000:
            raise PaperQualificationError("invalid_fold_training_data")
        windows.append(FrozenValidationWindow(
            _stamp(fold["train_through_ts_utc"]),
            _stamp(fold["training_labels_available_through_ts_utc"]),
            _stamp(fold["validation_start_ts_utc"]), _stamp(fold["validation_end_ts_utc"]),
            _stamp(fold["validation_labels_available_through_ts_utc"]),
            tuple(_hex(value, 32).hex() for value in fold_data),
            _hex(fold["validation_data_sha256"], 32).hex(),
        ))
    lineage = verify_frozen_validation_windows(
        tuple(windows), embargo=timedelta(seconds=embargo), frozen=frozen,
        declared_data=frozenset(training), minimum_folds=3 if artifact_kind == "TRAINED_MODEL" else 0,
    )
    if not lineage.valid or any(
        window.train_through > trained or window.training_labels_available_through > labels
        or window.validation_labels_available_through > labels
        for window in windows
    ):
        raise PaperQualificationError("walk_forward_lineage_invalid")
    return StudyArm(
        arm, cohort_hash, artifact_hash, revision, manifest_hash, frozen, trained, labels,
        training, artifact_kind, lineage.folds,
        files["cohort"], files["trust"], files["cost_policy"], files["sessions"],
        cohort, trust.account_ref, trust.source_id, trust.rules_sha256,
        cost.market_source_id, cost.horizon_seconds, cost.max_mark_age_ms,
        trust.horizon_seconds,
    )


def _study(path: Path, digest: str, inputs: Path, root: _Root) -> ProspectiveStudy:
    data = _unique_json(_pin(path, digest))
    _fields(data, {
        "schema", "authority", "family_id", "kind", "registered_ts_utc", "alpha_units",
        "embargo_seconds", "minimum_improvement_bps", "maximum_challenger_cost_bps",
        "soak_sessions", "arms", "adverse_session_null_rate", "adverse_session_alternative_rate",
        "upstream_source_id",
    })
    if (
        data["schema"] != "scorpion.prospective-study.v1" or data["authority"] != "RESEARCH_ONLY"
        or data["kind"] not in ("COHORT", "PAIRED_ENTRY_TRIAL")
    ):
        raise PaperQualificationError("study_scope")
    kind = _text(data["kind"])
    registered = _stamp(data["registered_ts_utc"])
    if root.registered > registered:
        raise PaperQualificationError("study_root_registered_too_late")
    family_id = _text(data["family_id"])
    units, embargo = _count(data["alpha_units"]), _count(data["embargo_seconds"])
    if units > 50_000_000 or not 86400 <= embargo <= 365 * 86400:
        raise PaperQualificationError("study_budget_or_embargo_bounds")
    paths = _input_arms(inputs)
    values = data["arms"]
    if not isinstance(values, list) or len(values) != (1 if kind == "COHORT" else 2):
        raise PaperQualificationError("study_arm_count")
    arms = tuple(_arm(row, paths, root, registered, embargo) for row in values)
    required = ("COHORT",) if kind == "COHORT" else ("INCUMBENT", "CHALLENGER")
    if tuple(arm.arm for arm in arms) != required or set(paths) != set(required):
        raise PaperQualificationError("study_arm_order_or_scope")
    first = arms[0].cohort
    if any(
        arm.cohort.family_id != family_id
        or Decimal(str(arm.cohort.family_alpha)) * 1_000_000_000 != units
        for arm in arms
    ):
        raise PaperQualificationError("study_cohort_alpha_or_family_mismatch")
    if kind == "PAIRED_ENTRY_TRIAL":
        left, right = arms
        if (
            left.account == right.account or left.source != right.source
            or left.rules == right.rules or left.artifact_hash == right.artifact_hash
            or left.market_source != right.market_source or left.cost_horizon != right.cost_horizon
            or left.mark_max_age_ms != right.mark_max_age_ms
            or left.outcome_horizon != right.outcome_horizon
            or replace(right.cohort, trust_sha256=first.trust_sha256,
                       cost_policy_sha256=first.cost_policy_sha256) != first
        ):
            raise PaperQualificationError("paired_arm_protocol_mismatch")
    improvement = _number(data["minimum_improvement_bps"], minimum=0, maximum=first.clip_bps)
    max_cost = _number(data["maximum_challenger_cost_bps"], minimum=0, maximum=1e9)
    soak = _count(data["soak_sessions"])
    if not 2 <= soak <= len(first.sessions):
        raise PaperQualificationError("invalid_soak_horizon")
    null = _number(data["adverse_session_null_rate"], minimum=0.01, maximum=0.98)
    alternative = _number(data["adverse_session_alternative_rate"], minimum=0.02, maximum=0.99)
    if alternative <= null:
        raise PaperQualificationError("invalid_degradation_alternative")
    upstream = _text(data["upstream_source_id"])
    if kind == "PAIRED_ENTRY_TRIAL" and upstream in {
        arm.source for arm in arms
    }.union(arm.market_source for arm in arms):
        raise PaperQualificationError("upstream_roster_source_not_independent")
    roster_paths = _roster_input_paths(inputs, first.sessions, paired=kind == "PAIRED_ENTRY_TRIAL")
    return ProspectiveStudy(
        digest, family_id, kind, registered, units, embargo, first.sessions, arms,
        improvement, max_cost, soak, null, alternative, upstream, roster_paths,
    )


@dataclass(frozen=True, slots=True)
class _Approval:
    digest: str
    raw_hex: str
    signature: str
    disposition: str


def _approval(
    path: Path, signature_path: Path, root: _Root, study: ProspectiveStudy,
    expected_chain: str, now: datetime, *, dossier: PaperQualificationDossier | None = None,
) -> _Approval:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    _hex(expected_chain, 32)
    raw = _bounded_bytes(path, 65536)
    data = _unique_json(raw)
    final = dossier is not None
    fields = {
        "schema", "authority", "action", "root_sha256", "study_sha256", "ledger_chain_hash",
        "signer_id", "issued_ts_utc", "expires_ts_utc",
    }
    if final:
        fields |= {"dossier_sha256", "disposition"}
    _fields(data, fields)
    key = root.reviewer_key if final else root.registrar_key
    signer = root.reviewer_id if final else root.registrar_id
    domain = _REVIEW_DOMAIN if final else _RESERVE_DOMAIN
    try:
        signature = _hex(_bounded_bytes(signature_path, 256).decode("ascii").strip(), 64)
        Ed25519PublicKey.from_public_bytes(key).verify(signature, domain + raw)
    except (InvalidSignature, ValueError, UnicodeError) as exc:
        raise PaperQualificationError("study_approval_signature_invalid") from exc
    issued, expires = _stamp(data["issued_ts_utc"]), _stamp(data["expires_ts_utc"])
    if (
        data["schema"] != "scorpion.prospective-study-approval.v1"
        or data["authority"] != "RESEARCH_ONLY"
        or data["action"] != ("REVIEW" if final else "RESERVE")
        or data["root_sha256"] != root.digest or data["study_sha256"] != study.digest
        or data["ledger_chain_hash"] != expected_chain or data["signer_id"] != signer
        or not study.registered <= issued <= now <= expires
        or expires - issued > timedelta(days=7)
    ):
        raise PaperQualificationError("study_approval_scope_or_expiry")
    disposition = "RESERVED"
    if dossier is not None:
        disposition = _text(data["disposition"])
        if (
            data["dossier_sha256"] != dossier.dossier_sha256
            or issued < dossier.as_of_ts_utc
            or disposition not in ("PAPER_RESEARCH_REVIEWED", "INCONCLUSIVE", "REJECTED")
            or (disposition == "PAPER_RESEARCH_REVIEWED"
                and dossier.status != "READY_FOR_INDEPENDENT_PAPER_REVIEW")
        ):
            raise PaperQualificationError("study_review_disposition_or_dossier_mismatch")
    return _Approval(
        stable_hash((hashlib.sha256(raw).hexdigest(), signature.hex())),
        raw.hex(), signature.hex(), disposition,
    )


def reserve_paper_study(
    *, registry_path: Path, root_path: Path, root_sha256: str, study_path: Path,
    study_sha256: str, inputs_path: Path, expected_chain_hash: str,
    approval_path: Path, signature_path: Path, now: datetime | None = None,
) -> ProspectiveReservation:
    """Explicit operator action; the CLI uses the actual UTC clock."""
    clock = _aware(now or datetime.now(UTC))
    root = _root(root_path, root_sha256)
    study = _study(study_path, study_sha256, inputs_path, root)
    first, _ = _open_close(study.sessions)
    if not study.registered <= clock < first:
        raise PaperQualificationError("study_reservation_not_prospective")
    approval = _approval(
        approval_path, signature_path, root, study, expected_chain_hash, clock,
    )
    return _reserve_prospective_study(
        registry_path, study_sha256=study.digest, root_sha256=root.digest,
        family_id=study.family_id, study_kind=study.kind, alpha_units=study.alpha_units,
        sessions=study.sessions, approval_sha256=approval.digest,
        approval_raw_hex=approval.raw_hex, approval_signature=approval.signature,
        expected_chain_hash=expected_chain_hash, now=clock,
    )


def _roster_input_paths(
    path: Path, sessions: tuple[date, ...], *, paired: bool,
) -> tuple[tuple[date, Path, Path, Path], ...]:
    data = _unique_json(_bounded_bytes(path))
    values = data.get("rosters", [])
    if not isinstance(values, list) or len(values) > len(sessions) or (values and not paired):
        raise PaperQualificationError("invalid_common_roster_index")
    result: dict[date, tuple[Path, Path, Path]] = {}
    for row in values:
        if not isinstance(row, dict):
            raise PaperQualificationError("invalid_common_roster_input")
        _fields(row, {"session_date", "roster", "source_artifact", "signature"})
        day = date.fromisoformat(_text(row["session_date"]))
        if day not in sessions or day in result:
            raise PaperQualificationError("duplicate_or_outside_common_roster_date")
        result[day] = (
            path.parent / _text(row["roster"]), path.parent / _text(row["source_artifact"]),
            path.parent / _text(row["signature"]),
        )
    return tuple((day, *paths) for day, paths in sorted(result.items()))


@dataclass(frozen=True, slots=True)
class _CommonRoster:
    digest: str
    source_artifact_sha256: str
    export_sha256: str
    expected_routes: int
    revisions: tuple[str, ...]


def _roster_decimal(value: object, *, allow_zero: bool = False) -> Decimal:
    raw = _text(value)
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise PaperQualificationError("invalid_roster_decimal") from exc
    if (
        len(raw) > 128 or not number.is_finite() or number < 0
        or (not allow_zero and number == 0) or number > 1_000_000_000
        or not isinstance(number.as_tuple().exponent, int)
        or abs(int(number.as_tuple().exponent)) > 128
    ):
        raise PaperQualificationError("invalid_roster_decimal")
    return number


def _load_common_roster(
    *, root: _Root, study: ProspectiveStudy, day: date, as_of: datetime,
    paths: tuple[Path, Path, Path],
) -> _CommonRoster:
    """Separate registrar attestation over the common upstream population."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    roster_path, artifact_path, signature_path = paths
    raw = _bounded_bytes(roster_path)
    try:
        signature = _hex(_bounded_bytes(signature_path, 256).decode("ascii").strip(), 64)
        Ed25519PublicKey.from_public_bytes(root.registrar_key).verify(
            signature, _ROSTER_DOMAIN + raw,
        )
    except (InvalidSignature, ValueError, UnicodeError) as exc:
        raise PaperQualificationError("common_roster_signature_invalid") from exc
    data = _unique_json(raw)
    _fields(data, {
        "schema", "authority", "study_sha256", "session_date", "source_id",
        "source_artifact_sha256", "complete_roster", "observed_through_ts_utc",
        "signed_ts_utc", "routes",
    })
    artifact = _bounded_bytes(artifact_path)
    artifact_hash, export_hash = hashlib.sha256(artifact).hexdigest(), hashlib.sha256(raw).hexdigest()
    hours = session_hours(day)
    if hours is None:
        raise PaperQualificationError("common_roster_calendar")
    if (
        not artifact or artifact_hash == export_hash
        or data["schema"] != "scorpion.common-entry-roster.v1"
        or data["authority"] != "RESEARCH_ONLY" or data["study_sha256"] != study.digest
        or data["session_date"] != day.isoformat() or data["source_id"] != study.upstream_source_id
        or data["source_artifact_sha256"] != artifact_hash or data["complete_roster"] is not True
        or not hours[1] <= _stamp(data["observed_through_ts_utc"])
        <= _stamp(data["signed_ts_utc"]) <= as_of
    ):
        raise PaperQualificationError("common_roster_scope_or_availability")
    values = data["routes"]
    if not isinstance(values, list) or len(values) > 100_000:
        raise PaperQualificationError("common_roster_size")
    entries: dict[str, tuple[object, ...]] = {}
    for row in values:
        if not isinstance(row, dict):
            raise PaperQualificationError("invalid_common_roster_row")
        _fields(row, {
            "revision_id", "ticker", "source_ts_utc", "initial_ts_utc", "target_quantity",
            "decision_midpoint", "horizon_ts_utc", "initial_spread_bps", "initial_quote_event_ns",
        })
        revision = _hex(row["revision_id"], 32).hex()
        source, initial = _stamp(row["source_ts_utc"]), _stamp(row["initial_ts_utc"])
        horizon = _stamp(row["horizon_ts_utc"])
        event_ns = row["initial_quote_event_ns"]
        initial_ns = int(initial.timestamp()) * 1_000_000_000 + initial.microsecond * 1000
        if (
            revision in entries or not source <= initial
            or not hours[0] <= initial < horizon < hours[1]
            or horizon - initial != timedelta(seconds=study.arms[0].cost_horizon)
            or not isinstance(event_ns, int) or isinstance(event_ns, bool)
            or not 0 < event_ns <= initial_ns
        ):
            raise PaperQualificationError("common_roster_identity_or_clock_conflict")
        entries[revision] = (
            revision, _text(row["ticker"]), source, initial, _count(row["target_quantity"]),
            _roster_decimal(row["decision_midpoint"]), horizon,
            _roster_decimal(row["initial_spread_bps"], allow_zero=True), event_ns,
        )
    return _CommonRoster(
        stable_hash(tuple(entries[key] for key in sorted(entries))), artifact_hash, export_hash,
        len(entries), tuple(sorted(entries)),
    )


def _roster_material(route: PaperRouteCost) -> tuple[object, ...]:
    return (
        route.revision_id, route.ticker, route.source_ts_utc, route.initial_ts_utc,
        route.target_quantity, route.decision_midpoint, route.horizon_ts_utc,
        route.initial_spread_bps, route.initial_quote_event_ns,
    )


def common_roster_digest(routes: tuple[PaperRouteCost, ...]) -> str:
    """Canonical common opportunity roster for normalized signed arm evidence."""
    return stable_hash(tuple(_roster_material(row) for row in sorted(
        routes, key=lambda row: row.revision_id,
    )))


def execution_binding_digest(study: ProspectiveStudy, arm: StudyArm, day: date,
                             cost: PaperExecutionCostReport) -> str:
    """Digest of the declared execution identity; not remote runtime attestation."""
    return stable_hash((
        "paper-execution-binding-v1", study.digest, arm.arm, arm.artifact_hash,
        arm.code_revision, day, arm.account, cost.journal_sha256, cost.source_artifact_sha256,
    ))


def _check_binding(study: ProspectiveStudy, arm: StudyArm, day: date,
                   cost: PaperExecutionCostReport) -> str:
    binding = cost.trial_binding
    roster = common_roster_digest(cost.routes)
    if binding is None or (
        binding.study_sha256 != study.digest or binding.arm != arm.arm
        or binding.artifact_sha256 != arm.artifact_hash or binding.code_revision != arm.code_revision
        or binding.common_roster_sha256 != roster
        or binding.execution_manifest_sha256 != execution_binding_digest(study, arm, day, cost)
    ):
        raise PaperQualificationError("signed_trial_binding_mismatch")
    return roster


def _pair_day(
    day: date, left: PaperExecutionCostReport | None, right: PaperExecutionCostReport | None,
    left_day: _Day, right_day: _Day, study: ProspectiveStudy,
    roster: _CommonRoster | None,
) -> tuple[_Day, PairedSession, list[float]]:
    def unknown(reason: str) -> tuple[_Day, PairedSession, list[float]]:
        return (
            _Day(day, "MISSING_SESSION", None, (), None, (reason,)),
            PairedSession(day, "UNKNOWN", roster.expected_routes if roster else None,
                          0, None, None, None, (reason,)), [],
        )

    if roster is None:
        return unknown("independent_common_roster_unavailable")
    if left is None or right is None or left_day.expected is None or right_day.expected is None:
        return unknown("arm_coverage_unverified")
    left_roster = _check_binding(study, study.arms[0], day, left)
    right_roster = _check_binding(study, study.arms[1], day, right)
    if (
        left.journal_sha256 == right.journal_sha256
        or left.outcome_export_sha256 == right.outcome_export_sha256
        or left.source_artifact_sha256 == right.source_artifact_sha256
    ):
        raise PaperQualificationError("paired_arm_execution_artifact_reuse")
    if (
        left_roster != roster.digest or right_roster != roster.digest
        or left.market_artifact_sha256 != right.market_artifact_sha256
    ):
        return unknown("paired_common_roster_or_market_mismatch")
    left_routes = {r.revision_id: r for r in left.routes}
    right_routes = {r.revision_id: r for r in right.routes}
    regimes = {r.call_id: r.regimes for r in left_day.scores}
    scores: list[_RouteScore] = []
    a_costs: list[float] = []
    b_costs: list[float] = []
    improvements: list[float] = []
    for key in sorted(left_routes):
        a, b = left_routes[key], right_routes[key]
        delta: float | None = None
        if (
            a.complete and b.complete
            and a.horizon_midpoint == b.horizon_midpoint
            and a.mark_quote_id == b.mark_quote_id
            and a.net_shortfall_bps is not None and b.net_shortfall_bps is not None
        ):
            with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                delta = float(a.net_shortfall_bps - b.net_shortfall_bps)
            a_costs.append(float(a.net_shortfall_bps))
            b_costs.append(float(b.net_shortfall_bps))
            improvements.append(delta)
        scores.append(_RouteScore(key, a.ticker, regimes[a.call_id], delta, 0.0, False))
    complete = len(improvements) == len(scores)
    status = "COVERED_EMPTY" if not scores else (
        "COVERED_COMPLETE" if complete else "COVERED_CENSORED"
    )
    reasons = () if complete else ("paired_outcome_or_horizon_incomplete",)
    pair = _Day(
        day, status, len(scores), tuple(scores),
        stable_hash((left.report_sha256, right.report_sha256)), reasons,
    )

    def mean(values: list[float]) -> float | None:
        return statistics.fmean(values) if values else (0.0 if not scores else None)

    return pair, PairedSession(
        day, status, len(scores), len(improvements), mean(a_costs), mean(b_costs),
        mean(improvements), reasons,
    ), b_costs


def _paired(
    root: _Root, study: ProspectiveStudy, left: VerifiedPaperCohort, right: VerifiedPaperCohort,
    as_of: datetime,
) -> PairedEntryAssessment:
    policy = replace(left.policy, minimum_primary_score_bps=study.minimum_improvement_bps)
    left_costs, right_costs = dict(left.costs), dict(right.costs)
    left_private = {identity for _, c in left.costs for identity in (
        c.journal_sha256, c.outcome_export_sha256, c.source_artifact_sha256, c.cost_export_sha256,
    )}
    right_private = {identity for _, c in right.costs for identity in (
        c.journal_sha256, c.outcome_export_sha256, c.source_artifact_sha256, c.cost_export_sha256,
    )}
    left_market = {c.market_artifact_sha256 for _, c in left.costs}
    right_market = {c.market_artifact_sha256 for _, c in right.costs}
    if left_private & (right_private | right_market) or right_private & left_market:
        raise PaperQualificationError("paired_arm_execution_artifact_reuse")
    paired_days: list[_Day] = []
    rows: list[PairedSession] = []
    challenger_costs: list[float] = []
    roster_paths = {day: (path, artifact, sig) for day, path, artifact, sig in study.roster_paths}
    roster_rows: list[tuple[date, str, str]] = []
    seen_roster_artifacts: set[str] = set()
    seen_roster_revisions: set[str] = set()
    arm_artifacts = left_private | right_private | {
        c.market_artifact_sha256 for _, c in left.costs + right.costs
    }
    if set(roster_paths) - {day.date for day in left.days}:
        raise PaperQualificationError("future_common_roster_input")
    for a, b in zip(left.days, right.days, strict=True):
        if a.date != b.date:
            raise PaperQualificationError("paired_calendar_mismatch")
        paths = roster_paths.get(a.date)
        roster = None
        if paths is not None:
            roster = _load_common_roster(root=root, study=study, day=a.date, as_of=as_of, paths=paths)
            identifiers = {roster.source_artifact_sha256, roster.export_sha256}
            if identifiers & (arm_artifacts | seen_roster_artifacts):
                raise PaperQualificationError("independent_roster_artifact_reuse")
            if seen_roster_revisions.intersection(roster.revisions):
                raise PaperQualificationError("independent_roster_revision_reuse")
            seen_roster_artifacts.update(identifiers)
            seen_roster_revisions.update(roster.revisions)
            roster_rows.append((a.date, roster.source_artifact_sha256, roster.export_sha256))
        day, row, costs = _pair_day(
            a.date, left_costs.get(a.date), right_costs.get(a.date), a, b, study, roster,
        )
        paired_days.append(day)
        rows.append(row)
        challenger_costs.extend(costs)
    return _summarize_paired(
        study, policy, paired_days, rows, challenger_costs, tuple(roster_rows),
        tuple(sorted(stable_hash(("revision", key)) for key in seen_roster_revisions)),
    )


def _summarize_paired(
    study: ProspectiveStudy, policy: PaperCohortPolicy,
    paired_days: list[_Day], rows: list[PairedSession], challenger_costs: list[float],
    roster_rows: tuple[tuple[date, str, str], ...], revision_keys: tuple[str, ...],
) -> PairedEntryAssessment:
    """Pure statistics seam; public study assessment always verifies original artifacts."""
    if tuple(day.date for day in paired_days) != study.sessions[:len(paired_days)] or (
        tuple(row.session_date for row in rows) != tuple(day.date for day in paired_days)
    ):
        raise PaperQualificationError("paired_statistics_session_identity_mismatch")
    complete = sum(row.status in ("COVERED_COMPLETE", "COVERED_EMPTY") for row in rows)
    observed = sum(row.matched_observed for row in rows)
    blockers: list[str] = []
    if len(rows) != len(study.sessions):
        blockers.append("prospective_horizon_not_finished")
    if complete != len(rows) or not rows:
        blockers.append("paired_session_coverage_incomplete")
    if len(rows) < policy.minimum_sessions or observed < policy.minimum_routes:
        blockers.append("paired_sample_insufficient")
    blockers.extend(_concentration_blockers(_concentration(paired_days), policy))
    endpoint = _endpoint(
        paired_days, policy, "primary", tuple(blockers), 0,
        alpha_override=0.8 * study.alpha_units / 1_000_000_000,
    )
    blockers.extend(endpoint.blockers)
    axes = _regimes(paired_days, policy, "primary")
    if any(axis.status != "DESCRIPTIVE_STRESS_OK" for axis in axes):
        blockers.append("paired_regime_robustness_not_supported")
    worst = max(challenger_costs, default=None)
    if worst is None or worst > study.maximum_challenger_cost_bps:
        blockers.append("challenger_absolute_cost_limit")
    recent = rows[-study.soak_sessions:]
    recent_delta = recent_cost = None
    if len(recent) == study.soak_sessions and all(
        r.status in ("COVERED_COMPLETE", "COVERED_EMPTY")
        and r.raw_improvement_mean_bps is not None and r.challenger_cost_mean_bps is not None
        for r in recent
    ):
        recent_delta = statistics.fmean(
            r.raw_improvement_mean_bps for r in recent if r.raw_improvement_mean_bps is not None
        )
        recent_cost = statistics.fmean(
            r.challenger_cost_mean_bps for r in recent if r.challenger_cost_mean_bps is not None
        )
    if (
        recent_delta is None or recent_delta <= study.minimum_improvement_bps
        or recent_cost is None or recent_cost > study.maximum_challenger_cost_bps
    ):
        blockers.append("recent_soak_degradation_or_insufficient")
    degradation = _degradation(tuple(rows), study)
    if degradation.status == "ALARM":
        blockers.append("sequential_adverse_session_alarm")
    return PairedEntryAssessment(
        tuple(rows), endpoint, axes, complete, observed, worst, recent_delta, recent_cost,
        degradation, roster_rows, revision_keys, tuple(dict.fromkeys(blockers)),
    )


def _degradation(
    rows: tuple[PairedSession, ...], study: ProspectiveStudy,
) -> PaperDegradationEvidence:
    alpha = 0.2 * study.alpha_units / 1_000_000_000
    monitor = BernoulliEProcess(BernoulliEPolicy(
        null_rate=study.adverse_session_null_rate,
        alternative_rate=study.adverse_session_alternative_rate, alpha=alpha,
    ))
    unknown = 0
    for row in rows:
        complete = row.status in ("COVERED_COMPLETE", "COVERED_EMPTY")
        delta, cost = row.raw_improvement_mean_bps, row.challenger_cost_mean_bps
        known = complete and delta is not None and cost is not None
        adverse = bool(
            known and delta is not None and cost is not None
            and (delta <= study.minimum_improvement_bps or cost > study.maximum_challenger_cost_bps)
        )
        if not known:
            unknown += 1
        # Unknown days contribute the lower indicator only, never evidence of health.
        monitor.observe(adverse)
    snapshot = monitor.snapshot()
    status = (
        "ALARM" if snapshot.status is SequentialEvidenceStatus.ALARM
        else "UNRESOLVED" if unknown or not rows else "NO_ALARM_NOT_PROOF_OF_HEALTH"
    )
    return PaperDegradationEvidence(
        len(rows) - unknown, snapshot.violations, unknown, alpha, snapshot.anytime_p_value,
        monitor.max_log_e_value, status,
    )


def _dossier(
    root: _Root, study: ProspectiveStudy, reservation: ProspectiveReservation, as_of: datetime,
) -> PaperQualificationDossier:
    first, close = _open_close(study.sessions)
    if (
        reservation.study_kind != study.kind or reservation.alpha_units != study.alpha_units
        or reservation.family_id != study.family_id or reservation.sessions != study.sessions
        or not study.registered <= reservation.reserved_ts_utc < first
        or as_of < reservation.reserved_ts_utc
    ):
        raise PaperQualificationError("study_reservation_binding_mismatch")
    cohorts = tuple(load_verified_paper_cohort(
        cohort_path=arm.cohort_path, expected_cohort_sha256=arm.cohort_hash,
        inputs_path=arm.inputs_path, trust_path=arm.trust_path,
        cost_policy_path=arm.cost_policy_path, as_of=as_of,
    ) for arm in study.arms)
    identity_set = {item for c in cohorts for item in c.evidence_identities}
    training = {stable_hash(("artifact", item)) for arm in study.arms
                for item in arm.training_data_hashes}
    blockers: list[str] = []
    if as_of < close:
        blockers.append("prospective_horizon_not_finished")
    paired = None
    if study.kind == "COHORT":
        report = cohorts[0].report
        if (
            report.primary.status != "BOUNDED_RESEARCH_SUPPORT"
            or report.chase.status != "BOUNDED_RESEARCH_SUPPORT"
            or report.regime_robustness != "DESCRIPTIVE_STRESS_OK"
            or report.complete_sessions != report.planned_sessions
        ):
            blockers.append("cohort_research_support_incomplete")
    else:
        paired = _paired(root, study, cohorts[0], cohorts[1], as_of)
        blockers.extend(paired.blockers)
        identity_set.update(stable_hash(("artifact", digest))
                            for _, source, export in paired.upstream_rosters
                            for digest in (source, export))
        identity_set.update(paired.upstream_revision_keys)
    identities = tuple(sorted(identity_set))
    if training.intersection(identities):
        raise PaperQualificationError("training_evaluation_artifact_overlap")
    arm_reports = tuple((arm.arm, c.report) for arm, c in zip(study.arms, cohorts, strict=True))
    final_blockers = tuple(dict.fromkeys(blockers))
    material = {
        "schema": "scorpion.paper-qualification-dossier.v1", "study_sha256": study.digest,
        "root_sha256": root.digest, "reservation_hash": reservation.record_hash,
        "ledger_chain_hash": reservation.ledger_chain_hash, "as_of": as_of,
        "arms": tuple((name, report.report_sha256) for name, report in arm_reports),
        "paired": paired, "evidence_identities": identities, "blockers": final_blockers,
    }
    return PaperQualificationDossier(
        study.digest, root.digest, reservation.record_hash, reservation.ledger_chain_hash,
        study.family_id, study.kind, as_of, study.alpha_units, arm_reports, paired, identities,
        final_blockers, "NOT_QUALIFIED" if final_blockers else "READY_FOR_INDEPENDENT_PAPER_REVIEW",
        stable_hash(material),
    )


def assess_paper_study(
    *, registry_path: Path, root_path: Path, root_sha256: str, study_path: Path,
    study_sha256: str, inputs_path: Path, expected_chain_hash: str, as_of: datetime,
) -> PaperQualificationDossier:
    """Recompute signed evidence; no state mutation or promotion."""
    root = _root(root_path, root_sha256)
    study = _study(study_path, study_sha256, inputs_path, root)
    reservation = read_prospective_reservation(
        registry_path, study_sha256=study.digest, root_sha256=root.digest,
        expected_chain_hash=expected_chain_hash,
    )
    return _dossier(root, study, reservation, _aware(as_of))


def finalize_paper_study(
    *, registry_path: Path, root_path: Path, root_sha256: str, study_path: Path,
    study_sha256: str, inputs_path: Path, expected_chain_hash: str, as_of: datetime,
    approval_path: Path, signature_path: Path, now: datetime | None = None,
) -> ProspectiveFinalization:
    """One-shot, independent signed disposition of the recomputed research dossier."""
    clock, cutoff = _aware(now or datetime.now(UTC)), _aware(as_of)
    if cutoff > clock:
        raise PaperQualificationError("study_review_future_cutoff")
    root = _root(root_path, root_sha256)
    study = _study(study_path, study_sha256, inputs_path, root)
    reservation = read_prospective_reservation(
        registry_path, study_sha256=study.digest, root_sha256=root.digest,
        expected_chain_hash=expected_chain_hash,
    )
    dossier = _dossier(root, study, reservation, cutoff)
    approval = _approval(
        approval_path, signature_path, root, study, expected_chain_hash, clock, dossier=dossier,
    )
    return _finalize_prospective_study(
        registry_path, study_sha256=study.digest, root_sha256=root.digest,
        dossier_sha256=dossier.dossier_sha256, approval_sha256=approval.digest,
        approval_raw_hex=approval.raw_hex, approval_signature=approval.signature,
        expected_chain_hash=expected_chain_hash, disposition=approval.disposition, now=clock,
        evidence_keys=dossier.evidence_identities, dossier_json=canonical_json(dossier),
    )
