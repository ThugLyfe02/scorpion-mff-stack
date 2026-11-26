# Paper adaptive evidence and operational campaign gate

Stacked on the green PR #13 revision 68101d8021a43183eb6768db0efb22fc915835ad.
Research and execution remain separate. No order policy, live host, size change,
automatic restart, model promotion, spend or training is authorized by this PR.

## P0: measure, do not guess

The worker now binds the candidate record digest at both existing venue
validation points. Removing the separate preparatory parse reduces full
prefix validations per candidate from three to two; prefix validation also
decodes each JSONL record once rather than twice. Both before-reservation and
before-send checks remain. This is a reduction in work, **not** a measured
end-to-end latency speedup. Same-file append and full-prefix integrity checks
remain mandatory; no unsafe cached-prefix trust has been introduced.

Each disposition records elapsed monotonic nanoseconds since offer. Worker
latency audit records separate dispatch and reconciliation wall durations
measured with a monotonic clock. These are observational records, not
latency guarantees. The stage timings include network/database operations.

On the private deployment machine, after explicit opening-flat account
initialization, independently retain the returned ledger head, operator risk
and worker manifest hashes. Run the read-only probe during the pinned session:

```sh
python -m scorpion paper-campaign-check --authorize-paper-api \
  --worker-config /private/worker.json --worker-sha256 WORKER_PIN \
  --risk-policy /private/risk.json --ledger /private/paper.db --samples 3
```

The probe checks the frozen account and opening ledger, allowed exit roster,
safety latch, read-only broker account snapshots and market clock. It performs
GETs only; never initializes, cancels, reserves or submits. It rejects a
worst measured snapshot-plus-clock read above 1,500 ms (half the 3-second
entry freshness window) as a conservative pilot diagnostic. This tiny
sample cannot certify p99, request-rate capacity, or full-path admission.
Do not run repeatedly without checking the venue API quota. No credentials
or raw broker artifacts should enter Git.

Run existing paper-capture-benchmark on the actual deployment disk using
measured capture burst rates; check ingress/feed entitlement and local clock.
Do not loosen freshness or exposure limits to make latency results pass.
Confirm the independent collector and reviewer identities, alert channel,
emergency cancellation and nonflat-account manual procedures before starting
the worker. No live money. Default service/timer remains unchanged.
Operator-pinned exits may rest; an unfilled exit or unknown state requires
manual paper reconciliation and never grants automatic liquidation authority.
No automatic recovery from a stopped writer or old ledger is introduced.

## P1: frozen historical baseline and forward monitoring cohort

Use the existing signed cost reports and prospective paper-session cohort
policy. Prepare two distinct, non-overlapping, complete-calendar cohort
manifests with the same trust, cost policy and clipping scale. The baseline
must be completely elapsed at its as-of cutoff; missing or censored data
remain intervals. Run paper-session-statistics on the baseline and retain
the exact report digest. Separately pin the following plan *before* monitoring
starts, with at least a 24-hour embargo. Keep the pins in independent custody.
The code verifies byte identity and declared clock chronology; it cannot
prove external timestamping or stop a human from creating a new registry.

```json
{
  "schema": "scorpion.paper-adaptive-plan.v1",
  "authority": "RESEARCH_ONLY",
  "baseline_cohort_sha256": "BASELINE_COHORT_PIN",
  "baseline_report_sha256": "BASELINE_REPORT_PIN",
  "monitor_cohort_sha256": "MONITOR_COHORT_PIN",
  "baseline_cutoff": "2026-11-20T21:00:00+00:00",
  "frozen_at": "2026-11-21T21:00:00+00:00",
  "train_sessions": 20,
  "miscoverage": 0.1,
  "density_ratio": 2,
  "novel_mass": 0.05,
  "score_floor_bps": -10,
  "adverse_null": 0.1,
  "adverse_alternative": 0.4,
  "family_alpha": 0.05,
  "budget_units": 15,
  "economic_floor_bps": 0,
  "economic_clip_bps": 100
}
```

All parameters are examples, not performance targets or recommended limits.
Plan sample size before collection. Use the existing independent prospective
study registry to govern formal alpha spending and candidate qualification;
this diagnostic plan is not a substitute for that ledger.

```sh
python -m scorpion paper-adaptive-research \
  --plan /private/adaptive.json --plan-sha256 PLAN_PIN \
  --baseline-cohort /private/baseline.json --baseline-inputs /private/base-inputs.json \
  --monitor-cohort /private/monitor.json --monitor-inputs /private/monitor-inputs.json \
  --trust /private/trust.json --cost-policy /private/cost-policy.json \
  --economic-inputs /private/economics.json --as-of EXPLICIT_UTC_CUTOFF
```

### Calibrated abstention

