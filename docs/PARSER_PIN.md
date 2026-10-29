# The parser pin: a certificate another lane can check

**Status 2026-10-29: built.** `scorpion-pin write` / `scorpion-pin verify`, 24 tests.

`certification.py` answers the semantic lane's question: *may this parser be promoted?* It
answers it well - paired baseline/candidate comparison, actionable precision, ambiguity,
exact McNemar, calibration, risk-coverage, action escalations, downstream state and effect
multisets, all digested into a `CERTIFIED` or `REFUSED` certificate.

That is not enough for the lanes downstream. A certificate is one JSON file among many.
Nothing forces the parser that **runs** to be the parser the certificate **measured**, and
nothing notices when the certified file is edited afterwards. `docs/fleet/DIRECTION.md`
names the missing seam:

> the certificate names the parser pair and its digest; PR #8 pins the digest and refuses a
> parser without one.

This module is that pin.

## What a pin says

```json
{
  "schema": "scorpion.parser_pin/1",
  "parser": "scorpion.parser:parse_message",
  "parser_version": "v1",
  "parser_digest": "sha256 of the module source file that will run",
  "certificate": {
    "digest": "the certificate's content digest",
    "decision": "CERTIFIED",
    "baseline": "scorpion.parser:parse_message_with_evidence",
    "candidate": "scorpion.parser:parse_message"
  },
  "corpus": {"digest": "sha256 of the corpus the certificate was measured on", "cases": 107},
  "criteria_digest": "sha256 of the criteria the certificate was issued under",
  "written_utc": "2026-10-29T...",
  "digest": "the pin's own content digest"
}
```

Every field is checkable. `parser_digest` is over **bytes**, not a label: a parser's `name` is
something a caller chooses, so a lane that trusts a label is trusting the caller. Line endings
are normalised (`\r\n` -> `\n`) before hashing, because a CRLF checkout and an LF checkout are
the same code and a digest that says otherwise produces false drift - and false drift is how a
real one gets ignored. The same normalisation applies to a corpus directory, which is digested
as its sorted relative names and contents so a split corpus is pinned as one thing.

## What `write` refuses

| Situation | Code | Why |
|---|---|---|
| the certificate is `REFUSED` (or carries reasons) | `certificate_unverified` | a pin is a promotion, not a receipt |
| the certificate's `candidate` is not the parser being pinned | `certificate_unverified` | a valid certificate for A pinned to B is laundering |
| the parser module cannot be imported | `parser_not_found` | the pin describes code that will run, not a name |
| an existing pin, without `--replace` | exit 2 | moving a pin changes which parser the live lane trusts |

## What `verify` re-derives, and how it reports

`verify_pin` recomputes every digest it can and returns **every** reason at once, in a
documented order. Reporting only the first disagreement hides the second, and it is usually
the second that explains the first.

```text
pin  <digest>  DRIFT  parser scorpion.parser:parse_message
  parser digest      a1b2c3...
  corpus digest      9f8e7d...
  certificate digest 4c5b6a...
  REFUSED            module_drift
  REFUSED            corpus_drift
```

| Reason | Meaning | What to do |
|---|---|---|
| `pin_corrupt` | the pin's content no longer hashes to its own digest | re-write the pin from the certificate |
| `module_drift` | the module that will run is not the module that was pinned | re-certify, or restore the file |
| `corpus_drift` | the corpus is not the one the certificate measured | re-run certification on the corpus you have |
| `certificate_changed` | a different certificate is at the path | point at the certificate the pin named |
| `certificate_unverified` | the certificate no longer verifies (refused, stale, other pair, parsed errors) | read the certificate's own reasons |
| `pin_stale` | older than `--max-age-hours` | re-certify and re-pin |
| `corpus_unreadable`, `certificate_unreadable`, `module_unreadable` | the evidence is not there | restore it - an unreadable input is never "assume fine" |

`corpus_path` and `certificate_path` are **required arguments**. A verifier that can skip the
corpus, or the certificate, is a verifier that can be made to pass by omission.

## Exit codes (the contract a downstream lane codes against)

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `scorpion-pin write` | pin written | - | refused (certificate, parser, existing pin) |
| `scorpion-pin verify` | every digest matches | at least one drift reason | the pin itself cannot be read |

A caller that treats 2 as 1 is still safe: neither is 0.

## Usage

```bash
# after `scorpion-certify` writes a CERTIFIED certificate
scorpion-pin write \
  --certificate reports/certificate.json \
  --parser scorpion.parser:parse_message \
  --corpus mff-backtest/trades.csv \
  --parser-version v1 \
  --out live/PARSER.pin.json

scorpion-pin verify \
  --pin live/PARSER.pin.json \
  --corpus mff-backtest/trades.csv \
  --certificate reports/certificate.json \
  --max-age-hours 168
```

A downstream lane consumes it like this, without importing `scorpion`:

1. read `live/PARSER.pin.json`;
2. hash the parser file it actually ships and compare with `parser_digest`;
3. hash its copy of the corpus and compare with `corpus.digest`;
4. fail closed - with the pin's digest in the error row - when either differs.

The live lane's parser is not this lane's parser, so its acceptance test pins *this digest*
as the certificate of the parser it runs; the point is that the artifact travels, not the
branch.

## What this is not

- **Not a signature.** There is no key management, and the file says so. `digest` is content
  identity; a reader that needs authority needs a signing scheme, which is out of scope.
- **Not an auto-refresh.** A pin moves only by an explicit `write --replace`.
- **Not a substitute for the certificate.** A pin without a readable certified certificate
  fails verification (`certificate_unreadable` / `certificate_unverified`).

## Acceptance

```bash
.venv/Scripts/python -m pytest tests/test_parser_pin.py -q    # 24 passed
```

The suite covers the laundering refusal, a refused certificate, module/corpus/pin/certificate
drift (and that all are reported together), missing evidence, staleness, CRLF-vs-LF identity,
directory hashing, the three loader refusals, and the CLI's 0/1/2 contract.
