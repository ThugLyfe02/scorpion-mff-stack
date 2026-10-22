# The risk layer that prices the loss before it happens

`POLICY.md` describes the governor: a reduce-only risk policy applied to *realised*
P&L. This document describes two things that sit beside it:

1. **`scorpion_mff/risk.py`** — a pre-trade budget that prices the loss that has not
   happened yet, and the four hot-path defects that made the gap visible.
2. **`scorpion_mff/evidence.py`** — what a record of trades can and cannot prove,
   with intervals, a posterior, selection control, drift detection and a hard gate
   against sizing on a biased corpus.

Both are opt-in or advisory where they touch live behaviour. Neither can raise size.

---

## Part 1 — the four defects

Each was reproduced against the engine before it was fixed; the regressions live in
`tests/test_engine_lifecycle.py`.

### 1. An expired contract key was a permanent tombstone

`_expire_carried` released the *slot* by setting `contracts = 0`, but left the key in
`Engine.positions`. A contract key is not unique across days:

```
QQQ|CALL|719|0DTE      # the same string every day that strike trades
```

The next day's entry hit `if key in self.positions` and was skipped as
`already_open`. The engine went silent on the most common contract class — the exact
failure mode (`silence`) the hot path exists to remove. It also leaked one dict entry
per expired contract.

**Fix:** delete the key when the contract is released, and treat only an *open*
position as blocking. The fill log keeps the `expire` record.

### 2. A STOP halted entries but not adds

`_on_stop` set `engine.halted`, and `_on_entry` checked it. `_on_add` did not. A
`STAND DOWN` followed by `adding at 1.30` increased exposure on a desk that had been
told to stop. Exits remain legal, as the lock requires.

**Fix:** `_on_add` rejects with `halted:stand-down`.

### 3. Adds bypassed the governor entirely

The documented invariant is that the policy may only shrink the locked clip, and the
engine "cannot exceed it". Adds did not call the governor at all, so a daily stop, a
loss ladder or a channel tilt could be undone by the next add post.

**Fix:** adds run the same `governor -> apply_contracts` path as entries, before the
position is mutated. A stand-down or a zero factor rejects the add.

### 4. An add without a channel used the wrong clip

```python
tier_for_channel(signal.channel or position.ticker, is_first_of_day=False)
```

A ticker is not a channel. When an add post did not restate the channel, a
high-confidence position was sized at the standard add clip ($2,250 instead of
$4,500). Conservative, but wrong, and invisible in the log.

**Fix:** fall back to `position.channel`.

### 5. A close for one strike could close another

`_resolve` used the full contract key only when it matched a held position, then
fell through to ticker/side. A close for `QQQ 720C` while holding `QQQ 719C` matched
the held 719 by ticker and side: the engine flattened the wrong contract and emitted
a sell for a contract it never held. Reproduced: the 719 position disappeared and a
`QQQ|CALL|720|0DTE` sell intent was produced.

**Fix:** when a post carries a full contract key, resolve only that key. The one
legitimate fallback is an *omitted expiry* (`QQQ 719 calls closed`): it resolves only
when exactly one open position shares ticker, side and strike. A different strike, or
a different known expiry, is never another position. The order intent now always
names `position.contract_key`, not the post's spelling — otherwise an expiry-less exit
would route an unroutable `...|?` key.

### 6. A strike could become the exit price

`NVDA STRIKE $180 CALLS CLOSED` (no `AT 2.65`) parsed the `$180` strike as the exit
price. The engine resolved the held NVDA 180C, sold 14 contracts at $180, and booked a
**$250,572 phantom profit** instead of the quote's $2.60. The same fallback made an
entry's posted price equal its strike.

**Fix:** `_DOLLAR_RE` refuses a dollar amount followed by option language (`STRIKE`,
`CALLS`, `PUTS`, `EXP`, `CONTRACT`) and the price loop skips a bare amount equal to the
parsed strike. Anchored prices (`AT 2.65`, `AVG 1.20`) still win. Regression tests parse
the strike-only exit and assert the engine books the quote, not the strike.

---

## Part 2 — the pre-trade budget

### The hole a realised stop cannot close

