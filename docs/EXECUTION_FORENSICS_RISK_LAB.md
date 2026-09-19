# Scorpion v0.5 — Execution Forensics, Risk Lab, and Fastpath

This layer is designed to answer a harder question than “did the Discord alert eventually go up?”:

> **What would Scorpion have actually done, under the current deterministic rules, at a realistic post-alert latency, using contemporaneous option quotes, and what evidence is strong enough to support research on position sizing?**

It deliberately refuses to turn sampled Discord history, posted percentage screenshots, missing option quotes, or a single attractive backtest into a sizing recommendation.

## Scope and non-goals

This layer is additive to Ryan's source strategy. It does not change `live/RULES.lock.md`, `docs/HANDOFF.md`, the existing `mff-backtest/` artifacts, agent snapshots, or the Ross scanner skill.

It also does **not** implement unattended live securities/options submission. The low-latency path is deterministic code with no LLM in the critical decision path, but it terminates at paper execution or an explicit human-authorization boundary.

No segment, score, simulation, or sizing envelope is a profit guarantee.

## 1. Exhaustive authorized Discord history archive

`scorpion-history-sync` uses an authorized Discord bot token to read the configured source channels from the beginning of the history available to that bot.

```bash
SCORPION_DISCORD_TOKEN=... \
scorpion-history-sync --archive scorpion-history.db
```

Properties:

- read-only: no messages, edits, reactions, or deletes;
- default scope is the four configured MFF source channels;
- verifies each channel belongs to the configured guild;
- walks `channel.history(limit=None, oldest_first=True)`;
- buffers messages into SQLite batches rather than committing one row per message;
- stores text plus visible Discord embed author/title/description/fields/footer;
- does not guess image text with OCR;
- records textless/embed counts so missing textual evidence is visible;
- keeps history in a **separate research archive** so old Discord messages never enter the live reducer;
- archives immutable message revisions using message ID + edit timestamp + content hash;
- repeated full audits are idempotent;
- later-observed edits are preserved instead of replacing prior evidence;
- migrates the earlier v1 history table safely;
- records per-channel oldest/newest timestamps, revision count, unique message count, and whether the iterator actually reached the beginning.

`reached_beginning=true` means the bot completed the history iterator for data accessible to that Discord identity. It does not claim that deleted or permission-inaccessible messages exist in the archive.

The repository's older `mff-backtest` evidence is useful context but is not treated as exhaustive. In particular, the prior multi-year work explicitly sampled Discord result pages. v0.5 will not emit sizing envelopes merely because those sampled CSVs look attractive.

## 2. Provider-neutral historical quote tape

Execution reconstruction requires timestamped option quotes. The tape format is JSONL:

```json
{"contract_key":"AAPL|CALL|200|2026-09-08","ts_utc":"2026-09-08T14:00:00.250000+00:00","bid":"1.03","ask":"1.05"}
```

Each record must contain:

- `contract_key`
- offset-aware `ts_utc`
- positive `bid`
- positive `ask`
- `ask >= bid`

The tape is provider-neutral by design. Scorpion does not fabricate historical NBBO data from Discord percentages or bar OHLC.

Most importantly, the lookup is **causal**: execution forensics only selects quotes at or after the simulated decision timestamp. A pre-alert quote cannot leak into a post-alert fill.

## 3. Exact-execution forensic replay

`scorpion-forensics` runs archived Discord messages through the same deterministic interpretation machinery used by the control plane:

```text
raw archived Discord
        ↓
parser v3
        ↓
reply/source association
        ↓
strategy eligibility
        ↓
deterministic reducer + invariants
        ↓
source-driven effect
        ↓
simulated decision latency
        ↓
historical quote tape
        ↓
actual modeled fill / skip / miss
```

Default execution profile:

