"""The parser pin: turn a certificate into an artifact another lane can check.

`certification.py` answers "may this parser be promoted?" with a signed-by-content
certificate. That is the right question for the semantic lane, and it is not enough for the
lanes downstream. A certificate is one JSON file among many; nothing forces the parser that
*runs* in a live loop to be the parser the certificate measured, and nothing notices when
the file that was certified is edited afterwards.

The direction document names this as the handoff: *"the certificate names the parser pair
and its digest; PR #8 pins the digest and refuses a parser without one."* This module is
that pin, and it is deliberately small, because an artifact another lane has to verify must
be cheap to verify:

    parser          module:function  (what will run, not what was described)
    parser_digest   sha256 of the module SOURCE FILE that will ship
    corpus_digest   sha256 of the corpus the certificate was measured on
    certificate     the certificate's content digest, its decision and its pair
    criteria_digest the criteria the certificate was issued under
    digest          the pin's own content digest

Why the module source, not the parser's name
--------------------------------------------
A parser's `name` is a label a caller chooses. A live lane that trusts a label is trusting
the caller. The digest is taken over the bytes of the module that will execute - line
endings normalised, because a CRLF checkout and an LF checkout are the same code and a
digest that reports otherwise is a false alarm that teaches people to ignore drift. The
same normalisation applies to the corpus.

What a pin refuses to be
------------------------
* It is **not** a signature. There is no key management here and the module does not pretend
  otherwise: `digest` is content identity. A reader that needs authority needs a signature
  scheme, and that is out of scope by design.
* It is **not a certificate**. A pin can only be built from a certificate that is
  `CERTIFIED` and whose `candidate` is the parser being pinned. Pinning a REFUSED
  certificate, or a different parser under a valid certificate, is the exact laundering this
  file exists to prevent.
* It is **not auto-refreshed**. `write` refuses to overwrite an existing pin without
  `--replace`, because silently moving a pin is silently changing which parser a live lane
  will trust.

Verification is total and named
-------------------------------
`verify_pin` re-derives every digest it can and returns every reason at once, in a stable
order: a pin that reports only the first disagreement hides the second, and it is usually
the second that explains the first. An unreadable pin, a foreign schema, a missing
certificate and an unchecked corpus are refusals, never "assume fine".
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .certification import (
    Certificate,
    CertificationError,
    load_certificate,
    verify_certificate,
)

PIN_SCHEMA = "scorpion.parser_pin/1"

#: The field order a human reads, and the order `verify_pin` reports reasons in.
REASON_ORDER: tuple[str, ...] = (
    "pin_unreadable",
    "pin_not_object",
    "pin_schema",
    "pin_malformed",
    "pin_corrupt",
    "parser_not_found",
    "module_unreadable",
    "module_drift",
    "corpus_unreadable",
    "corpus_drift",
    "certificate_unreadable",
    "certificate_changed",
    "certificate_unverified",
    "pin_stale",
)


class PinError(ValueError):
    """A pin that cannot be built or read, with a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


# ── digests ─────────────────────────────────────────────────────────────────
def source_digest(path: str | Path) -> str:
    """sha256 of a source file, normalised so a checkout's line endings are not drift.

    A CRLF checkout and an LF checkout are the same code. A digest that reported otherwise
    would produce a false drift on every Windows/Linux pair - and false drift is how a real
    one gets ignored.
    """
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise PinError("module_unreadable", f"{target}: {exc}") from None
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def corpus_digest(path: str | Path) -> str:
    """sha256 of the corpus file the certificate was measured on.

    Directories are digested by their sorted file names and contents, so a corpus split into
    files is pinned as one thing rather than as whichever file someone remembered.
    """
    target = Path(path)
    if target.is_dir():
        digest = hashlib.sha256()
        for child in sorted(p for p in target.rglob("*") if p.is_file()):
            digest.update(child.relative_to(target).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(child.read_bytes().replace(b"\r\n", b"\n"))
            digest.update(b"\0")
        return digest.hexdigest()
    return source_digest(target)


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def pin_digest(payload: Mapping[str, Any]) -> str:
    """The pin's own content digest, excluding `written_utc` and `digest`.

    Two pins that describe the same parser and the same evidence are the same claim even if
    they were written a minute apart, which is the same rule `certificate_digest` uses.
    """
    body = {key: value for key, value in payload.items()
            if key not in {"written_utc", "digest"}}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def _resolve_module_file(spec: str) -> Path:
    """The source file of the module a `module:function` spec names."""
    module_name, separator, _ = spec.partition(":")
    if not separator:
        module_name, _, _ = spec.rpartition(".")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise PinError("parser_not_found", f"{module_name}: {exc}") from None
    origin = getattr(module, "__file__", None)
    if not origin:
        raise PinError("module_unreadable", f"{module_name} has no source file")
    return Path(origin)


# ── the pin ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ParserPin:
    """One certified parser, named by the bytes that will run and the evidence that judged it."""

    parser: str
    parser_version: str
    parser_digest: str
    certificate_digest: str
    certificate_decision: str
    baseline: str
    candidate: str
    corpus_digest: str
    corpus_cases: int
    criteria_digest: str
    written_utc: str
    digest: str
    schema: str = PIN_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "parser_digest": self.parser_digest,
            "certificate": {
                "digest": self.certificate_digest,
                "decision": self.certificate_decision,
                "baseline": self.baseline,
                "candidate": self.candidate,
            },
            "corpus": {"digest": self.corpus_digest, "cases": self.corpus_cases},
            "criteria_digest": self.criteria_digest,
            "written_utc": self.written_utc,
            "digest": self.digest,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json() + "\n", encoding="utf-8")
        return target

    @property
    def age_hours(self) -> float | None:
        try:
            written = datetime.fromisoformat(self.written_utc)
        except ValueError:
            return None
        if written.tzinfo is None:
            return None
        return (datetime.now(UTC) - written).total_seconds() / 3600.0


