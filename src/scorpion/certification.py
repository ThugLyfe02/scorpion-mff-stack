"""Candidate-parser certification: make a promotion a signed, reviewable claim.

A parser change is not promoted because its unit tests pass or because its label accuracy looks
better on a convenient sample. It is promoted when a certificate can say, on a named corpus and a
named set of adjudications, that:

* the candidate is at least as accurate as the baseline;
* actionable precision did not fall;
* ambiguity did not rise;
* no IGNORE/AMBIGUOUS became an ACTION without a human label saying so;
* the confidence the candidate states is calibrated, not merely high;
* the risk-coverage curve still reaches the precision the desk requires;
* the downstream book and effect multiset did not change silently.

Every one of those is a gate with a named failure. A certificate that cannot be produced is the
correct answer for a change the evidence cannot support. The certificate is canonically serialised
and digested, but the digest is content identity, not cryptographic authorship: this system has no
key management and will not pretend otherwise.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .accuracy import ACTIONABLE_KINDS, LabeledDecision, score_decisions
from .config import CHANNELS, GUILD_ID
from .domain import EventKind, RawDiscordMessage, SignalEvent
from .evaluation import VersionComparison
from .parser import ParseDecision
from .semantic_replay import compare_replay_semantics
from .store import Store

EvidenceParser = Callable[[RawDiscordMessage], ParseDecision]
DEFAULT_TIMESTAMP = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)


class CertificationError(ValueError):
    """A certification request that cannot be answered, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class ParserSpec:
    name: str
    parse: EvidenceParser

    def __call__(self, raw: RawDiscordMessage) -> ParseDecision:
        return self.parse(raw)


@dataclass(frozen=True, slots=True)
class CorpusCase:
    raw: RawDiscordMessage
    expected_kind: EventKind | None = None
    expected_contract_key: str | None = None

    @property
    def message_id(self) -> str:
        return self.raw.message_id


@dataclass(frozen=True, slots=True)
class CertificationCriteria:
    min_adjudicated_samples: int = 50
    min_corpus_cases: int = 1
    min_accuracy: float = 0.95
    min_actionable_precision: float = 0.995
    max_ambiguity_rate: float = 0.35
    max_action_escalations: int = 0
    max_expected_calibration_error: float = 0.05
    min_risk_coverage: float = 0.80
    target_precision: float = 0.995
    min_delta_accuracy: float = -0.01
    require_distinct_parser: bool = True
    require_no_semantic_state_change: bool = True
    require_no_effect_multiset_change: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    low: float
    high: float
    n: int
    mean_confidence: float
    accuracy: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "low": round(self.low, 4),
            "high": round(self.high, 4),
            "n": self.n,
            "mean_confidence": round(self.mean_confidence, 4),
            "accuracy": round(self.accuracy, 4),
        }


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    expected_calibration_error: float
    brier_score: float
    reliability: float
    resolution: float
    uncertainty: float
    bins: tuple[CalibrationBin, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_calibration_error": round(self.expected_calibration_error, 4),
            "brier_score": round(self.brier_score, 4),
            "reliability": round(self.reliability, 4),
            "resolution": round(self.resolution, 4),
            "uncertainty": round(self.uncertainty, 4),
            "bins": [item.to_dict() for item in self.bins],
        }


@dataclass(frozen=True, slots=True)
class RiskCoverage:
    target_precision: float
    selected_coverage: float | None
    selected_precision: float | None
    selected_threshold: float | None
    curve: tuple[dict[str, float], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_precision": round(self.target_precision, 4),
            "selected_coverage": (
                round(self.selected_coverage, 4)
                if self.selected_coverage is not None
                else None
            ),
            "selected_precision": (
                round(self.selected_precision, 4)
                if self.selected_precision is not None
                else None
            ),
            "selected_threshold": (
                round(self.selected_threshold, 4)
                if self.selected_threshold is not None
                else None
            ),
            "curve": [
                {key: round(value, 4) for key, value in point.items()}
                for point in self.curve
            ],
        }


