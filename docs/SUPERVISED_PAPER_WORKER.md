# Supervised external equity paper worker (P0 pilot)

Stacked against #12, not main. Default run-session remains intent-only. No live
host, new asset class, unattended restart, or autonomous policy change exists.
The venue reports external *simulator* behavior, not actual exchange fills.

## Operator prerequisites

- Review and pass the stacked PR CI. Use an exclusive, opening-flat USD equity
  paper account. Inject only SCORPION_ALPACA_PAPER_KEY and
  SCORPION_ALPACA_PAPER_SECRET privately; never commit credentials or market data.
- Pin the existing paper-risk.v1 JSON, then explicitly initialize the ledger
  via paper-order-init. Retain its returned chain head outside the worker.
  No manual trading or competing writers during a session.
- Independently pin a worker authorization file. Every policy symbol needs an
  operator-selected sell limit on a valid tick; no dynamic exit-price discretion.
  The illustrative example is not a recommended trading price:

```json
{
  "schema": "scorpion.supervised-paper-worker.v1",
  "authority": "PAPER_ONLY",
  "risk_sha256": "OPERATOR_PIN",
  "opening_ledger_head": "EXTERNALLY_RETAINED_INIT_HEAD",
  "exit_limits": {"XYZ": "9.00"}
}
```

- Verify licensed XNAS.ITCH mbp-1 entitlement, source clocks, Discord allowlist,
  disk/fsync throughput, paper API latency/rate limits, and safety-latch
  integrity. Run the existing capture benchmark at production burst rates.
  The adapter's double-read snapshot may consume the three-second quote budget:
  misses must be measured, not worked around by extending the quote TTL.
- Arm the latch explicitly, then start a preplanned, monitored session:

```sh
python -m scorpion run-session --capture-market --authorize-paper-worker \
  --paper-worker-config /private/worker.json --paper-worker-sha256 WORKER_PIN \
  --paper-risk-policy /private/risk.json --paper-ledger /private/paper.db
```

Do not enable this via the default systemd service/timer. A reviewed pilot
service must disable automatic restart, allow sufficient stop time, isolate
credentials and have independent alert/reconciliation coverage.

## Runtime contract

The producer offers only bounded candidates after journal admission. The worker
waits for the durable sequence and revalidates the source record. A stale
monotonic heartbeat, shutdown, source fault, full queue or closed latch stops
admission. A cooperative guard cannot atomically revoke an in-flight HTTP call.

Each dispatch uses the existing ledger CAS and atomic exposure reservation.
A committed reservation is never retried, even if POST was not sent or lookup
returns 404. Independent snapshots and recovery run off the producer loop.
Unexpected external orders, inventory drift, excessive cash residual and
blocked venue account latch the worker. Deadline sweeps cancel only identified
working orders; late cancellation can make a chase ineligible.

Exit uses exact confirmed terminal entry inventory and the pinned operator
limit, not modeled fills. The conditional parent quantity is an upper bound.
A resting exit can leave inventory. Shutdown closes new admission, joins the
worker and attempts cancellation only of identified working orders, then
requires a flat reconciled account. No automatic market liquidation or
arbitrary retry. If unresolved, escalate to the paper-account operator.
On host crash, use explicitly authorized paper-order-recover and
paper-order-sweep, never auto-resume a stopped journal or ledger.

## Research-only calibration

The separate worker audit uses the existing byte-sealed journal. Dispositions
bind source record hash, role, sequence and ledger head. A clean audit is not
an independent signature. Keep the final ledger head in external custody,
and obtain independent dual-signed venue outcomes. The pilot's trust
account_ref must equal the pinned paper account ID.

Run paper-worker-calibration with the existing paper-outcome-labels arguments
plus --audit, --worker-config, --worker-sha256, --risk-policy, --ledger, and
--ledger-head. It re-verifies source and audit, candidate denominator,
ledger identity, signed outcome admission and fixed-tape replay. Refusals,
rejections and missing/censored routes are not zero fills. The paired
fill-fraction residual compares the external simulator with the local replay;
it is subset diagnostic evidence, not a statistical edge or P&L claim.

Use paper-round-trip-reconcile for signed economic outcomes,
paper-execution-costs and paper-session-statistics for cost attribution and
prospective session-level inference, and paper-study-reserve/assess/finalize
for independent preregistered challenger qualification. Research must have
no venue credentials or policy/latch writes. No self-promotion is authorized.

## Operational limits

CI uses synthetic transport and data. No real-data throughput or authenticated
venue calls have been certified. Existing full-prefix validation is bounded
but may miss latency targets; an incremental prefix verifier, measured API
budget, and trade-update stream require further fault certification. Venue
double-read snapshots are not atomic. Hash chains are not rollback-resistant
without independent checkpoints. Static exit floors can miss fills; separately
review a deterministic bounded quote-based exit policy before permitting it.