One scalar per exchange session: clipped negative parent-route implementation
shortfall, not trade profit. Train on a fixed chronological prefix, calibrate
on the remaining baseline sessions, then freeze the envelope for a separate
monitoring cohort. Censored calibration intervals contribute their maximum
possible residual, never a guessed midpoint. The finite-sample order-statistic
rank is ceil((n+1)*(1-alpha)); a rank beyond the calibration sample means full
support, not an extrapolated quantile. Formal split-conformal marginal
coverage requires exchangeable session scores; financial time dependence and
shift can break that assumption. Hence diagnostic envelope and abstention only.
The holdout separately reports certain/possible coverage and misses.

### Distributionally robust uncertainty

Use the existing exact bounded-density-ratio mixture solver on every historical
session's lower/upper identification interval. Equal baseline session mass
avoids trade-count weighting. Add epsilon mass of unknown bounded support:
the stressed lower bound is (1-epsilon)*minimum mixture - epsilon*clip.
The same optimizer bounds the adverse-session probability, accounting for
certain vs possible violations. This is exact for the specified empirical
ambiguity set; neither a population confidence interval nor proof that all
future market distributions lie inside it. Raw tail failures from the
existing cohort remain independent qualification blockers.

### Decay monitoring

For each endpoint, mix Bernoulli e-processes over every fixed prospective
change start; unstarted bets retain e-value 1. This avoids an uncorrected
maximum over windows and cannot forget earlier alarm evidence. A confirmed
adverse indicator is hi < floor; a possibly adverse indicator is lo < floor.
The former is pointwise no greater than the latent true adverse event and
the likelihood-ratio bet is monotone in this indicator. Its mixture is
dominated pathwise by the true mixture under the conditional Bernoulli null,
even with unknown observations. The possible path is explicitly a
diagnostic watch, not an anytime-valid alarm. The alpha budget is split
between the two endpoints within this frozen plan; no claim of global
type-I control across arbitrary new plans or endpoint definitions.
Do not interpret no alarm as health, especially under missingness.
Never reset the plan, roster or threshold based on results.

The separate economic endpoint is clipped account cash change divided by
opening cash, in bps, for complete opening/closing-flat, independently
dual-attested paper accounts with exact fee and cash reconciliation and
matching signed cost/entry evidence. The optional economics index contains:

```json
{
  "schema": "scorpion.paper-economic-inputs.v1",
  "sessions": [
    {
      "session_date": "2026-11-23",
      "export": "day/round-trip.json",
      "artifact": "day/round-trip-source.json",
      "collector_signature": "day/round-trip-collector.sig",
      "reviewer_signature": "day/round-trip-reviewer.sig"
    }
  ]
}
```

Missing or invalid supplements are unknown, not zero. Entry/exit/fill and
artifact reuse is checked across the available evidence. The economics
monitor is paper performance deterioration, not benchmark-adjusted alpha,
live profitability or model promotion. Raw economics remain in the existing
signed round-trip report; this monitor uses bounded values only.

### Information-value research allocation

Five fixed deficit families: calibration resolution, distribution shift,
evidence completeness, execution cost degradation and economic deterioration.
Score the observable deficits and use the existing exact discrete knapsack
for light/deep shadow and human review. Deficit-derived utilities are
explicit engineering proxies, not measured information gains or expected
P&L. Budget units are abstract review/compute costs, not dollars or GPU
hours. No execution, experiment dispatch, resource spending or model
training is authorized. Integrate with realized learning-yield attribution
after actual independently adjudicated research outcomes exist, then freeze
a new validated mapping in a separate PR.

## Remaining before a real external paper campaign

CI is necessary, not sufficient. Use the actual licensed feed, paper account,
operator pins, signed collectors and dedicated host. Verify measured timing,
complete account/venue observation, clean flat shutdown and independent
post-close signatures. The raw venue simulator may differ from exchange
liquidity. Do not equate the first successful paper order with profitable
alpha. No automatic resumption, quote-based exit authority or streaming
broker event ingestion has been introduced. Those require independent
design, fault certification and operator authorization.

### Decision-theoretic research information value

The adaptive report additionally evaluates exact finite-outcome beta-binomial
expected value of information for one and five hypothetical future adjudicated
sessions for each deterioration endpoint. Research retain/retire utility is
max(0, tolerance minus posterior adverse-event probability). Enumerate all
predictive outcomes, every possible missing adverse-indicator count, and four
fixed beta priors. Report both minimum and maximum expected decision value;
only the minimum normalized value can supplement the deficit-based utilities.
This sensitivity model assumes conditionally IID Bernoulli observations and
a fixed utility function; correlated sessions may violate those assumptions.
It is not an empirical measure of learning gain, monetary EV or a guarantee
that spending compute creates independent observations. No dispatch occurs.