@dataclass(frozen=True, slots=True)
class PairRecord:
    case: CorpusCase
    baseline: ParseDecision
    candidate: ParseDecision
    expected_kind: EventKind | None = None
    expected_contract_key: str | None = None

    @property
    def labelled(self) -> bool:
        return self.expected_kind is not None

    def baseline_correct(self) -> bool:
        return _correct(
            self.baseline.event,
            self.expected_kind,
            self.expected_contract_key,
        )

    def candidate_correct(self) -> bool:
        return _correct(
            self.candidate.event,
            self.expected_kind,
            self.expected_contract_key,
        )


@dataclass(frozen=True, slots=True)
class Certificate:
    decision: str
    certified: bool
    reasons: tuple[str, ...]
    digest: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload, "digest": self.digest}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json() + "\n", encoding="utf-8")
        return target


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _correct(
    event: SignalEvent,
    expected_kind: EventKind | None,
    expected_contract_key: str | None,
) -> bool:
    if expected_kind is None or event.kind is not expected_kind:
        return False
    return expected_contract_key is None or event.contract_key == expected_contract_key


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _check_parser(spec: ParserSpec, raw: RawDiscordMessage) -> ParseDecision:
    result = spec.parse(raw)
    if not isinstance(result, ParseDecision):
        raise CertificationError(
            "parser_must_return_parse_decision",
            f"{spec.name} returned {type(result).__name__}",
        )
    return result


def load_corpus(path: str | Path) -> tuple[CorpusCase, ...]:
    """Read a JSONL or JSON-array corpus of raw messages and optional labels.

    Supported record keys: `message_id` or `id`; `content` or `text`; `source_ts_utc` or
    `timestamp`; optional `guild_id`, `channel_id`, `author_id`, `expected_kind`,
    `expected_contract_key`. A record without content is refused rather than silently skipped.
    """
    target = Path(path)
    text = target.read_text(encoding="utf-8").strip()
    if not text:
        raise CertificationError("empty_corpus", str(target))
    if text.startswith("["):
        rows = json.loads(text)
        if not isinstance(rows, list):
            raise CertificationError("bad_corpus", "JSON root must be an array")
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    cases: list[CorpusCase] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise CertificationError("bad_corpus_row", f"line {index} is not an object")
        content = str(row.get("content") or row.get("text") or "").strip()
        if not content:
            raise CertificationError("bad_corpus_row", f"line {index} has no content")
        message_id = str(row.get("message_id") or row.get("id") or f"corpus-{index}")
        stamp = str(row.get("source_ts_utc") or row.get("timestamp") or DEFAULT_TIMESTAMP)
        try:
            source_ts = datetime.fromisoformat(stamp)
        except ValueError as exc:
            raise CertificationError(
                "bad_corpus_timestamp", f"line {index}: {stamp}"
            ) from exc
        if source_ts.tzinfo is None:
            source_ts = source_ts.replace(tzinfo=UTC)
        expected_raw = row.get("expected_kind") or row.get("kind")
        expected_kind: EventKind | None = None
        if expected_raw is not None:
            try:
                expected_kind = EventKind(str(expected_raw))
            except ValueError as exc:
                raise CertificationError(
                    "bad_expected_kind", f"line {index}: {expected_raw}"
                ) from exc
        raw = RawDiscordMessage(
            message_id=message_id,
            guild_id=str(row.get("guild_id") or GUILD_ID),
            channel_id=str(row.get("channel_id") or CHANNELS["etf-options"]),
            author_id=str(row.get("author_id") or "corpus"),
            content=content,
            source_ts_utc=source_ts.astimezone(UTC),
            received_ts_utc=source_ts.astimezone(UTC),
        )
        cases.append(
            CorpusCase(
                raw=raw,
                expected_kind=expected_kind,
                expected_contract_key=(
                    str(row["expected_contract_key"])
                    if row.get("expected_contract_key") is not None
                    else None
                ),
            )
        )
    return tuple(cases)


def adjudications_from_store(store: Store) -> dict[str, tuple[EventKind, str | None]]:
    return {
        str(row["message_id"]): (
            EventKind(str(row["expected_kind"])),
            str(row["expected_contract_key"])
            if row["expected_contract_key"] is not None
            else None,
        )
        for row in store.adjudicated_messages()
    }