def build_pin(*, certificate: Certificate, parser: str,
              corpus_path: str | Path, module_path: str | Path | None = None,
              parser_version: str = "", now: datetime | None = None) -> ParserPin:
    """Pin a certified parser. Refuses a refused certificate or a different parser.

    `parser` is the `module:function` spec that will run. `module_path` overrides the file
    whose bytes are digested, for a lane that ships a copy; the default resolves the file
    from the spec, so the pin describes the code that actually executes.
    """
    if not certificate.certified or certificate.decision != "CERTIFIED":
        raise PinError(
            "certificate_unverified",
            f"the certificate is {certificate.decision} "
            f"({', '.join(certificate.reasons) or 'no reasons'}); a pin is a promotion, "
            f"not a receipt")
    payload = certificate.payload
    candidate = str(payload.get("candidate", ""))
    if candidate and candidate != parser:
        # The laundering case: a valid certificate for parser A, pinned to parser B.
        raise PinError(
            "certificate_unverified",
            f"the certificate names {candidate!r}, not {parser!r}")

    module = Path(module_path) if module_path is not None else _resolve_module_file(parser)
    criteria = payload.get("criteria")
    corpus = payload.get("corpus")
    cases = 0
    if isinstance(corpus, Mapping):
        cases = int(corpus.get("cases", 0) or 0)
    module_digest = source_digest(module)
    corpus_hash = corpus_digest(corpus_path)
    criteria_hash = hashlib.sha256(_canonical(criteria).encode()).hexdigest()
    written = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    body: dict[str, Any] = {
        "schema": PIN_SCHEMA,
        "parser": parser,
        "parser_version": parser_version,
        "parser_digest": module_digest,
        "certificate": {
            "digest": certificate.digest,
            "decision": certificate.decision,
            "baseline": str(payload.get("baseline", "")),
            "candidate": candidate,
        },
        "corpus": {"digest": corpus_hash, "cases": cases},
        "criteria_digest": criteria_hash,
        "written_utc": written,
    }
    return ParserPin(
        parser=parser,
        parser_version=parser_version,
        parser_digest=module_digest,
        certificate_digest=certificate.digest,
        certificate_decision=certificate.decision,
        baseline=str(payload.get("baseline", "")),
        candidate=candidate,
        corpus_digest=corpus_hash,
        corpus_cases=cases,
        criteria_digest=criteria_hash,
        written_utc=written,
        digest=pin_digest(body),
    )


