"""Certification: a promotion is a gate, and every gate has a named refusal."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from scorpion.accuracy import DecisionEvidence
from scorpion.certification import (
    CertificationCriteria,
    CertificationError,
    CorpusCase,
    PairRecord,
    ParserSpec,
    calibration_report,
    certify,
    certify_main,
    load_certificate,
    load_corpus,
    load_parser,
    mcnemar_exact,
    render,
    risk_coverage,
    segment_report,
    verify_certificate,
)
from scorpion.config import CHANNELS
from scorpion.domain import EventKind, SignalEvent
from scorpion.parser import ParseDecision, parse_message_with_evidence

BASE = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)


def _decision(
    kind: EventKind,
    *,
    confidence: float = 0.99,
    contract: bool = True,
) -> ParseDecision:
    event = SignalEvent(
        event_id=f"e-{kind.value}-{confidence}",
        message_id=f"m-{kind.value}-{confidence}",
        kind=kind,
        channel_id="1231301953972207667",
        author_id="author",
        source_ts_utc=BASE,
        received_ts_utc=BASE,
        ticker="QQQ" if contract else None,
        option_side="CALL" if contract else None,
        strike=Decimal("700") if contract else None,
        expiry=date(2026, 9, 8) if contract else None,
        referenced_price=Decimal("1.00") if contract else None,
    )
    return ParseDecision(
        event,
        DecisionEvidence(
            "entry.complete" if contract else "ignore.no_supported_signal",
            confidence,
        ),
    )


def _pair(
    message_id: str,
    confidence: float,
    correct: bool,
    *,
    expected: EventKind = EventKind.ENTRY,
) -> PairRecord:
    predicted = expected if correct else EventKind.IGNORE
    raw = SignalEvent(
        event_id=message_id,
        message_id=message_id,
        kind=predicted,
        channel_id="1231301953972207667",
        author_id="author",
        source_ts_utc=BASE,
        received_ts_utc=BASE,
        ticker="QQQ",
        option_side="CALL",
        strike=Decimal("700"),
        expiry=date(2026, 9, 8),
    )
    candidate_event = replace(raw, kind=predicted)
    case = CorpusCase(
        raw=type("Raw", (), {"message_id": message_id})(),
        expected_kind=expected,
    )
    return PairRecord(
        case=case,  # type: ignore[arg-type]
        baseline=ParseDecision(candidate_event, DecisionEvidence("baseline", 0.99)),
        candidate=ParseDecision(candidate_event, DecisionEvidence("candidate", confidence)),
        expected_kind=expected,
    )


def _corpus(raw_factory) -> tuple[CorpusCase, ...]:
    texts: list[str] = []
    for index in range(30):
        texts.append(f"QQQ {700 + index}C TODAY @ 1.01")
    texts.extend(["All out"] * 15)
    texts.extend(["Watching QQQ around VWAP"] * 10)
    texts.extend(["Nice bounce +10%"] * 5)
    cases: list[CorpusCase] = []
    for index, text in enumerate(texts):
        raw = raw_factory(text, message_id=f"m{index}")
        parsed = parse_message_with_evidence(raw)
        cases.append(
            CorpusCase(
                raw=raw,
                expected_kind=parsed.event.kind,
                expected_contract_key=parsed.event.contract_key,
            )
        )
    return tuple(cases)


def _candidate(raw) -> ParseDecision:
    decision = parse_message_with_evidence(raw)
    evidence = replace(
        decision.evidence,
        confidence=0.99,
        rule_id=f"candidate.{decision.evidence.rule_id}",
    )
    return ParseDecision(decision.event, evidence)


def test_certification_of_a_faithful_candidate_passes(raw_factory):
    cases = _corpus(raw_factory)
    certificate = certify(
        cases,
        baseline=ParserSpec("baseline", parse_message_with_evidence),
        candidate=ParserSpec("candidate", _candidate),
        criteria=CertificationCriteria(min_adjudicated_samples=50),
    )
    payload = certificate.to_dict()
    assert certificate.certified is True
    assert payload["decision"] == "CERTIFIED"
    assert payload["metrics"]["candidate"]["accuracy"] == 1.0
    assert payload["metrics"]["action_escalations"] == 0
    assert payload["segments"]["regressions"] == []
    assert payload["semantic"]["state_changed"] is False
    assert payload["digest"]
    assert "CERTIFIED" in render(certificate)
    assert "segments" in render(certificate)


def test_candidate_is_refused_when_it_escalates_an_ignore(raw_factory):
    cases = _corpus(raw_factory)

    def escalating(raw) -> ParseDecision:
        decision = parse_message_with_evidence(raw)
        if raw.content.startswith("Watching"):
            event = replace(
                decision.event,
                kind=EventKind.ENTRY,
                ticker="QQQ",
                option_side="CALL",
                strike=Decimal("700"),
                expiry=date(2026, 9, 8),
            )
            return ParseDecision(
                event, replace(decision.evidence, confidence=0.99)
            )
        return _candidate(raw)

    certificate = certify(
        cases,
        baseline=ParserSpec("baseline", parse_message_with_evidence),
        candidate=ParserSpec("escalating", escalating),
        criteria=CertificationCriteria(min_adjudicated_samples=50),
    )
    assert certificate.certified is False
    assert any(reason.startswith("action_escalations") for reason in certificate.reasons)
    assert "REFUSED" in render(certificate)


def test_candidate_is_refused_for_poor_calibration(raw_factory):
    cases = _corpus(raw_factory)

    def overconfident(raw) -> ParseDecision:
        decision = parse_message_with_evidence(raw)
        return ParseDecision(
            decision.event,
            replace(decision.evidence, confidence=0.50, rule_id="candidate.low"),
        )

    certificate = certify(
        cases,
        baseline=ParserSpec("baseline", parse_message_with_evidence),
        candidate=ParserSpec("overconfident", overconfident),
        criteria=CertificationCriteria(
            min_adjudicated_samples=50,
            max_expected_calibration_error=0.05,
        ),
    )
    assert certificate.certified is False
    assert any(
        reason.startswith("expected_calibration_error") for reason in certificate.reasons
    )


def test_candidate_is_refused_for_too_few_adjudications(raw_factory):
    cases = _corpus(raw_factory)[:5]
    certificate = certify(
        cases,
        baseline=ParserSpec("baseline", parse_message_with_evidence),
        candidate=ParserSpec("candidate", _candidate),
        criteria=CertificationCriteria(min_adjudicated_samples=50),
    )
    assert certificate.certified is False
    assert any(
        reason.startswith("insufficient_adjudicated_samples") for reason in certificate.reasons
    )


def test_identical_parser_is_not_a_promotion(raw_factory):
    cases = _corpus(raw_factory)
    spec = ParserSpec("baseline", parse_message_with_evidence)
    certificate = certify(
        cases,
        baseline=spec,
        candidate=spec,
        criteria=CertificationCriteria(min_adjudicated_samples=50),
    )
    assert certificate.certified is False
    assert "candidate_is_baseline" in certificate.reasons


def test_mcnemar_exact_counts_discordant_pairs():
    result = mcnemar_exact([True, True, False, False], [True, False, True, False])
    assert result["baseline_only"] == 1
    assert result["candidate_only"] == 1
    assert 0.0 <= result["p_value"] <= 1.0
    no_discordance = mcnemar_exact([True, False], [True, False])
    assert no_discordance["p_value"] == 1.0


def test_risk_coverage_curve_and_selection():
    pairs = (
        _pair("a", 0.99, True),
        _pair("b", 0.98, True),
        _pair("c", 0.97, False),
        _pair("d", 0.96, True),
    )
    selected = risk_coverage(pairs, target_precision=0.70)
    assert selected.selected_coverage == 1.0
    assert selected.selected_precision == 0.75
    strict = risk_coverage(pairs, target_precision=0.90)
    assert strict.selected_coverage == 0.5
    assert strict.selected_precision == 1.0
    empty = risk_coverage((), target_precision=0.90)
    assert empty.selected_coverage is None


def test_calibration_report_and_bins():
    pair = _pair("a", 0.90, True)
    report = calibration_report((pair,), bins=10)
    assert report.expected_calibration_error == pytest.approx(0.10)
    assert report.brier_score == pytest.approx(0.01)
    assert report.bins[0].n == 1


def test_load_corpus_and_parser_spec(tmp_path):
    path = tmp_path / "corpus.jsonl"
    path.write_text(
        json.dumps(
            {
                "message_id": "m1",
                "content": "QQQ 719C TODAY @ 1.01",
                "expected_kind": "ENTRY",
            }
        )
        + "\n"
        + json.dumps({"id": "m2", "text": "All out"})
        + "\n",
        encoding="utf-8",
    )
    cases = load_corpus(path)
    assert [case.message_id for case in cases] == ["m1", "m2"]
    assert cases[0].expected_kind is EventKind.ENTRY
    assert cases[1].expected_kind is None
    spec = load_parser("scorpion.parser:parse_message_with_evidence")
    assert spec.name.endswith("parse_message_with_evidence")


def test_load_corpus_refuses_a_row_without_content(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"message_id":"m1"}\n', encoding="utf-8")
    with pytest.raises(CertificationError, match="no content"):
        load_corpus(path)


def test_certify_cli_refuses_an_identical_parser(tmp_path):
    path = tmp_path / "corpus.jsonl"
    path.write_text(
        json.dumps(
            {
                "message_id": "m1",
                "content": "QQQ 719C TODAY @ 1.01",
                "expected_kind": "ENTRY",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert certify_main(["--corpus", str(path), "--json"]) == 1


def test_golden_corpus_kind_key_is_an_expected_label():
    cases = load_corpus("tests/fixtures/mff_golden.json")
    assert len(cases) == 15
    assert cases[0].expected_kind is EventKind.ENTRY
    assert any(case.expected_kind is EventKind.AMBIGUOUS for case in cases)


def _permissive(**overrides):
    values = {
        "min_adjudicated_samples": 10,
        "min_accuracy": 0.0,
        "min_actionable_precision": 0.0,
        "max_ambiguity_rate": 1.0,
        "max_expected_calibration_error": 1.0,
        "min_risk_coverage": 0.0,
        "min_delta_accuracy": -1.0,
        "require_no_semantic_state_change": False,
        "require_no_effect_multiset_change": False,
    }
    values.update(overrides)
    return CertificationCriteria(**values)


def _channel_corpus(raw_factory, *, primary: int = 20, secondary: int = 20):
    cases: list[CorpusCase] = []
    for channel, count in (
        (CHANNELS["etf-options"], primary),
        (CHANNELS["options-king"], secondary),
    ):
        for index in range(count):
            raw = raw_factory(
                "QQQ 700C TODAY @ 1.01",
                message_id=f"{channel}-{index}",
                channel_id=channel,
            )
            parsed = parse_message_with_evidence(raw)
            cases.append(
                CorpusCase(
                    raw=raw,
                    expected_kind=parsed.event.kind,
                    expected_contract_key=parsed.event.contract_key,
                )
            )
    return tuple(cases)


def _breaks_one_channel(raw):
    decision = parse_message_with_evidence(raw)
    if raw.channel_id == CHANNELS["options-king"]:
        return ParseDecision(
            replace(decision.event, kind=EventKind.IGNORE),
            replace(decision.evidence, confidence=0.99, rule_id="candidate.channel_loss"),
        )
    return _candidate(raw)


def test_segment_report_names_the_channel_that_regressed(raw_factory):
    cases = _channel_corpus(raw_factory)
    pairs: list[PairRecord] = []
    for case in cases:
        baseline = parse_message_with_evidence(case.raw)
        if case.raw.channel_id == CHANNELS["options-king"]:
            candidate = ParseDecision(
                replace(baseline.event, kind=EventKind.IGNORE),
                baseline.evidence,
            )
        else:
            candidate = baseline
        pairs.append(
            PairRecord(
                case=case,
                baseline=baseline,
                candidate=candidate,
                expected_kind=case.expected_kind,
                expected_contract_key=case.expected_contract_key,
            )
        )
    report = segment_report(pairs, min_samples=10)
    channel = next(
        item
        for item in report.results
        if item.dimension == "channel"
        and item.segment == CHANNELS["options-king"]
    )
    assert channel.status == "regression"
    assert channel.delta_accuracy == -1.0
    assert channel.n == 20
    assert report.failures


def test_certification_refuses_a_segment_regression(raw_factory):
    cases = _channel_corpus(raw_factory)
    certificate = certify(
        cases,
        baseline=ParserSpec("baseline", parse_message_with_evidence),
        candidate=ParserSpec("channel-blind", _breaks_one_channel),
        criteria=_permissive(),
    )
    assert any(
        reason.startswith(f"segment_regression:channel:{CHANNELS['options-king']}:")
        for reason in certificate.reasons
    ), certificate.reasons
    assert certificate.payload["segments"]["regressions"]


def test_a_segment_below_the_floor_is_reported_not_used_to_refuse(raw_factory):
    cases = _channel_corpus(raw_factory, primary=20, secondary=5)
    certificate = certify(
        cases,
        baseline=ParserSpec("baseline", parse_message_with_evidence),
        candidate=ParserSpec("candidate", _candidate),
        criteria=_permissive(min_adjudicated_samples=20),
    )
    channel = next(
        item
        for item in certificate.payload["segments"]["results"]
        if item["dimension"] == "channel"
        and item["segment"] == CHANNELS["options-king"]
    )
    assert channel["status"] == "underpowered"
    assert certificate.certified is True
    assert not any(
        reason.startswith("segment_regression") for reason in certificate.reasons
    )


def test_segment_report_refuses_an_impossible_floor():
    with pytest.raises(CertificationError, match="bad_min_segment_samples"):
        segment_report((), min_samples=0)
    with pytest.raises(CertificationError, match="bad_max_segment_accuracy_drop"):
        segment_report((), max_accuracy_drop=1.5)


# ── a certificate is only useful if it can be read back before it is acted on ──
def _certified(raw_factory):
    return certify(
        _corpus(raw_factory),
        baseline=ParserSpec("baseline", parse_message_with_evidence),
        candidate=ParserSpec("candidate", _candidate),
        criteria=CertificationCriteria(min_adjudicated_samples=50),
        generated_at=BASE,
    )


def test_a_written_certificate_verifies_and_reports_its_age(raw_factory):
    certificate = _certified(raw_factory)
    result = verify_certificate(certificate, now=datetime(2026, 9, 9, 14, tzinfo=UTC))
    assert result.ok is True
    assert result.reasons == ()
    assert result.age_hours == pytest.approx(24.0)
    assert result.candidate == "candidate"


def test_an_edited_certificate_is_refused_by_the_digest(raw_factory):
    certificate = _certified(raw_factory)
    payload = json.loads(json.dumps(certificate.payload, default=str))
    payload["metrics"]["candidate"]["accuracy"] = 0.0
    tampered = replace(certificate, payload=payload)
    result = verify_certificate(tampered, now=datetime(2026, 9, 9, tzinfo=UTC))
    assert result.ok is False
    assert "digest_mismatch" in result.reasons


def test_a_stale_certificate_is_refused(raw_factory):
    certificate = _certified(raw_factory)
    result = verify_certificate(
        certificate,
        now=datetime(2026, 10, 1, tzinfo=UTC),
        max_age=timedelta(days=7),
    )
    assert result.ok is False
    assert any(reason.startswith("stale:") for reason in result.reasons)


def test_a_certificate_issued_for_another_pair_is_refused(raw_factory):
    certificate = _certified(raw_factory)
    result = verify_certificate(
        certificate,
        now=datetime(2026, 9, 9, tzinfo=UTC),
        expected_candidate="a.different.parser",
    )
    assert result.ok is False
    assert any(reason.startswith("candidate_mismatch:") for reason in result.reasons)


def test_a_refused_certificate_cannot_be_read_as_permission(raw_factory):
    certificate = certify(
        _corpus(raw_factory)[:5],
        baseline=ParserSpec("baseline", parse_message_with_evidence),
        candidate=ParserSpec("candidate", _candidate),
        criteria=CertificationCriteria(min_adjudicated_samples=50),
        generated_at=BASE,
    )
    result = verify_certificate(certificate, now=datetime(2026, 9, 9, tzinfo=UTC))
    assert result.ok is False
    assert "not_certified" in result.reasons


def test_a_future_dated_certificate_is_refused(raw_factory):
    certificate = _certified(raw_factory)
    result = verify_certificate(certificate, now=datetime(2026, 9, 1, tzinfo=UTC))
    assert result.ok is False
    assert any(reason.startswith("generated_utc:future:") for reason in result.reasons)


def test_a_naive_timestamp_is_not_a_certificate_time(raw_factory):
    certificate = _certified(raw_factory)
    payload = {**certificate.payload, "generated_utc": "2026-09-08T14:00:00"}
    result = verify_certificate(
        replace(certificate, payload=payload),
        now=datetime(2026, 9, 9, tzinfo=UTC),
    )
    assert "generated_utc:naive" in result.reasons


def test_load_certificate_round_trips_a_written_file(raw_factory, tmp_path):
    certificate = _certified(raw_factory)
    path = certificate.write(tmp_path / "cert.json")
    loaded = load_certificate(path)
    assert loaded.digest == certificate.digest
    assert loaded.decision == "CERTIFIED"
    result = verify_certificate(loaded, now=datetime(2026, 9, 9, tzinfo=UTC))
    assert result.ok is True


def test_load_certificate_refuses_a_missing_file(tmp_path):
    with pytest.raises(CertificationError, match="certificate_not_found"):
        load_certificate(tmp_path / "absent.json")


def test_cli_verify_passes_a_good_certificate_and_fails_a_tampered_one(
    raw_factory, tmp_path, capsys
):
    certificate = _certified(raw_factory)
    path = certificate.write(tmp_path / "cert.json")
    assert certify_main(["--verify", str(path), "--max-age-hours", "100000"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    written = json.loads(path.read_text(encoding="utf-8"))
    written["decision"] = "CERTIFIED"
    written["metrics"]["candidate"]["accuracy"] = 0.0
    path.write_text(json.dumps(written), encoding="utf-8")
    assert certify_main(["--verify", str(path), "--max-age-hours", "100000"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