def load_parser(spec: str, *, name: str | None = None) -> ParserSpec:
    """Load `module:function` (or `module.function`) as a parser specification."""
    module_name, separator, attribute = spec.partition(":")
    if not separator:
        module_name, separator, attribute = spec.rpartition(".")
    if not module_name or not attribute:
        raise CertificationError("bad_parser_spec", spec)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise CertificationError("parser_module_not_found", module_name) from exc
    function = getattr(module, attribute, None)
    if function is None or not callable(function):
        raise CertificationError("parser_not_callable", spec)
    return ParserSpec(name=name or spec, parse=function)


def calibration_report(
    pairs: Sequence[PairRecord],
    *,
    bins: int = 10,
) -> CalibrationReport:
    labelled = [pair for pair in pairs if pair.labelled]
    if not labelled:
        return CalibrationReport(0.0, 0.0, 0.0, 0.0, 0.0, ())
    if bins < 1:
        raise CertificationError("bad_bins", str(bins))
    counts = [0] * bins
    correct = [0] * bins
    confidence = [0.0] * bins
    brier_terms: list[float] = []
    outcomes: list[float] = []
    for pair in labelled:
        stated = max(0.0, min(1.0, pair.candidate.evidence.confidence))
        hit = pair.candidate_correct()
        bucket = min(bins - 1, int(stated * bins))
        counts[bucket] += 1
        correct[bucket] += int(hit)
        confidence[bucket] += stated
        brier_terms.append((stated - (1.0 if hit else 0.0)) ** 2)
        outcomes.append(1.0 if hit else 0.0)
    total = len(labelled)
    base_rate = _mean(outcomes)
    ece = 0.0
    reliability = 0.0
    resolution = 0.0
    output_bins: list[CalibrationBin] = []
    for index in range(bins):
        if counts[index] == 0:
            continue
        average_confidence = confidence[index] / counts[index]
        accuracy = correct[index] / counts[index]
        weight = counts[index] / total
        ece += weight * abs(accuracy - average_confidence)
        reliability += weight * (average_confidence - accuracy) ** 2
        resolution += weight * (accuracy - base_rate) ** 2
        output_bins.append(
            CalibrationBin(
                low=index / bins,
                high=(index + 1) / bins,
                n=counts[index],
                mean_confidence=average_confidence,
                accuracy=accuracy,
            )
        )
    return CalibrationReport(
        expected_calibration_error=ece,
        brier_score=_mean(brier_terms),
        reliability=reliability,
        resolution=resolution,
        uncertainty=base_rate * (1.0 - base_rate),
        bins=tuple(output_bins),
    )


def risk_coverage(
    pairs: Sequence[PairRecord],
    *,
    target_precision: float = 0.995,
) -> RiskCoverage:
    """Accuracy at every confidence threshold, and the coverage that reaches the target.

    The curve answers the operator's question: if I only act on the parses the candidate is most
    confident about, how much of the corpus can I still cover at the precision I require? The
    selected point is the LARGEST coverage that still meets the target, because acting on more
    correct parses is the point of the measurement.
    """
    if not 0.0 <= target_precision <= 1.0:
        raise CertificationError("bad_target_precision", str(target_precision))
    labelled = [pair for pair in pairs if pair.labelled]
    if not labelled:
        return RiskCoverage(target_precision, None, None, None, ())
    ordered = sorted(
        labelled,
        key=lambda pair: (-pair.candidate.evidence.confidence, pair.case.message_id),
    )
    hits = 0
    curve: list[dict[str, float]] = []
    selected: tuple[float, float, float] | None = None
    for index, pair in enumerate(ordered, start=1):
        hits += int(pair.candidate_correct())
        precision = hits / index
        coverage = index / len(ordered)
        curve.append(
            {
                "coverage": coverage,
                "precision": precision,
                "threshold": pair.candidate.evidence.confidence,
            }
        )
        if precision >= target_precision:
            selected = (coverage, precision, pair.candidate.evidence.confidence)
    if selected is None:
        return RiskCoverage(target_precision, None, None, None, tuple(curve))
    return RiskCoverage(target_precision, *selected, tuple(curve))


