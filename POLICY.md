# The risk layer: policy, governor, and what the record actually says

The executor knows how to place a trade. This is about how much of the account is allowed
to be at risk, what happens after a loss, and when the desk stops. It is the difference
between a bot and a desk.

Three modules, one doctrine:

    scorpion_mff/policy.py       the written policy - versioned, validated, reduce-only
    scorpion_mff/governor.py     policy + live state -> a multiplier the engine may not exceed
    scorpion_mff/expectancy.py   what the record says, with its uncertainty

    python -m scorpion_mff.cli policy show|validate|init|simulate
    python -m scorpion_mff.cli desk [--md reports/desk-report.md]

## The one invariant

**The policy is reduce-only.** Every multiplier it can express is clamped to `(0, 1]`.
The locked clip table is the ceiling; the policy may only shrink it. This is enforced
three times, on purpose:

1. `policy.parse()` **rejects** a document containing any multiplier above 1.0, naming the
   key (`channel_multipliers.etf-options: 1.5 exceeds the locked cap 1.0 - the policy
   cannot size up, only down`). A risk file that tries to raise risk is a bug worth
   surfacing, not one worth silently clamping.
2. `governor.decide()` takes the **minimum** of every rule that binds, so rules cannot
   cancel: a channel tilt of 1.0 cannot restore size after a daily stop.
3. `governor.apply_contracts()` clamps again and floors, so a hand-edited file that
   bypassed validation still cannot size up. A test asserts a governor returning
   `factor=5.0` produces exactly the locked clip.

## The policy document

`live/policy.json` (never generated silently; `policy init` refuses to overwrite):

| field | meaning |
|---|---|
| `risk_unit_usd` | 1R in dollars (default $1,500 = the locked standard clip) |
| `daily_loss_limit_r` / `daily_loss_limit_usd` | hard daily stop; at least one is required |
| `max_drawdown_r` | stand down when drawdown from the equity peak reaches this |
| `ladder` | `at_r -> factor`, e.g. `-1.0R -> x0.5`, `-2.0R -> x0` |
| `loss_streak` | `at -> factor`, e.g. `2 losses -> x0.5`, `3 -> x0` |
| `channel_multipliers` / `session_multipliers` | tilts down for a channel or a session |
| `stale_after_minutes` | a policy older than this refuses to arm |
| `provenance` | required: why this exists and who reviewed it |

Expressing limits in **R** rather than dollars is deliberate: the numbers move with the
account, the doctrine does not.

## Two kinds of stop

* **Daily stop** (`daily_loss_limit_*`) resets at the day roll. It is a scale, then a stop.
* **Max drawdown** (`max_drawdown_r`) is **terminal for that state file**: once breached,
  `halted_reason` is written into `live/governor.json` and every later decision stands down
  — including after equity recovers. A limit that clears itself the moment you get lucky
  is not a limit. Re-arming is `arm` refusing until a human deletes `live/governor.json`
  after a review; that deletion is the audit trail.

Risk state is **persisted after every settle** (`live/governor.json`, atomic write). This
matters more than it looks: a crash loop or a Task Scheduler relaunch that forgot its
drawdown would silently reset the limit the file exists to enforce. `arm` loads the file,
refuses on a corrupt one (a corrupt risk file must not parse as "no drawdown"), and
refuses outright if it is already stood down.

## What the record says (`desk report`)

91 closed trades from the corpus, 1R = $1,500:

| metric | value |
|---|---|
| expectancy | $636.82/trade (+0.425R) |
| win rate | 87.9% (95% CI 79.6%–93.1%) |
| profit factor | 50.62 |
| per-trade score | +0.302 |
| max drawdown | $429.00 (0.29R) |
| longest losing run | 1 |
| runs test | z = +0.97 — consistent with independence |

**Channel allocation** (ranked by shrunk expectancy ÷ dispersion, not by average P&L):

| rank | channel | n | win rate | exp/trade | shrunk | score |
|---|---|---|---|---|---|---|
| 1 | platinum-options | 7 | 100% | $590 | $618 | +0.861 |
| 2 | etf-options | 34 | 88.2% | $270 | $353 | +0.643 |
| 3 | options-king | 41 | 90.2% | $573 | $586 | +0.478 |
| 4 | high-confidence-options | 9 | 66.7% | **$2,349** | $1,448 | +0.238 |

The last row is the interesting one, and it is the reason this module exists: the channel
with the **largest average trade** ranks **last** per unit of risk, because its wins are
enormous and its losses are too. Ranking channels by average P&L would have pointed the
next dollar at the lumpiest one. Shrinkage (`PRIOR_TRADES = 10`) also keeps the 7-trade
channel from outranking a 41-trade channel on luck alone.

**The runs test is a negative result, honestly reported:** z = +0.97 means wins and losses
here alternate slightly more than chance. Three losses in a row are expected ~0.16 times
across this whole record. So the loss-streak rule buys *discipline*, not edge — it is cheap
insurance against a regime this corpus does not contain, and it is calibrated against the
measured loss rate rather than a fear.

## Does the policy do anything? (`policy simulate`)

On the record as traded, **the baseline policy is inert — zero interventions**. That is the
correct and slightly unflattering answer: the record has a 0.29R max drawdown and no losing
streak, so there is nothing for a risk ladder to bite on. The locked rules are already
tight. A policy demonstrated only on data it never binds on has not been demonstrated at
all, so `policy simulate` takes the same record under a worse fill assumption:

| scenario | ungoverned | governed | difference | interventions |
|---|---|---|---|---|
| as recorded | $57,951 | $57,951 | — | 0 |
| exits at 50% of his level | −$4,469 | **+$653** | +$5,122 | 21 |
| exits at 30%, pays 15% through the bid | −$33,487 | **−$9,937** | **+$23,550** | 53 |

In the 50% stress the ladder halves at −1R, halves again on two losses, stops at three, and
then the max-drawdown rule stands the desk down on 2026-08-25 at 6.57R. From that point the
desk does not trade — which is why the governed number is only +$653 and not a hero number.
**A risk layer that never stops you is not a risk layer.**

## What this does not claim

* The corpus is the desk's own winners-heavy call record. Expectancy is calibration, not
  forecast; it cannot price the calls he did not post.
* Sessions are bucketed from a fixed UTC-04:00 offset (EDT), not a market calendar.
  `governor.session_for` is the function to replace when a real calendar arrives.
* There is no position-level stop. There **is** now a correlation cap - the
  `scorpion_mff/exposure.py` guard refuses a second position in the same underlying, an
  expiry cluster, or a per-underlying gross-premium breach - but it is **opt-in**: the
  engine ships with `exposure_limits=None`, and `arm` activates it only when
  `live/exposure.json` exists (`python -m scorpion_mff.cli exposure init` writes the
  default). Enabling it is a rule decision, not a silent default.
* Per-channel allocation is a *ranking*, not an automatic reallocation of size. Wiring the
  ranking into `channel_multipliers` is a decision for a human, on evidence that is still
  thin (7 trades in the top row).
