"""The parser pin: a certified parser named by the bytes that will run.

The properties that make this artifact worth another lane trusting:

* a pin can only be built from a **CERTIFIED** certificate, for **that** parser - a valid
  certificate for parser A pinned to parser B is the laundering this file exists to prevent;
* verification re-derives every digest and reports **every** disagreement at once, not the
  first, because the second usually explains the first;
* line endings are not drift - a CRLF checkout and an LF checkout are the same code, and false
  drift trains people to ignore real drift;
* a missing pin, a missing corpus and a missing certificate are refusals, never "assume fine";
* the exit codes are the contract (`write` 0/2, `verify` 0/1/2), because a downstream lane codes
  against them.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import scorpion.parser as parser_module
from scorpion.certification import Certificate, certificate_digest
from scorpion.parser_pin import (
    PIN_SCHEMA,
    PinError,
    build_pin,
    corpus_digest,
    load_pin,
    pin_digest,
    pin_main,
    source_digest,
    verify_pin,
)

SPEC = "scorpion.parser:parse_message"
REPO = Path(__file__).resolve().parents[1]
MODULE = Path(parser_module.__file__)
CORPUS = REPO / "tests" / "fixtures" / "mff_golden.json"


def certificate(*, decision: str = "CERTIFIED", candidate: str = SPEC,
                reasons: list[str] | None = None,
                cases: int = 12, adjudicated: int = 8,
                generated: datetime | None = None) -> Certificate:
    payload = {
        "schema": "scorpion.certification/1",
        "generated_utc": (generated or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "decision": decision,
        "reasons": list(reasons or []),
        "baseline": "scorpion.parser:parse_message_with_evidence",
        "candidate": candidate,
        "corpus": {"cases": cases, "paired": cases, "adjudicated": adjudicated,
                   "unlabelled": cases - adjudicated, "errors": []},
        "criteria": {"min_accuracy": 0.95, "min_segment_samples": 10},
        "metrics": {"candidate": {"accuracy": 1.0}},
        "segments": {"regressions": []},
    }
    return Certificate(
        decision=decision,
        certified=decision == "CERTIFIED" and not payload["reasons"],
        reasons=tuple(str(item) for item in payload["reasons"]),
        digest=certificate_digest(payload),
        payload=payload,
    )


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def fixture(tmp_path: Path, **cert_kwargs: object) -> tuple[Path, Path, Path]:
    """A certificate file, a module copy and a corpus file, all consistent."""
    cert = certificate(**cert_kwargs)           # type: ignore[arg-type]
    cert_path = cert.write(tmp_path / "certificate.json")
    module_path = write(tmp_path / "parser_snapshot.py", MODULE.read_text(encoding="utf-8"))
    corpus_path = write(tmp_path / "corpus.json", CORPUS.read_text(encoding="utf-8"))
    return cert_path, module_path, corpus_path


# ── building a pin ──────────────────────────────────────────────────────────
def test_a_certified_parser_pins_to_the_module_that_will_run(tmp_path):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    from scorpion.certification import load_certificate

    pin = build_pin(certificate=load_certificate(cert_path), parser=SPEC,
                    corpus_path=corpus_path, module_path=module_path)

    assert pin.parser == SPEC
    assert pin.candidate == SPEC
    assert pin.certificate_decision == "CERTIFIED"
    assert pin.parser_digest == source_digest(module_path)
    assert pin.corpus_digest == source_digest(corpus_path)
    assert pin.corpus_cases == 12
    assert pin.schema == PIN_SCHEMA
    assert pin.digest == pin_digest(pin.to_dict())
    assert pin.age_hours == pytest.approx(0.0, abs=0.1)


def test_a_refused_certificate_cannot_be_pinned(tmp_path):
    cert_path, module_path, corpus_path = fixture(
        tmp_path, decision="REFUSED", reasons=["accuracy:0.90<0.95"])
    from scorpion.certification import load_certificate

    with pytest.raises(PinError) as error:
        build_pin(certificate=load_certificate(cert_path), parser=SPEC,
                  corpus_path=corpus_path, module_path=module_path)
    assert error.value.code == "certificate_unverified"
    assert "not a receipt" in error.value.detail


def test_a_certificate_for_another_parser_cannot_be_laundered(tmp_path):
    """The exact attack this file exists to stop: a valid cert for A, pinned to B."""
    cert_path, module_path, corpus_path = fixture(tmp_path)
    from scorpion.certification import load_certificate

    with pytest.raises(PinError) as error:
        build_pin(certificate=load_certificate(cert_path),
                  parser="scorpion.parser:parse_message_with_evidence",
                  corpus_path=corpus_path, module_path=module_path)
    assert error.value.code == "certificate_unverified"
    assert "not" in error.value.detail


def test_an_unknown_parser_spec_is_refused_when_the_module_cannot_be_found(tmp_path):
    certificate_file = certificate(
        candidate="scorpion.no_such_module:parse").write(tmp_path / "c.json")
    corpus_path = write(tmp_path / "corpus.json", "[]")
    from scorpion.certification import load_certificate

    with pytest.raises(PinError) as error:
        build_pin(certificate=load_certificate(certificate_file),
                  parser="scorpion.no_such_module:parse", corpus_path=corpus_path)
    assert error.value.code == "parser_not_found"


# ── verification: every reason, at once ─────────────────────────────────────
def test_a_clean_pin_verifies(tmp_path):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    from scorpion.certification import load_certificate

    pin = build_pin(certificate=load_certificate(cert_path), parser=SPEC,
                    corpus_path=corpus_path, module_path=module_path)
    result = verify_pin(pin, corpus_path=corpus_path, certificate_path=cert_path,
                        module_path=module_path)

    assert result.ok and result.reasons == ()
    assert result.checked["module_digest"] == pin.parser_digest
    assert result.checked["certificate_ok"] is True


def test_module_drift_is_named(tmp_path):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    from scorpion.certification import load_certificate

    pin = build_pin(certificate=load_certificate(cert_path), parser=SPEC,
                    corpus_path=corpus_path, module_path=module_path)
    write(module_path, module_path.read_text(encoding="utf-8") + "\n# edited after the pin\n")

    result = verify_pin(pin, corpus_path=corpus_path, certificate_path=cert_path,
                        module_path=module_path)
    assert not result.ok
    assert "module_drift" in result.reasons
    assert "DRIFT" in result.to_text()


def test_corpus_drift_is_named(tmp_path):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    from scorpion.certification import load_certificate

    pin = build_pin(certificate=load_certificate(cert_path), parser=SPEC,
                    corpus_path=corpus_path, module_path=module_path)
    corpus_path.write_text('{"changed": true}\n', encoding="utf-8")

    result = verify_pin(pin, corpus_path=corpus_path, certificate_path=cert_path,
                        module_path=module_path)
    assert "corpus_drift" in result.reasons


def test_every_disagreement_is_reported_not_just_the_first(tmp_path):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    from scorpion.certification import load_certificate

    pin = build_pin(certificate=load_certificate(cert_path), parser=SPEC,
                    corpus_path=corpus_path, module_path=module_path)
    write(module_path, module_path.read_text(encoding="utf-8") + "\n# drift\n")
    corpus_path.write_text('{"also": "changed"}\n', encoding="utf-8")

    result = verify_pin(pin, corpus_path=corpus_path, certificate_path=cert_path,
                        module_path=module_path)
    assert "module_drift" in result.reasons and "corpus_drift" in result.reasons
    # the reported order is the documented order
    assert result.reasons.index("module_drift") < result.reasons.index("corpus_drift")


def test_a_changed_certificate_is_named(tmp_path):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    from scorpion.certification import load_certificate

    pin = build_pin(certificate=load_certificate(cert_path), parser=SPEC,
                    corpus_path=corpus_path, module_path=module_path)
    # A different, still internally consistent certificate for the same pair.
    other = certificate(cases=13).write(cert_path)

    result = verify_pin(pin, corpus_path=corpus_path, certificate_path=other,
                        module_path=module_path)
    assert "certificate_changed" in result.reasons


def test_a_missing_corpus_is_a_reason_not_an_exception(tmp_path):
    cert_path, module_path, _ = fixture(tmp_path)
    from scorpion.certification import load_certificate

    pin = build_pin(certificate=load_certificate(cert_path), parser=SPEC,
                    corpus_path=CORPUS, module_path=module_path)
    result = verify_pin(pin, corpus_path=tmp_path / "gone.json",
                        certificate_path=cert_path, module_path=module_path)
    assert "corpus_unreadable" in result.reasons


def test_a_missing_certificate_is_a_reason(tmp_path):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    from scorpion.certification import load_certificate

    pin = build_pin(certificate=load_certificate(cert_path), parser=SPEC,
                    corpus_path=corpus_path, module_path=module_path)
    result = verify_pin(pin, corpus_path=corpus_path,
                        certificate_path=tmp_path / "no-certificate.json",
                        module_path=module_path)
    assert "certificate_unreadable" in result.reasons


def test_a_tampered_pin_is_pin_corrupt(tmp_path):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    from scorpion.certification import load_certificate

    pin = build_pin(certificate=load_certificate(cert_path), parser=SPEC,
                    corpus_path=corpus_path, module_path=module_path)
    tampered = replace(pin, corpus_cases=999)

    result = verify_pin(tampered, corpus_path=corpus_path, certificate_path=cert_path,
                        module_path=module_path)
    assert "pin_corrupt" in result.reasons


def test_a_stale_pin_can_be_refused_when_the_caller_asks(tmp_path):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    from scorpion.certification import load_certificate

    old = datetime.now(UTC) - timedelta(days=30)
    cert = certificate(generated=old).write(cert_path)
    pin = build_pin(certificate=load_certificate(cert), parser=SPEC,
                    corpus_path=corpus_path, module_path=module_path, now=old)
    result = verify_pin(pin, corpus_path=corpus_path, certificate_path=cert_path,
                        module_path=module_path, max_age_hours=24)
    assert "pin_stale" in result.reasons
    # the certificate is stale too, and that is a second reason rather than a silent pass
    assert "certificate_unverified" in result.reasons


# ── bytes and checkouts ─────────────────────────────────────────────────────
def test_line_endings_do_not_count_as_drift(tmp_path):
    lf = tmp_path / "lf.py"
    lf.write_bytes(b"a = 1\nb = 2\n")
    crlf = tmp_path / "crlf.py"
    crlf.write_bytes(b"a = 1\r\nb = 2\r\n")
    assert source_digest(lf) == source_digest(crlf)


def test_a_corpus_directory_is_digested_as_one_thing(tmp_path):
    (tmp_path / "one.csv").write_text("a,b\n", encoding="utf-8")
    (tmp_path / "two.csv").write_text("c,d\n", encoding="utf-8")
    first = corpus_digest(tmp_path)
    (tmp_path / "two.csv").write_text("c,e\n", encoding="utf-8")
    assert corpus_digest(tmp_path) != first


def test_the_same_pin_content_gives_the_same_digest_regardless_of_write_time():
    base = {"schema": PIN_SCHEMA, "parser": SPEC, "parser_digest": "a" * 64,
            "written_utc": "2026-09-08T14:00:00+00:00"}
    later = {**base, "written_utc": "2026-09-09T14:00:00+00:00"}
    assert pin_digest(base) == pin_digest(later)


# ── loading ─────────────────────────────────────────────────────────────────
def test_a_missing_pin_is_refused_not_an_empty_pin(tmp_path):
    with pytest.raises(PinError) as error:
        load_pin(tmp_path / "missing.pin.json")
    assert error.value.code == "pin_unreadable"


def test_a_foreign_schema_is_refused(tmp_path):
    path = tmp_path / "pin.json"
    path.write_text(json.dumps({"schema": "other/1"}), encoding="utf-8")
    with pytest.raises(PinError) as error:
        load_pin(path)
    assert error.value.code == "pin_schema"


def test_a_pin_without_a_certificate_block_is_refused(tmp_path):
    path = tmp_path / "pin.json"
    path.write_text(json.dumps({"schema": PIN_SCHEMA, "parser": SPEC}), encoding="utf-8")
    with pytest.raises(PinError) as error:
        load_pin(path)
    assert error.value.code == "pin_malformed"


# ── the CLI contract ────────────────────────────────────────────────────────
def test_pin_write_then_verify_is_exit_zero(tmp_path, capsys):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    out = tmp_path / "PARSER.pin.json"

    code = pin_main(["write", "--certificate", str(cert_path), "--parser", SPEC,
                     "--corpus", str(corpus_path), "--module", str(module_path),
                     "--out", str(out)])
    assert code == 0 and out.exists()
    assert "parser pin" in capsys.readouterr().out

    code = pin_main(["verify", "--pin", str(out), "--corpus", str(corpus_path),
                     "--certificate", str(cert_path), "--module", str(module_path)])
    assert code == 0
    assert "OK" in capsys.readouterr().out


def test_pin_write_refuses_to_overwrite_without_replace(tmp_path, capsys):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    out = tmp_path / "PARSER.pin.json"
    build_pin_args = ["write", "--certificate", str(cert_path), "--parser", SPEC,
                      "--corpus", str(corpus_path), "--module", str(module_path),
                      "--out", str(out)]
    assert pin_main(build_pin_args) == 0
    capsys.readouterr()

    assert pin_main(build_pin_args) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert pin_main([*build_pin_args, "--replace"]) == 0
    capsys.readouterr()


def test_pin_write_refuses_a_refused_certificate(tmp_path, capsys):
    cert_path, module_path, corpus_path = fixture(
        tmp_path, decision="REFUSED", reasons=["accuracy_regression"])
    code = pin_main(["write", "--certificate", str(cert_path), "--parser", SPEC,
                     "--corpus", str(corpus_path), "--module", str(module_path),
                     "--out", str(tmp_path / "pin.json")])
    assert code == 2
    assert "refusing to pin" in capsys.readouterr().err


def test_pin_verify_exits_one_on_drift(tmp_path, capsys):
    cert_path, module_path, corpus_path = fixture(tmp_path)
    out = tmp_path / "PARSER.pin.json"
    pin_main(["write", "--certificate", str(cert_path), "--parser", SPEC,
              "--corpus", str(corpus_path), "--module", str(module_path),
              "--out", str(out)])
    capsys.readouterr()
    write(module_path, module_path.read_text(encoding="utf-8") + "\n# drift\n")

    code = pin_main(["verify", "--pin", str(out), "--corpus", str(corpus_path),
                     "--certificate", str(cert_path), "--module", str(module_path),
                     "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert "module_drift" in payload["reasons"]


def test_pin_verify_exits_two_when_the_pin_cannot_be_read(tmp_path, capsys):
    code = pin_main(["verify", "--pin", str(tmp_path / "gone.json"),
                     "--corpus", str(CORPUS),
                     "--certificate", str(tmp_path / "gone-cert.json")])
    assert code == 2
    assert "pin refused" in capsys.readouterr().err