def load_pin(path: str | Path) -> ParserPin:
    """Read a pin without trusting a single field.

    The digest is kept as data and re-derived by `verify_pin`; this loader only enforces the
    shape needed to recompute it. A missing file is `pin_unreadable`, never "no pin": a lane
    that treats an absent pin as "nothing to check" has the gate inverted.
    """
    target = Path(path)
    try:
        raw: object = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PinError("pin_unreadable", f"{target} does not exist") from None
    except OSError as exc:
        raise PinError("pin_unreadable", f"{target}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise PinError("pin_unreadable", f"{target}: {exc}") from None
    if not isinstance(raw, Mapping):
        raise PinError("pin_not_object", str(target))
    if raw.get("schema") != PIN_SCHEMA:
        raise PinError("pin_schema", f"{target} declares {raw.get('schema')!r}")
    certificate = raw.get("certificate")
    corpus = raw.get("corpus")
    if not isinstance(certificate, Mapping) or not isinstance(corpus, Mapping):
        raise PinError("pin_malformed", f"{target} has no certificate/corpus block")
    required = ("parser", "parser_digest", "digest", "written_utc")
    missing = [name for name in required if not raw.get(name)]
    if missing:
        raise PinError("pin_malformed", f"{target} is missing {', '.join(missing)}")
    return ParserPin(
        parser=str(raw["parser"]),
        parser_version=str(raw.get("parser_version", "")),
        parser_digest=str(raw["parser_digest"]),
        certificate_digest=str(certificate.get("digest", "")),
        certificate_decision=str(certificate.get("decision", "")),
        baseline=str(certificate.get("baseline", "")),
        candidate=str(certificate.get("candidate", "")),
        corpus_digest=str(corpus.get("digest", "")),
        corpus_cases=int(corpus.get("cases", 0) or 0),
        criteria_digest=str(raw.get("criteria_digest", "")),
        written_utc=str(raw["written_utc"]),
        digest=str(raw["digest"]),
    )


# ── verification ────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PinVerification:
    """Every disagreement at once, in a stable order, with the digests that were compared."""

    ok: bool
    reasons: tuple[str, ...]
    pin: str
    parser: str
    parser_digest: str
    corpus_digest: str
    certificate_digest: str
    checked: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "pin": self.pin,
            "parser": self.parser,
            "parser_digest": self.parser_digest,
            "corpus_digest": self.corpus_digest,
            "certificate_digest": self.certificate_digest,
            "checked": dict(self.checked),
        }

    def to_text(self) -> str:
        lines = [f"pin  {self.pin}  {'OK' if self.ok else 'DRIFT'}  parser {self.parser}"]
        lines.append(f"  parser digest      {self.parser_digest[:32]}")
        lines.append(f"  corpus digest      {self.corpus_digest[:32]}")
        lines.append(f"  certificate digest {self.certificate_digest[:32]}")
        for reason in self.reasons:
            lines.append(f"  REFUSED            {reason}")
        return "\n".join(lines)


def _ordered(reasons: set[str]) -> tuple[str, ...]:
    known = [reason for reason in REASON_ORDER if reason in reasons]
    return tuple(known) + tuple(sorted(reasons - set(REASON_ORDER)))


def verify_pin(pin: ParserPin, *, corpus_path: str | Path,
               certificate_path: str | Path,
               module_path: str | Path | None = None,
               now: datetime | None = None,
               max_age_hours: float | None = None,
               max_certificate_age: timedelta = timedelta(hours=168)) -> PinVerification:
    """Re-derive every digest and report every disagreement.

    Nothing is optional except the module override. A verifier that can skip the corpus, or
    the certificate, is a verifier that can be made to pass by omission - so `corpus_path`
    and `certificate_path` are required arguments, and an unreadable one is a reason, not an
    exception that a caller might catch and ignore.
    """
    reasons: set[str] = set()
    checked: dict[str, Any] = {}

    # 1. the pin's own content
    expected_pin = pin_digest(pin.to_dict())
    checked["pin_digest_expected"] = expected_pin
    if expected_pin != pin.digest:
        reasons.add("pin_corrupt")

    # 2. the module that will run
    try:
        module = Path(module_path) if module_path is not None else _resolve_module_file(
            pin.parser)
        actual_module = source_digest(module)
    except PinError as exc:
        reasons.add(exc.code)
        actual_module = ""
    checked["module_digest"] = actual_module
    if actual_module and actual_module != pin.parser_digest:
        reasons.add("module_drift")

    # 3. the corpus the certificate was measured on
    try:
        actual_corpus = corpus_digest(corpus_path)
    except (PinError, OSError):
        reasons.add("corpus_unreadable")
        actual_corpus = ""
    checked["corpus_digest"] = actual_corpus
    if actual_corpus and actual_corpus != pin.corpus_digest:
        reasons.add("corpus_drift")

    # 4. the certificate: still the same claim, still certified, still current enough
    try:
        certificate = load_certificate(certificate_path)
    except CertificationError:
        reasons.add("certificate_unreadable")
        certificate = None
    if certificate is not None:
        checked["certificate_digest"] = certificate.digest
        if certificate.digest != pin.certificate_digest:
            reasons.add("certificate_changed")
        verification = verify_certificate(
            certificate,
            now=now,
            max_age=max_certificate_age,
            expected_candidate=pin.candidate or None,
            expected_baseline=pin.baseline or None,
        )
        checked["certificate_ok"] = verification.ok
        if not verification.ok:
            reasons.add("certificate_unverified")

    # 5. the pin's own freshness, when the caller asks for it
    if max_age_hours is not None:
        age = pin.age_hours
        checked["age_hours"] = None if age is None else round(age, 3)
        if age is None or age > max_age_hours:
            reasons.add("pin_stale")

    return PinVerification(
        ok=not reasons,
        reasons=_ordered(reasons),
        pin=pin.digest,
        parser=pin.parser,
        parser_digest=pin.parser_digest,
        corpus_digest=pin.corpus_digest,
        certificate_digest=pin.certificate_digest,
        checked=checked,
    )