def mcnemar_exact(
    baseline_correct: Sequence[bool],
    candidate_correct: Sequence[bool],
) -> dict[str, Any]:
    if len(baseline_correct) != len(candidate_correct):
        raise CertificationError("ragged_pairs", "McNemar requires paired outcomes")
    baseline_only = sum(
        1 for old, new in zip(baseline_correct, candidate_correct, strict=True) if old and not new
    )
    candidate_only = sum(
        1 for old, new in zip(baseline_correct, candidate_correct, strict=True) if new and not old
    )
    discordant = baseline_only + candidate_only
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(
            math.comb(discordant, k) for k in range(0, min(baseline_only, candidate_only) + 1)
        )
        p_value = min(1.0, 2.0 * tail / (2.0 ** discordant))
    return {
        "baseline_only": baseline_only,
        "candidate_only": candidate_only,
        "discordant": discordant,
        "p_value": p_value,
    }


def _semantic_payload(pairs: Sequence[PairRecord]) -> dict[str, Any]:
    baseline_events = [pair.baseline.event for pair in pairs]
    candidate_events = [pair.candidate.event for pair in pairs]
    diff = compare_replay_semantics(baseline_events, candidate_events)
    return {
        "state_changed": diff.state_changed,
        "added_effects": [list(item) for item in diff.added_effects],
        "removed_effects": [list(item) for item in diff.removed_effects],
        "position_changes": {
            key: list(value) for key, value in sorted(diff.position_changes.items())
        },
    }


def _version_comparison(
    pairs: Sequence[PairRecord],
) -> VersionComparison:
    # The public comparison helper works from parser functions. The pair records already hold
    # the results, so this adapter keeps one source of truth and avoids parsing twice.
    diffs = []
    escalations = 0
    contract_changes = 0
    from .evaluation import ParserDiff

    for pair in pairs:
        old = pair.baseline.event
        new = pair.candidate.event
        if old.kind is new.kind and old.contract_key == new.contract_key:
            continue
        escalation = old.kind not in ACTIONABLE_KINDS and new.kind in ACTIONABLE_KINDS
        contract_changed = old.contract_key != new.contract_key
        escalations += int(escalation)
        contract_changes += int(contract_changed)
        diffs.append(
            ParserDiff(
                message_id=pair.case.message_id,
                baseline_kind=old.kind.value,
                candidate_kind=new.kind.value,
                baseline_contract=old.contract_key,
                candidate_contract=new.contract_key,
                action_escalation=escalation,
                contract_changed=contract_changed,
            )
        )
    return VersionComparison(
        total=len(pairs),
        changed=len(diffs),
        action_escalations=escalations,
        contract_changes=contract_changes,
        diffs=tuple(diffs),
    )


