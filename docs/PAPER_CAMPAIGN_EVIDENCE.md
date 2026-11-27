# Paper campaign evidence collection and pre-open checks

Stacked on the verified PR #14 revision c44ec655d5632cad755900725539343e905dbc41.
No live endpoint, risk-policy expansion, automatic restart or deployment
activation is introduced. Only a separately authorized paper campaign may
produce external observations.

## Pre-open workflow

The existing pilot requires a session journal from before the opening bell.
Use paper-campaign-check with --pre-open on the policy's session date after
initializing the exclusive flat paper ledger and pinning its opening head.
It checks the venue's fresh closed market clock, exact opening cash, actual snapshot, latch,
operator exit roster, and the same conservative 1,500 ms read latency budget.
Do not treat this small sample as a p99 guarantee. The worker now refuses
late starts in its opt-in mode; intent-only mode is unchanged. The default
service and timer remain unchanged.

## Independent post-close collector

Run on a separately controlled collector environment, after the source and
audit journals have closed, on the same exchange session date:

```sh
python -m scorpion paper-collect-venue --authorize-paper-api \
  --policy /private/risk.json --policy-sha256 RISK_PIN \
  --out /private/venue-source.json
```

The only transport method is GET to the fixed Alpaca paper origin. The
provider credentials may have broader permissions than this program;
network/process separation and key custody are operational responsibilities.
The collector reads account, all current-session orders, all open orders,
positions and FILL account activities in two independent passes. Activity
pagination uses the last activity ID, bounded 100-row pages and a maximum
of 20 pages per pass; no response is inferred complete after a full final
page. A 500-order response fails as possibly truncated. The original
response bytes and paths are retained with SHA-256 fingerprints, not
request headers or secrets. Outputs are exclusive-create mode 0600 on
POSIX, fsync'd with the parent directory. Failed inconsistent reads do not
publish a complete artifact; separately retain any failure diagnostics.
Use a private directory, never commit broker data.

Two normalized passes must agree, including explicit terminal timestamps,
individual fill identities, cumulative/remaining quantities, side, symbol,
time, aggregate quantity and exact notional. The cutoff is the start of
collection, not the end; no order update later than the cutoff is admitted.
A rounded provider aggregate that differs from exact fill notional is
refused, not silently adjusted. A coherent but nonflat or working account
can be captured with blockers; it cannot produce a completed outcome draft.

The adapter follows the Trading API docs:
https://docs.alpaca.markets/us/docs/account-activities
https://docs.alpaca.markets/us/reference/getaccountactivities-2
https://docs.alpaca.markets/us/reference/getallorders-1

## Offline preparation for independent signatures

On a private research copy of the stopped execution state, retain external
pins for the source artifact and final ledger head. The existing trust
manifest must identify the account UUID and source_id alpaca-equity-paper.
Distinct collector/reviewer keys must be established before the campaign.

```sh
python -m scorpion paper-prepare-outcomes \
  --journal /private/session.jsonl --audit /private/worker-audit.jsonl \
  --worker-config /private/worker.json --worker-sha256 WORKER_PIN \
  --risk-policy /private/risk.json --ledger /private/paper.db --ledger-head FINAL_HEAD \
  --trust /private/trust.json --trust-sha256 TRUST_PIN \
  --source-artifact /private/venue-source.json --source-sha256 SOURCE_PIN \
  --out /private/unsigned-outcomes.json
```

The offline path replays every expected GET path, checks original response
bytes, verifies both sealed journals, binds all candidate dispositions to
the ledger, and inspects a historical account snapshot without writing
ledger events. Every dispatch intent digest and exact historical source
prefix must be present in the final sealed journal. No credentials are
needed by the offline path. An ambiguous dispatch or missing venue order
refuses drafting; an intent that was never dispatched remains unobserved.

The draft deliberately contains reviewed_ts_utc=null. It is not a signed
outcome or authorized training input. Independent collector and reviewer
must verify the raw artifact and all normalized fields, establish the
actual review time, then sign the exact finalized bytes using the existing
two signature domains. Use paper-verify-reviewed-outcomes with the existing paper-outcome-labels\narguments plus --draft and --draft-sha256 to verify the signed file. Only\nthe review timestamp may differ from the independently pinned draft; any\nchanged normalized field refuses even when both signatures are valid.
No signer is embedded, and no signature custody is implied by the program.
Retain the unsigned draft digest, final signed export digest, and source
pin separately. Neither this artifact nor a hash alone authenticates
the provider; independent collection and review remain required.

The collector covers entry fills and checks exit activity against ledger
identity. It does NOT infer zero fees, construct the independent fee/market
mark supplement, or certify economic round trips or profitability.
Those need explicit order-level fees/rebates, independent market marks,
opening/closing account evidence and signed round-trip supplements.
No synthetic fill, P&L, training, promotion or automatic order follows.


## Local campaign operations report

After both journals close, use the separately retained final ledger head:

```sh
python -m scorpion paper-campaign-report \
  --journal /private/session.jsonl --audit /private/worker-audit.jsonl \
  --worker-config /private/worker.json --worker-sha256 WORKER_PIN \
  --risk-policy /private/risk.json --ledger /private/paper.db --ledger-head FINAL_HEAD
```

The report verifies candidate coverage and the exact durable reservation
roster, counts disposition reasons and order states, and reports timing
distributions for offer-to-durable-observed (including queue wait), offer
to disposition, dispatch and reconciliation. Reservation slack is measured
at the durable reservation, **not** the actual network send or exchange
arrival. Empirical p95 and p99 are withheld below 20 and 100 observations,
respectively; they are not population guarantees or production SLOs.
Missing timing samples are explicit blockers. A session with zero dispatches
has no execution-latency evidence. The report never contacts the venue,
certifies external execution, or changes the policy or ledger. Unsealed
crashes still require the existing explicit recovery procedure.