The baseline policy has a 2R daily stop: $3,000 at 1R = $1,500. The locked clips can
open two high-confidence positions at $3,000 premium each. If both expire worthless:

```
realised loss = -$6,000
daily limit   = -$3,000
```

The stop could not fire before the loss existed. It reacts after settlement, and by
then the day is twice over its limit. A stop on realised P&L is necessary; it is not a
bound on the day.

### The arithmetic

For a long option, the maximum loss is the premium paid. So the book's worst-case day
is:

```
worst_case = realised_today - sum(open premium) - new premium
```

A candidate is admitted only if `worst_case >= -daily_loss_limit`. In headroom terms:

```
new_premium <= daily_limit + realised_today - open_premium
```

This uses cost basis, not the current mark. A position that is up cannot lose more than
it cost, so cost is conservative at every price and exact at zero.

### Three caps, all reduce-only

| cap | what it prevents |
|---|---|
| `max_total_premium_usd` | the whole book risking more than the budget |
| `max_underlying_premium_usd` | two strikes on QQQ becoming one oversized bet |
| `max_cluster_premium_usd` | QQQ + SPY + NVDA calls pretending to be diversification |
| `daily_loss_limit_usd` | the worst-case day exceeding the realised stop |

A **cluster** is a declared set of symbols treated as one exposure. The default map is
index ETFs, semis, mega-cap tech and crypto-adjacent names. An unknown symbol is its
own cluster — guessing a correlation is how a diversified book becomes a single
position. A symbol may appear in only one cluster; the loader rejects duplicates.

The result is a contract count, never larger than the one requested:

```python
budget.admit(engine.open_positions, ticker="SPY", qty=30, limit=1.00,
             realized_today_usd=state.realized_today_usd)
# Admission(qty=18, reasons=("total_premium", "daily_worst_case"))
```

`reasons` lists **every** cap that bound, not only the smallest. A log line of
`budget=total_premium+daily_worst_case` is an audit trail, not a shrug.

### Wiring

`Engine` consults the budget after the locked clip and the governor, for entries and
adds. `arm` loads `live/risk-budget.json` if it exists. Missing is allowed (behaviour
unchanged); **corrupt is a refusal**, because a risk file that cannot be read is not
the same as no risk file.

```bash
python -m scorpion_mff.cli risk-budget init          # writes live/risk-budget.json
python -m scorpion_mff.cli risk-budget show
python -m scorpion_mff.cli risk-budget validate
```

`init` refuses to overwrite. Defaults are a shape, not a recommendation: total = 2R,
one underlying = 2/3 of that, one cluster = 5/6. Review the numbers before arming.

---

## Part 3 — evidence, not vibes

`desk report` answers "what did the record average". That is a description. The
decision is different:

* is the average distinguishable from zero?
* how much of it is the tail?
* is the best channel the best, or just the luckiest of four?
* is the edge still there?
* how many trades would it take to know?
* may we size on this record at all?

`scorpion_mff/evidence.py` answers those, and refuses the last one for the historical
corpus.

### 1. An interval on money

A Wilson interval on win rate is not an interval on expectancy. Options P&L is
right-skewed; one 1,200% runner can carry a record while the median trade is a
scratch. The module bootstraps the **mean P&L**, with BCa correction for bias and
skew, and reports the one-sided bootstrap p-value beside it.

### 2. A posterior, not only a p-value

A Bayesian bootstrap (Dirichlet(1) weights) puts a distribution-free posterior on the
mean. `posterior_p_up` is P(mean > 0) given the trades and no normal-likelihood
assumption. It is reported next to the frequentist interval because the two answer
different questions; neither is allowed to overrule the other.

### 3. Channels shrunk by the data, not a constant

`expectancy.py` shrinks each channel toward the pooled mean with ten pseudo-trades.
That is a reasonable default. Here the prior strength is estimated from the
between-channel spread net of sampling variance:

```
tau^2 = var(channel means) - mean(within-channel variance / n)
weight = n / (n + within_variance / tau^2)
```

When the channels are indistinguishable, `tau^2 = 0` and everything is pooled. When
they genuinely differ, each keeps more of its own mean. The fixed prior becomes a
special case rather than an assumption.