- normal source channels: `$1,500` base clip;
- high-confidence channel: `$3,000` base clip;
- source-posted add: `50%` of base clip, producing the intended `$2,250` / `$4,500` total budget pattern when filled;
- first successfully filled entry of the market day: `1 contract` pipe test;
- simulated decision latency: `250 ms` by default;
- first usable quote must be no more than `3 s` after the target timestamp;
- entry limit uses the current rule: `min(live_ask, source_bid * 1.15)`;
- if contemporaneous ask is more than `+25%` above source bid, the historical entry is marked stale and skipped;
- if ask is between the +15% limit and the +25% stale boundary, Scorpion waits up to the configured limit window for a quote whose ask is actually at or below the limit;
- buys/adds use ask-side fills;
- trims/exits use bid-side fills;
- adds only occur when a source `ADD` event is parsed—there is no synthetic “down 15%, therefore add” rule;
- trim behavior sells down to one runner;
- Scorpion never invents a later runner exit simply because Discord posted a final percentage;
- a `CompletedTrade` enters the sizing dataset only when its lifecycle is fully supported by source actions and timestamped quote evidence.

Forensic leg statuses include `FILLED`, `RESEARCH_ONLY`, `REVIEW`, `NO_QUOTE`, `STALE_ENTRY`, `LIMIT_NOT_FILLED`, `UNAFFORDABLE`, `NO_POSITION_QUANTITY`, and `NON_ACTIONABLE`.

## 4. ETF and lotto isolation

The current execution-priority posture is implemented as an **eligibility overlay**, not deletion of source data.

Default research-only categories include:

- the `etf-options` Discord channel;
- known ETF/leveraged ETF underlyings such as QQQ, SPY, IWM, DIA, TQQQ, SOXL, TSLL, etc.;
- alerts explicitly carrying lotto-style language such as `lotto`, `ER lotto`, or `FOMC lotto`.

These messages still receive:

- raw evidence capture;
- parsing;
- state/replay visibility;
- forensic research classification;
- integrity/audit evidence.

But an actionable effect is persisted as `BLOCKED_STRATEGY`, and its Operator Decision Packet is `BLOCKED_STRATEGY`. It stays P3/research-only in the operator inbox and does not become urgent merely because it ages.

`BLOCKED_SYSTEM` still takes precedence when the resilience controller is halted.

This preserves the ability to develop a dedicated ETF/lotto strategy later without allowing today's general execution path to treat those alerts as equivalent to the preferred single-name bucket.

## 5. Evidence-gated strategy research

The risk lab does not rank a segment solely by historical P&L.

For every completed quote-supported segment it calculates:

- sample count;
- arithmetic and median return;
- win rate;
- 90% Wilson lower bound on win rate;
- profit factor;
- lower-tail CVaR;
- realized path maximum drawdown;
- time-ordered positive-fold ratio;
- bootstrap lower bound on mean return;
- one-sided bootstrap probability of non-positive edge;
- shrinkage of the observed mean toward zero;
- conservative edge = minimum of the shrunk mean and bootstrap lower bound;
- a composite evidence-weighted edge score.

Segments are generated for channel, strategy bucket, ticker, and the combined set.

### Multiple-testing control

Searching many channels/tickers can manufacture apparent winners by chance. `rank_segments()` therefore applies Benjamini-Hochberg false-discovery-rate control across the tested segment family.

A standalone `score_segment()` is treated as a single-hypothesis context (`q = p`). When multiple segments are ranked, q-values are recomputed across that family.

A segment cannot become a selected research candidate merely because it has the highest backtest return.

Default selection gates also require:

- at least 30 fully reconstructed trades;
- FDR q-value <= 0.10;
- positive conservative edge;
- at least 75% of chronological folds with positive mean return;
- observed maximum drawdown <= 50%;
- 90% lower win-rate bound >= 45%.

Failing any one of those gates produces an explicit rejection reason.

## 6. Position-sizing stress lab

A selected segment can then enter the risk simulator.

The simulator uses block resampling rather than independent single-trade shuffling so short winning/losing streak structure is better preserved.

Default research constraints:

- horizon: 100 trades;
- 3,000 simulations per tested fraction;
- block length: 5 historical trades;
- tested risk fraction up to 10% in 0.25% increments;
- ruin floor: 50% of starting equity;
- maximum accepted estimated ruin probability: 1%;
- maximum drawdown threshold: 25%;
- maximum accepted probability of breaching that drawdown: 5%.

The envelope reports the largest **tested research fraction** that passes the configured simulation constraints. It is deliberately named `max_research_risk_fraction`; it is not a live order-sizing instruction.

If sample depth, conservative edge, FDR significance, history completeness, or quote evidence is insufficient, the correct output is zero / no envelope.

## 7. Missing-data protection

`scorpion-forensics` withholds sizing envelopes unless both evidence gates pass:

1. every requested Discord channel is marked exhaustive by the history synchronizer; and
2. timestamped quote evidence coverage meets the configured threshold (95% by default).

Example:

```bash
SCORPION_SIGNAL_AUTHOR_IDS=... \
scorpion-forensics \
  --archive scorpion-history.db \
  --quotes option-quotes.jsonl \
  --min-quote-coverage 0.95 \
  --latency-sensitivity \
  --output forensics-report.json
```

This is intentional protection against survivorship/selection bias. If missing historical quotes cluster around the fastest losses or difficult fills, using only the surviving observations could make the strategy look much better than it was.

## 8. Latency survival analysis

The optional sensitivity harness replays the same raw history across a latency grid, defaulting to:

```text
50 ms
100 ms
250 ms
500 ms
1000 ms
2000 ms
```

Each scenario reports:

- completed quote-supported trades;
- mean and median return;
- win rate;
- total modeled P&L;
- entry attempts and fill rate;
- stale entries;
- +15% limit misses;
- missing quote legs.

The combined report shows mean-return range, fill-rate range, and fastest-vs-slowest modeled mean return.

This gives a direct empirical answer to whether sub-second code execution is economically material for the signal class instead of assuming lower latency must always improve the strategy by the same amount.

## 9. Deterministic no-LLM fastpath

`fastpath.py` is the coded preparation path for current alerts.

A separate market-data process feeds `QuoteCache`; the critical path then performs in memory:

```text
accepted deterministic event/effect
        ↓
Operator Decision Packet
        ↓
strategy eligibility
        ↓
latest cached quote + freshness check
        ↓
+25% dislocation check
        ↓
+15% limit construction
        ↓
PAPER_READY or AWAITING_HUMAN_AUTHORIZATION
```

No LLM or browser round-trip is required for quote validation or limit construction.

Fastpath dispositions include:

- `PAPER_READY`
- `AWAITING_HUMAN_AUTHORIZATION`
- `WAITING_FOR_LIMIT`
- `ENTRY_DISLOCATION`
- `QUOTE_UNAVAILABLE`
- `QUOTE_STALE`
- `REVIEW_REQUIRED`
- `BLOCKED_STRATEGY`
- `BLOCKED_SYSTEM`

In `REVIEW_ONLY`, dispatch returns `AWAITING_HUMAN_AUTHORIZATION`; it does not submit a live order. In `PAPER`, it can exercise the deterministic paper broker.

This replaces an agent as the latency-critical decision/preparation component without creating unattended live brokerage authority.

## 10. What this layer can and cannot currently prove

The repository's pre-v0.5 sampled data demonstrates that relevant Discord history is visible back into 2022 for some channels, but that earlier work explicitly did not exhaustively parse every intermediate page.

Therefore, v0.5 does **not** claim that a complete multi-year performance result has already been computed.

The proper production research sequence is:

1. run the authorized full history sync until all target channels report `exhaustive=true`;
2. obtain a timestamped option quote tape with sufficient coverage for the relevant contracts/times;
3. run exact-execution forensics under the current rules;
4. inspect missing/review/stale/limit-miss distributions;
5. require the history + quote evidence gates;
6. evaluate FDR-controlled strategy candidates;
7. inspect latency sensitivity;
8. only then inspect research sizing envelopes.

A system that refuses to size when its evidence is incomplete is more useful than one that always produces an impressive number.
