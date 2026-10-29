# Execution quality: what our fills cost, and what the bars actually proved

Two instruments, one purpose: stop trusting assumptions that have not been measured.

    python -m scorpion_mff.cli tca path   [--md reports/path-analysis.md] [--json]
    python -m scorpion_mff.cli tca fills  [--executions live/executions.jsonl] [--json]

`path` works today on the repo's own contract bars. `fills` needs venue executions and is
empty until the hone trades. Both refuse to print a number they cannot support.

## 1. Path analysis — and the finding that matters

Contract bars live in `mff-backtest/charts_1m/bars`. There are two naming schemes and they
are not equal:

| scheme | files | content |
|---|---|---|
| `trade_<n>_<date>_<TICKER>_<side>_<strike>.json` | 22 | full RTH session, 5-minute bars (81 rows), ending at ~0.01 |
| `<date>_<instrument_id>.json` | 43 | a **single** bar each — not a day of data |

The loader reads only the first kind and joins to the corpus **by content** (date, ticker,
side, strike), because the `trade_<n>` index is a numbering from the bar-fetch pass, not a row
in `trades.json`. 18 of the 22 join; the 4 that do not are trades absent from the corpus
(including two dated after the corpus ends).

### The result, stated plainly

    joined                                    18
    measurable (entry AND exit in coverage)    2     <- aggregates use only these
    entry before coverage                     16     <- pre-market entries; bars start 09:30
    path aggregates                        withheld (2 < floor of 5)
    carry: value at first covered bar vs entry   -12.9% median
    contracts that expired at ~zero             17%

**The corpus trades pre-market and the bars start at 09:30.** For 16 of 18 trades the move
that mattered had already happened before the data begins. Any MAE/MFE table over those rows
would describe a trade nobody entered, so the report withholds the aggregates below five
measurable trades and says why. That refusal is the deliverable: an assumption labelled
*unverified* is worth more than a statistic that looks precise and is not.

### What the bars do settle

* **Carry is expensive.** The median contract was **−12.9%** below our entry by the first
  covered bar. That is the honest cost of a missed exit at the open: the position does not
  wait for you.
* **17% finished at ~zero.** Combined with the carry number, this replaces a hand-wave in
  `BACKTEST.md` ("an unexited expire is marked at entry, which hides the real loss") with a
  measured magnitude. The expiry sweep in the engine is therefore not a bookkeeping nicety —
  it is the difference between booking 0 and booking −100% of the premium.
* The two measurable trades (one contract, two entries) both show the exit level printing and
  no stop being hit — a sample of two, quoted in the per-trade table and nowhere else.

## 2. Venue fills — built, waiting for the first real execution

`Execution` records what a venue actually did, and `cost_usd` is **signed so that positive is
always money lost**: a buy above its limit and a sell below its limit both cost.

| field | meaning |
|---|---|
| `limit` vs `fill_price` | cost per contract, and in bps of the limit |
| `fill_qty` / `fill_ratio` | partial fills are visible, not rounded into a full fill |
| `intent_ts` → `fill_ts` | latency in ms, against the 10 s budget |
| `reference` | his posted bid, so we can measure how far through it we actually paid |
| `through_bid` | `fill_price / reference` — the number that calibrates `ask_multiple` |

The report gives mean and p90 cost, the share filled at or better than our limit, the partial
share, p90 latency, and **a suggested `ask_multiple`**: the p90 of what we actually paid past
his bid, **rounded up** to 0.05. Rounding up is deliberate — that number would become an
assumption in the backtest, and an assumption that is optimistic by construction is worse than
a conservative one. It refuses to suggest anything below ten fills with a reference price.

`ExecutionLog` is append-only JSONL, one write per record, torn lines skipped on read.

With no fills the command says exactly that:

    fill analysis | no venue fills recorded yet
      live/fills are intents; executions are written by the broker when a
      venue confirms a fill. ... nothing here is estimated.

## What is missing, and what it would take

1. **Pre-market bars.** The single highest-value data acquisition for this desk: his entries
   are pre-market and the analysis cannot see them. With 04:00–09:30 coverage the path
   analysis becomes real for 16 more trades.
2. **Venue executions.** `live/executions.jsonl` fills automatically once the broker records
   confirmed fills; then `tca fills` calibrates `ask_multiple` from evidence and the +25% gate
   can be judged against measured slippage instead of a modelled one.
3. **Post-fill quotes for mark-out.** The engine can record the quote it acted on; with that,
   mark-out (price 30/60 s after the fill) becomes measurable and adverse selection stops
   being a hypothesis.