### 4. The winner is adjusted for being the winner

With four channels, the best one is selected on noise. Every channel gets a
Benjamini–Hochberg q-value; **certification requires q ≤ 0.10**, not the raw p.

### 5. Drift and power

A single-change-point scan maximises the mean shift over every split with a minimum
segment, and assigns a permutation p-value. A break is reported with the two means and
the split index, not drawn by hand on an equity curve.

`required_n` gives the trades needed to detect the observed effect at 80% power. A
record that is underpowered says so.

### 6. The source gate

| source | may produce an allocation? |
|---|---|
| `LIVE_FILLS` — reconstructed from `live/fills.jsonl` | yes, if status is PROVISIONAL/CERTIFIED |
| `CORPUS_CALIBRATION` — the 93-alert record | **never** |
| `SYNTHETIC` | never |

The corpus is the hone's own record of calls he chose to post. It is winners-heavy by
construction: 81 of 93 are win-class. It can calibrate what the executor would have
done; it cannot price the calls he did not post. Its headline status is
`CALIBRATION_ONLY`, even when the bootstrap is decisive, and the allocation is
withheld with that reason.

### Statuses

| status | meaning |
|---|---|
| `INSUFFICIENT` | fewer than 20 closed trades |
| `NEGATIVE` | posterior P(edge > 0) ≤ 5% |
| `INCONCLUSIVE` | the interval still includes zero |
| `PROVISIONAL` | P(edge > 0) ≥ 95% and the interval excludes zero |
| `CERTIFIED` | ≥ 30 trades, P ≥ 99%, interval excludes zero, q ≤ 0.10 |
| `DEGRADED` | a detected change point with a lower, non-positive post-break mean |
| `CALIBRATION_ONLY` | a non-live source with a statistically strong record |

### Allocation

When eligible, weights are proportional to shrunk mean / variance, scaled by
confidence, capped at **50% per channel**. The remainder is cash; the function does not
renormalise a capped book into a bigger bet. This is a research allocation for a human
to review, not live size.

### Run it

```bash
python -m scorpion_mff.cli evidence report                    # live fills
python -m scorpion_mff.cli evidence report --corpus mff-backtest/bt_full/trades.json
python -m scorpion_mff.cli evidence report --md reports/evidence.md --json
```

`--json` is machine-readable. `--md` writes the shareable report. The command exits 1
when there are no closed trades; a negative verdict is a finding, not an error.

---

## Part 4 — the research desk's context (attribution only)

`scorpion_mff/context.py` verifies the stock-finder's
`scorpion.research-context.v1` snapshot independently: schema, `RESEARCH_ONLY`
authority, `US_EQUITY:<SYMBOL>` identity, and the SHA-256 checksum. It then joins
closed trades to the snapshot by symbol and **ET session date**.

```
python -m scorpion_mff.cli evidence report --context path/to/scorpion-context-DATE.json
```

The output groups P&L by explicit flag (`buyout_at_deal`, `halt_risk`,
`dilution_risk`) and tape call (`GO`/`MIXED`/`FADE`). That answers a research
question: *do hone trades on names the desk flagged underperform?*

It deliberately does **not** size, shrink, delay or veto an order:

* the scanner is research-only, and the MFF money path does not depend on it;
* a missing, stale, tampered or invalid snapshot is ignored with a warning — optional
  context must not become a trading outage;
* there is no conversion from a context row to an `OrderIntent`, a quantity, a limit,
  or an effect. The module imports only the evidence types.

The producer contract is documented in the stock-finder's `CONTEXT.md`; the fixture in
`tests/fixtures/research-context-2026-09-11.json` is generated by the producer and
pinned in both repos, so a canonicalisation drift fails a test rather than a trade.

---

## What this layer does not claim

* It does not forecast the next trade. It measures the record it is given.
* It does not make the corpus unbiased. It labels it and refuses to size on it.
* It does not prove the live path fills at the modelled limit. That is `TCA.md`.
* It does not replace the governor. The budget is a pre-trade bound; the governor is
  the realised-P&L ladder and terminal stand-down.
* Correlation clusters are declared, not estimated. The point is a conservative bound,
  not a covariance model.