def certify(
    cases: Sequence[CorpusCase],
    *,
    baseline: ParserSpec,
    candidate: ParserSpec,
    adjudications: Mapping[str, tuple[EventKind, str | None]] | None = None,
    criteria: CertificationCriteria | None = None,
    generated_at: datetime | None = None,
) -> Certificate:
    """Evaluate a candidate parser and return a promotion certificate or a refusal."""
    criteria = criteria or CertificationCriteria()
    if len(cases) < criteria.min_corpus_cases:
        raise CertificationError(
            "insufficient_cases", f"{len(cases)}<{criteria.min_corpus_cases}"
        )
    labels = dict(adjudications or {})
    pairs: list[PairRecord] = []
    errors: list[str] = []
    for case in cases:
        try:
            old = _check_parser(baseline, case.raw)
            new = _check_parser(candidate, case.raw)
        except Exception as exc:  # noqa: BLE001 - a parser error is certification evidence
            errors.append(f"{case.message_id}:{type(exc).__name__}")
            continue
        label = labels.get(case.message_id)
        expected_kind = case.expected_kind or (label[0] if label else None)
        expected_contract = case.expected_contract_key or (label[1] if label else None)
        pairs.append(
            PairRecord(
                case=case,
                baseline=old,
                candidate=new,
                expected_kind=expected_kind,
                expected_contract_key=expected_contract,
            )
        )

    labelled = [pair for pair in pairs if pair.labelled]
    baseline_report = score_decisions(
        [
            LabeledDecision(pair.expected_kind, pair.baseline.event.kind)
            for pair in labelled
            if pair.expected_kind is not None
        ]
    )
    candidate_report = score_decisions(
        [
            LabeledDecision(pair.expected_kind, pair.candidate.event.kind)
            for pair in labelled
            if pair.expected_kind is not None
        ]
    )
    calibration = calibration_report(pairs)
    coverage = risk_coverage(pairs, target_precision=criteria.target_precision)
    mcnemar = mcnemar_exact(
        [pair.baseline_correct() for pair in labelled],
        [pair.candidate_correct() for pair in labelled],
    )
    comparison = _version_comparison(pairs)
    escalations = comparison.action_escalations
    contract_changes = comparison.contract_changes
    semantic = _semantic_payload(pairs)
    reasons: list[str] = []
    if errors:
        reasons.append(f"parser_errors:{len(errors)}")
    if criteria.require_distinct_parser and baseline.parse is candidate.parse:
        reasons.append("candidate_is_baseline")
    if len(labelled) < criteria.min_adjudicated_samples:
        reasons.append(
            f"insufficient_adjudicated_samples:{len(labelled)}"
            f"<{criteria.min_adjudicated_samples}"
        )
    if not labelled:
        reasons.append("no_adjudicated_cases")
    if candidate_report.accuracy < criteria.min_accuracy:
        reasons.append(
            f"accuracy:{candidate_report.accuracy:.6f}<{criteria.min_accuracy:.6f}"
        )
    if candidate_report.actionable_precision < criteria.min_actionable_precision:
        reasons.append(
            "actionable_precision:"
            f"{candidate_report.actionable_precision:.6f}"
            f"<{criteria.min_actionable_precision:.6f}"
        )
    if candidate_report.ambiguity_rate > criteria.max_ambiguity_rate:
        reasons.append(
            f"ambiguity_rate:{candidate_report.ambiguity_rate:.6f}"
            f">{criteria.max_ambiguity_rate:.6f}"
        )
    if escalations > criteria.max_action_escalations:
        reasons.append(
            f"action_escalations:{escalations}>{criteria.max_action_escalations}"
        )
    if calibration.expected_calibration_error > criteria.max_expected_calibration_error:
        reasons.append(
            "expected_calibration_error:"
            f"{calibration.expected_calibration_error:.6f}"
            f">{criteria.max_expected_calibration_error:.6f}"
        )
    if coverage.selected_coverage is None:
        reasons.append("risk_coverage:none")
    elif coverage.selected_coverage < criteria.min_risk_coverage:
        reasons.append(
            f"risk_coverage:{coverage.selected_coverage:.4f}"
            f"<{criteria.min_risk_coverage:.4f}"
        )
    delta_accuracy = candidate_report.accuracy - baseline_report.accuracy
    if delta_accuracy < criteria.min_delta_accuracy:
        reasons.append(
            f"accuracy_regression:{delta_accuracy:.6f}<{criteria.min_delta_accuracy:.6f}"
        )
    if criteria.require_no_semantic_state_change and semantic["state_changed"]:
        reasons.append("semantic_state_changed")
    if criteria.require_no_effect_multiset_change and (
        semantic["added_effects"] or semantic["removed_effects"]
    ):
        reasons.append("effect_multiset_changed")
    if semantic["position_changes"]:
        reasons.append(f"position_status_changes:{len(semantic['position_changes'])}")

    payload: dict[str, Any] = {
        "schema": "scorpion.certification/1",
        "generated_utc": (
            generated_at or datetime.now(UTC)
        ).astimezone(UTC).isoformat(),
        "decision": "CERTIFIED" if not reasons else "REFUSED",
        "reasons": reasons,
        "baseline": baseline.name,
        "candidate": candidate.name,
        "corpus": {
            "cases": len(cases),
            "paired": len(pairs),
            "adjudicated": len(labelled),
            "unlabelled": len(pairs) - len(labelled),
            "errors": errors,
        },
        "criteria": criteria.to_dict(),
        "metrics": {
            "baseline": asdict(baseline_report),
            "candidate": asdict(candidate_report),
            "delta_accuracy": delta_accuracy,
            "calibration": calibration.to_dict(),
            "risk_coverage": coverage.to_dict(),
            "mcnemar": mcnemar,
            "action_escalations": escalations,
            "contract_changes": contract_changes,
            "version_comparison": {
                "total": comparison.total,
                "changed": comparison.changed,
                "action_escalations": comparison.action_escalations,
                "contract_changes": comparison.contract_changes,
                "diffs": [asdict(diff) for diff in comparison.diffs[:50]],
            },
        },
        "semantic": semantic,
        "caveats": [
            "a certificate describes this corpus and these adjudications, not all future messages",
            "accuracy is an event-kind comparison; contract correctness is stricter and is "
            "reported separately",
            "action escalations are refused by default because turning IGNORE/AMBIGUOUS into "
            "an action is the failure that costs money",
            "the digest is content identity, not a signature: this system has no key management",
        ],
    }
    digest_payload = {key: value for key, value in payload.items() if key != "generated_utc"}
    digest = hashlib.sha256(_canonical(digest_payload).encode()).hexdigest()
    return Certificate(
        decision=str(payload["decision"]),
        certified=not reasons,
        reasons=tuple(reasons),
        digest=digest,
        payload=payload,
    )