def render(pin: ParserPin) -> str:
    """A short human view. The JSON is the artifact; this is for the operator."""
    age = pin.age_hours
    age_text = f"{age:.1f}h old" if age is not None else "undated"
    lines = [
        f"parser pin  {pin.parser}  ({pin.parser_version or 'unversioned'})  {age_text}",
        f"  module digest      {pin.parser_digest[:32]}",
        f"  corpus             {pin.corpus_digest[:32]}  ({pin.corpus_cases} cases)",
        f"  certificate        {pin.certificate_digest[:32]}  "
        f"[{pin.certificate_decision}] {pin.baseline} -> {pin.candidate}",
        f"  criteria digest    {pin.criteria_digest[:32]}",
        f"  pin digest         {pin.digest[:32]}",
    ]
    return "\n".join(lines)


# ── the operator surface ────────────────────────────────────────────────────
def pin_main(argv: list[str] | None = None) -> int:
    """`scorpion-pin write|verify`: the semantic lane's handoff artifact.

    Exit codes are the contract another lane codes against: `write` returns 0 on a pin and 2
    on any refusal (a refused certificate, a different parser, an existing pin without
    `--replace`); `verify` returns 0 clean, 1 on ANY drift reason, and 2 when the arguments
    themselves cannot be honoured. A reader that treats 2 as 1 is still safe: neither is 0.
    """
    parser = argparse.ArgumentParser(
        prog="scorpion-pin",
        description="Pin a certified parser to the bytes that will run, and verify the pin.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    write = sub.add_parser("write", help="build a pin from a CERTIFIED certificate")
    write.add_argument("--certificate", required=True,
                       help="the certificate JSON to promote")
    write.add_argument("--parser", required=True,
                       help="the parser that will run, as module:function")
    write.add_argument("--corpus", required=True,
                       help="the corpus the certificate was measured on (file or directory)")
    write.add_argument("--module", default="",
                       help="override the source file whose bytes are digested "
                            "(default: the module the --parser spec names)")
    write.add_argument("--out", default="live/PARSER.pin.json")
    write.add_argument("--parser-version", default="")
    write.add_argument("--replace", action="store_true",
                       help="overwrite an existing pin (moving a pin is a promotion)")
    write.add_argument("--json", action="store_true")

    verify = sub.add_parser("verify", help="re-derive every digest and name every drift")
    verify.add_argument("--pin", default="live/PARSER.pin.json")
    verify.add_argument("--corpus", required=True)
    verify.add_argument("--certificate", required=True)
    verify.add_argument("--module", default="")
    verify.add_argument("--max-age-hours", type=float, default=None,
                        help="refuse a pin older than this (default: no age limit)")
    verify.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "write":
        return _pin_write(args)
    return _pin_verify(args)


def _pin_write(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists() and not args.replace:
        print(f"refusing to overwrite {out}: moving a pin changes which parser downstream "
              f"lanes trust (pass --replace to promote)", file=sys.stderr)
        return 2
    try:
        certificate = load_certificate(args.certificate)
        pin = build_pin(
            certificate=certificate,
            parser=args.parser,
            corpus_path=args.corpus,
            module_path=args.module or None,
            parser_version=args.parser_version,
        )
    except (CertificationError, PinError) as exc:
        code = getattr(exc, "code", "pin_refused")
        print(f"refusing to pin: {code}: {exc}", file=sys.stderr)
        return 2
    pin.write(out)
    if args.json:
        print(json.dumps(pin.to_dict(), indent=2, sort_keys=True))
    else:
        print(render(pin))
        print(f"  written            {out}")
    return 0


def _pin_verify(args: argparse.Namespace) -> int:
    try:
        pin = load_pin(args.pin)
    except PinError as exc:
        print(f"pin refused: {exc.code}: {exc.detail}", file=sys.stderr)
        return 2
    verification = verify_pin(
        pin,
        corpus_path=args.corpus,
        certificate_path=args.certificate,
        module_path=args.module or None,
        max_age_hours=args.max_age_hours,
    )
    if args.json:
        print(json.dumps(verification.to_dict(), indent=2, sort_keys=True))
    else:
        print(verification.to_text())
    return 0 if verification.ok else 1
