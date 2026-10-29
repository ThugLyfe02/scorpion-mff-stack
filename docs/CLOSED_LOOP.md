# Closed-loop coverage: the one number

**Status 2026-10-29: 0 / 107 closed. 105 parse, 3 price, 0 execute, 0 replay.**
Command: `python -m scorpion.cli closed-loop`

`docs/fleet/DIRECTION.md` names the problem this document exists to answer: the stack has
proofs for every segment and a number for none of them. Mark-out is 3 of 107, TCA is 2 of 18,
attribution is 91 of 93, the live lane is 0 of 93. Each is true; none is the question a reviewer,
a desk owner, or a risk committee actually asks:

> Of the alerts the desk actually posted, how many can this stack take **end to end**?

This is that number, computed as a chain of four segments that must all hold for one alert:

| Segment | Question | Evidence used | Refusal code when it fails |
|---|---|---|---|
| **parse** | does the reference parser read the reconstructed post unambiguously, and name the contract the corpus recorded? | `parser.parse_message` on `corpus_replay.entry_text(trade)` | `unparsed:<kind>`, `parse_contract_mismatch` |
| **price** | do real bars exist for that contract on that day, with the entry inside their coverage? | `coverage.classify` (3 of 107) | `no_bars`, `no_exit`, `entry_*`, `multi_session_bars`, `clock_skew` |
| **execute** | did the venue acknowledge **both** sides of the cycle? | the live lane's fill/execution JSONL | `no_entry_ack`, `no_exit_ack`, `venue_record_malformed` |
| **replay** | did the same alert replay through the pipeline, and did the applied log reproduce the book? | `corpus_replay.replay` + `verify` | `no_replay`, `replay_unavailable`, `replay_refused`, `replay_divergence` |

An alert is assessed in that order and **stops at the first failure**. The report therefore
names the input to fetch, not an opinion about the strategy.

## What the number says today

```text
closed-loop  0 / 107 (0.0%)   parse 105  price 3  execute 0  replay 0
  venue       no confirmed records on record - segment 3 is unmeasured, not passed
  blocking     96  price:no_bars
  blocking      5  price:no_exit
  blocking      3  execute:no_entry_ack
  blocking      2  parse:unparsed:ignore
  blocking      1  price:entry_outside_session
    etf-options                  0/34    0.0%  price:no_bars 30, price:no_exit 2, ...
    high-confidence-options      0/10    0.0%  price:no_bars 8, ...
    lotto-friday                 0/14    0.0%  price:no_bars 12, ...
    options-king                 0/42    0.0%  price:no_bars 40, ...
    platinum-options              0/7    0.0%  price:no_bars 6, ...
```

Read it in order, because that is how it is computed:

1. **Parse is not the problem.** 105 of 107 alerts parse to exactly the contract the ledger
   recorded. The two exceptions are `NKE` posts, which the parser deliberately ignores as an
   excluded ticker - a rule, working.
2. **Price is the wall.** 3 of 107 alerts have bars that cover the entry. The other 101 stop
   there, and 96 of those are simply `no_bars`. The corpus is a list of trades; it is not a
   list of market data. `docs/FRONTIER_*` and `coverage.py` have said this for weeks; this
   report is the first place the cost of it is visible as one number.
3. **Execute is unmeasured, not failed.** The three alerts that reach segment 3 have no venue
   record, because there is no live fill yet. `no_entry_ack` is the honest code for "we did not
   look", and the report prints it as unmeasured rather than passing it.
4. **Replay is unassessed for all of them.** It is only reached when segments 1-3 hold, which is
   why the default run does not spend 90 seconds replaying the corpus to learn nothing.

The moment the live lane writes an `ack`, this number can move without a code change:

```bash
python -m scorpion.cli closed-loop --venue ../../live/fills.jsonl --replay --names 20
```

## The two artifacts it consumes

The live lane emits two JSONL shapes, and this report reads both without importing that lane's
code (the seam in `docs/fleet/DIRECTION.md` §3):

| Shape | Where it comes from | How the contract is recovered |
|---|---|---|
| fill log - `{"event": "ack", "contract_key", "qty", "price", "ts", "note"}` | `live/fills.jsonl` | the row carries the contract |
| execution log - `{"t": "intent"|"ack", "cid", ...}` | `live/execution.jsonl` | `cid` joins the ack to the intent that names the contract |

Only the venue's **answers** become records. A `buy`/`sell` decision row and an `intent` row are
plans; counting a plan as an execution is how an offline rehearsal becomes a fabricated closed
loop. An ack for an order the log never recorded is skipped, not adopted: that is an operator
event, and adopting it would invent an intent. Anything that is neither shape, a torn line, an
unreadable file, or a non-object row is **refused with its line number** - a venue log that
cannot be read is not an empty book.

## What it refuses, deliberately

- **A missing venue log is `no_entry_ack`, never "execute skipped".** An unmeasured segment is a
  missing input, not a pass.
- **An unrun replay is `no_replay`, never "replay assumed clean".** The number is only as strong
  as its weakest assessed segment.
- **Three of four is not closed.** `reached` and `closed` are separate counts; the headline
  number uses `closed` only.
- **Contract identity is compared by value.** The ledger writes `227.50` and the parser writes
  `227.5` for the same strike; the parser keys a contract by its **expiry** while `coverage`
  keys a bar set by the trade **day**. Comparing those as strings reported 54 of 107 alerts as
  parse mismatches - a measurement bug masquerading as a parse defect. The stage compares
  `(ticker, side, Decimal(strike), expiry-day)` instead, and a test pins it.
- **A venue record from another session does not satisfy today.** The day is the ET date of the
  ack's timestamp, and the contract's expiry has to be the same contract the alert names.

## Why this lives in the control-plane lane (PR #1)

The live lane owns execution truth (it is the only place an order can exist). The control plane
owns **correctness truth and the studies** - and a corpus-wide coverage number is a study, not an
order. This module reads the live lane's *artifacts* and never its code; the two lanes meet at a
JSONL shape, exactly as the direction document specifies.

## What is not built

- **No extrapolation.** The report does not project what coverage would be if bars existed, or
  what the P&L would be on the closed loop. A missing bar is a missing measurement.
- **No automatic bar fetching.** Filling the 96 `no_bars` gaps is a data acquisition project
  with a cost; this report prices that cost in alerts, which is the first step, and stops.
- **No live execution fallback.** If the venue log is absent the answer is zero. That is the
  correct answer, and the report says why in one line.

## Acceptance

```bash
.venv/Scripts/python -m scorpion.cli closed-loop            # 0/107, the blocking reasons
.venv/Scripts/python -m scorpion.cli closed-loop --json     # machine-readable
.venv/Scripts/python -m scorpion.cli closed-loop --names 20 # the alerts, by identity
.venv/Scripts/python -m pytest tests/test_closed_loop.py -q  # 23 passed
```

Exit 0 is the report; exit 2 is unreadable evidence (the corpus, the bars, or a *provided* venue
log). A zero is never an error - it is the artifact.