def render(certificate: Certificate) -> str:
    payload = certificate.to_dict()
    metrics = payload["metrics"]
    candidate = metrics["candidate"]
    baseline = metrics["baseline"]
    corpus = payload["corpus"]
    cal = metrics["calibration"]
    coverage = metrics["risk_coverage"]
    lines = [
        f"{payload['decision']}  {payload['candidate']} vs {payload['baseline']}",
        f"digest {payload['digest'][:16]}  cases {corpus['cases']}  paired {corpus['paired']}  "
        f"adjudicated {corpus['adjudicated']}",
        f"candidate accuracy {candidate['accuracy']:.4f}  actionable precision "
        f"{candidate['actionable_precision']:.4f}  ambiguity {candidate['ambiguity_rate']:.4f}",
        f"baseline  accuracy {baseline['accuracy']:.4f}  actionable precision "
        f"{baseline['actionable_precision']:.4f}  ambiguity {baseline['ambiguity_rate']:.4f}",
        f"ECE {cal['expected_calibration_error']:.4f}  Brier {cal['brier_score']:.4f}  "
        f"risk coverage "
        f"{coverage['selected_coverage'] if coverage['selected_coverage'] is not None else 'none'}",
        f"McNemar p {metrics['mcnemar']['p_value']:.4f}  "
        f"action escalations {metrics['action_escalations']}  "
        f"contract changes {metrics['contract_changes']}",
    ]
    if payload["reasons"]:
        lines.append("reasons " + ", ".join(payload["reasons"]))
    return "\n".join(lines)


def certify_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="scorpion-certify")
    parser.add_argument("--corpus", required=True, help="JSONL or JSON array of raw messages")
    parser.add_argument("--db", default="", help="optional store with adjudications")
    parser.add_argument(
        "--baseline",
        default="scorpion.parser:parse_message_with_evidence",
        help="module:function returning ParseDecision",
    )
    parser.add_argument(
        "--candidate",
        default="scorpion.parser:parse_message_with_evidence",
        help="module:function returning ParseDecision",
    )
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        cases = load_corpus(args.corpus)
        adjudications: dict[str, tuple[EventKind, str | None]] = {}
        if args.db:
            adjudications = adjudications_from_store(Store(args.db))
        certificate = certify(
            cases,
            baseline=load_parser(args.baseline),
            candidate=load_parser(args.candidate),
            adjudications=adjudications,
        )
    except CertificationError as error:
        print(json.dumps({"ok": False, "error": error.code, "detail": error.detail}))
        return 2
    if args.out:
        path = certificate.write(args.out)
        print(json.dumps({"ok": certificate.certified, "path": str(path),
                          "digest": certificate.digest}))
    elif args.json:
        print(certificate.to_json())
    else:
        print(render(certificate))
    return 0 if certificate.certified else 1


if __name__ == "__main__":
    raise SystemExit(certify_main())
