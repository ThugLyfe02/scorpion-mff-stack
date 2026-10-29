# Semantic control plane: review queue and parser certification

The calibrated semantic layer could compute evidence. It could not turn that evidence into work,
and it could not turn a review into a promotion decision. This layer closes both loops without
giving either one execution authority.

```text
raw message -> parser evidence -> semantic envelope
                                   |
                                   +--> ReviewQueue (durable, prioritized, SLA)
                                   |       claim -> resolve -> adjudication
                                   |       adjudication -> calibration
                                   |
                                   +--> certify(candidate parser)
                                           corpus + adjudications
                                           accuracy / precision / ambiguity
                                           ECE / Brier / risk-coverage
                                           McNemar / action escalations
                                           semantic state + effect multiset
                                           -> CERTIFIED or REFUSED (digest)
```

Nothing here mutates the book, creates a proposed effect, approves an effect, sizes a position or
submits an order. The queue changes what a human sees. The certificate changes what a human is
willing to promote. The money path remains deterministic and review-only.

## The review queue

`scorpion.review.ReviewQueue` is a durable worklist over `review_items`:

* one item per event, deterministic id (`sha256("scorpion.review/1|" + event_id)`);
* priority from the existing `active_learning` scorer, so parser uncertainty, association
  uncertainty, novelty, ensemble entropy, actionable disagreement, uncalibrated rules, contract
  and quote findings all keep their weights;
* the item stores the parser evidence, association confidence, envelope and a small evidence
  digest, so a reviewer is not asked to re-derive why the item exists;
* `OPEN -> CLAIMED -> RESOLVED` (or `DISMISSED`) are explicit transitions in one SQLite writer
  transaction; `resolve` writes the adjudication in the same transaction;
* a resolved or dismissed item is never silently reopened by a later enqueue. A Discord edit
  arrives with a new revision id and therefore a new event id, which is the correct reopen path;
* `backfill` triages stored, unadjudicated signals using only the decision audit and adjudications
  that already exist. It never invents a rule, confidence or label for an event with no audit;
* `metrics` reports open count by tier and reason, age, SLA breaches, resolution rate and
  per-rule calibration progress.

Run it:

```bash
scorpion-review --db scorpion.db backfill
scorpion-review --db scorpion.db list --tier URGENT
scorpion-review --db scorpion.db claim <review-id> --assignee ryan
scorpion-review --db scorpion.db resolve <review-id> --kind ENTRY \
  --contract QQQ|CALL|719|2026-09-08 --reviewer ryan --note "exact parse"
scorpion-review --db scorpion.db metrics
```

Real output from a two-message store:

```text
tier     score status   event      reasons
LOW        5.0 OPEN     b5b1990346 uncalibrated_rule
LOW       14.0 OPEN     6e1c187070 low_parser_confidence

items 2  open 2  resolved 0  dismissed 0  resolution 0.0%
oldest open 0s  SLA breaches 0  adjudications 0
by tier LOW 2
by reason low_parser_confidence 1, uncalibrated_rule 1
```

The low tier is deliberate: an uncalibrated but syntactically confident entry is not an emergency,
it is a missing-evidence item. A low-confidence ambiguous post scores higher because it is more
likely to be wrong *and* less likely to be safe to ignore.

## Parser certification

`scorpion.certification.certify` compares a baseline parser with a candidate on a named corpus and
a named set of adjudications. A promotion is `CERTIFIED` only when every gate passes. A gate that
cannot be evaluated refuses.

| Gate | Default | Why it exists |
|---|---|---|
| adjudicated samples | ≥ 50 | a promotion on ten labels is a story |
| candidate accuracy | ≥ 0.95 | the baseline is not allowed to be better |
| actionable precision | ≥ 0.995 | a false action costs money; a missed action costs an opportunity |
| ambiguity rate | ≤ 0.35 | "ambiguous" is honest, but a candidate that hides behind it is not an improvement |
| action escalations | 0 | turning IGNORE/AMBIGUOUS into an ACTION is the failure that costs money |
| expected calibration error | ≤ 0.05 | high confidence is not the same as calibrated confidence |
| risk coverage at precision | ≥ 0.80 | the candidate must still cover most of the corpus at the precision the desk requires |
| accuracy regression vs baseline | ≥ −0.01 | a small regression is still a regression |
| segment parity | no drop on any channel or rule with ≥ 10 labels | an aggregate gain cannot be bought by sacrificing the channel or rule a reviewer was protecting |
| semantic state change | none | a candidate cannot look good on labels while changing the book |
| effect multiset change | none | a second identical trim remains visible rather than disappearing through set deduplication |
| distinct parser | required | certifying the baseline against itself is not evidence |

The certificate also reports the exact McNemar test over paired outcomes, Brier decomposition
(reliability, resolution, uncertainty), the risk-coverage curve, the parser diffs, and a digest.
The digest is content identity over the evidence, not a signature; the system has no key
management and does not pretend otherwise.

### A real certified run

The test suite builds a 60-case corpus (30 entries, 15 exits, 10 ignores, 5 ambiguous) and a
faithful candidate wrapper that states 0.99 confidence. The certificate is:

```text
CERTIFIED
corpus cases 60  paired 60  adjudicated 60
candidate accuracy 1.0000  actionable precision 1.0000  ambiguity 0.0833
ECE 0.0100  Brier 0.0001  risk coverage 1.0
action escalations 0  contract changes 0
semantic state changed false  effect multiset changed false
```

The same harness refuses a candidate that escalates one IGNORE to ENTRY:

```text
REFUSED
reasons action_escalations:1>0, accuracy:..., semantic_state_changed, ...
```

and refuses a candidate that states 0.50 confidence for every parse:

```text
REFUSED
reasons expected_calibration_error:0.4900>0.0500
```

Run it:

```bash
scorpion-certify --corpus tests/fixtures/mff_golden.json --json
scorpion-certify --corpus corpus.jsonl --db scorpion.db \
  --baseline scorpion.parser:parse_message_with_evidence \
  --candidate my_parser:parse_with_evidence --out certificate.json
```

The corpus loader accepts JSONL or a JSON array. Required: `content` or `text`. Optional:
`message_id`/`id`, `source_ts_utc`/`timestamp`, `guild_id`, `channel_id`, `author_id`,
`expected_kind`/`kind`, `expected_contract_key`. A record without content is refused, not skipped.
A parser must return `ParseDecision` (event plus evidence), because a confidence is part of the
claim being certified.

## Acceptance

`tests/test_review.py` (9 tests) covers idempotent enqueue, refusal of an unstored event, claim ->
resolve -> calibration, dismissal that stays dismissed, backfill with real decision audits,
metrics and renders, and the CLI. `tests/test_certification.py` (16 tests) covers a faithful
candidate, an action escalation, poor calibration, too few adjudications, identical baseline and
candidate, exact McNemar, risk-coverage selection, calibration bins, a channel-level segment
regression, an underpowered segment that is reported rather than refused, corpus loading, and the CLIs.

The full PR#3 gate:

```text
ruff check src tests            -> All checks passed
mypy src/scorpion (strict)      -> Success: no issues found in 60 source files
pytest                          -> 346 collected, all passing
```

## What this does not prove

A certificate describes this corpus and these adjudications, not all future messages. Accuracy is
an event-kind comparison; contract correctness is stricter and is reported separately. Action
escalations are refused by default because that is the costly direction, not because every
escalation is wrong. Segment parity is a one-sided floor with no multiplicity correction: it
refuses a visible drop, and an underpowered segment is reported instead of being passed. The
queue prioritizes review; it does not decide what the review should say. The digest proves the
certificate is the same evidence, not who produced it.

## Next

The next honest build is not another score. It is a raw Discord revision corpus with enough
human-adjudicated examples to make the 50-sample floor routine rather than aspirational. Until
then, the queue is the safest place for the evidence to accumulate.
