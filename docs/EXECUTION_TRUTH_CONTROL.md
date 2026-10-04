# Execution Truth Control Plane

Scorpion v0.6 separates three concepts that must never be conflated in a trading control plane:

1. **source intent** — what the Discord signal appears to request;
2. **execution admission** — whether that event is allowed to participate in executable state;
3. **execution truth** — what quantity actually filled and at what price.

A signal is not a fill. A proposed effect is not a position. A review decision is not a broker confirmation.

## Admission isolation

Research-only and system-blocked decision packets are retained for research, association, audit, and source-behavior analysis, but they are excluded from the execution-state replay.

This means a blocked QQQ/SPY research alert cannot:

- consume `max_open_positions`;
- consume the first-entry pipe-test slot;
- alter executable generation state;
- be recorded later as a valid execution fill.

The pipeline keeps a separate observed/source state so follow-up language can still be associated with research signals without allowing those signals to contaminate executable capacity.

## Append-only fill journal

`execution_journal.py` introduces an append-only SQLite ledger for fills that already happened.

Supported sources are:

- `PAPER`
- `EXTERNAL_CONFIRMATION`
- `MANUAL_RECONCILIATION`

The journal never routes, submits, modifies, or cancels an order.

Every fill binds:

- source event ID;
- exact contract and generation;
- signed quantity delta;
- fill price;
- final-close marker;
- fill source;
- external execution reference when applicable;
- recorder identity;
- actual fill timestamp;
- journal-record timestamp;
- note.

The journal validates the record against the durable proposed effect and decision packet. Blocked effects are rejected. Open/add fills must increase quantity; trims/closes must reduce it; reductions cannot exceed held quantity; a final close must consume the full executed position.

The deterministic `quantity_hint` is also enforced. In particular, a first-entry pipe-test effect cannot be turned into a larger executed position merely by writing a larger fill.

## Idempotent broker confirmation

External confirmations require an `external_ref`, intended to be the broker/exchange execution identifier.

The pair `(source, external_ref)` is unique. Re-ingesting the same execution with the same material facts returns the existing ledger record. Reusing that reference with conflicting price/quantity/event data fails closed.

## Tamper evidence

Each fill has a canonical JSON payload and SHA-256 payload digest. Ledger records form a hash chain:

`record_hash = SHA256(sequence | fill_id | previous_hash | payload_sha256)`

Verification also recomputes the canonical payload from the SQLite columns. This catches accidental or partial mutation of fields such as quantity, price, event, contract, generation, timestamps, or recorder identity.

The same limitation as any local hash chain applies: an attacker capable of rewriting the entire database and recomputing the entire chain can forge local history. Stronger non-repudiation requires anchoring the current head hash in an independent system.

## Reconstructed execution state

`reconstruct_execution_state()`:

1. replays only admitted normalized signals;
2. verifies the complete fill ledger;
3. applies fills in actual fill-time order;
4. validates every fill against its durable effect and current executed quantity;
5. returns execution state only if the full reconstruction is internally consistent.

If verification or reconstruction fails, execution truth is **unavailable** rather than guessed.

## Reconciliation

`scorpion-reconcile` now compares external positions against reconstructed execution truth, not against signal intent.

If fill integrity is broken, reconciliation refuses to produce a false clean result and emits a critical `execution_truth_unavailable` finding.

External reconciliation also flags:

- unexpected positions;
- missing positions;
- quantity mismatches;
- average-price mismatches;
- duplicate contract observations;
- invalid non-positive average prices;
- unsupported short positions in the current long-only state model.

## Recording fills

`scorpion-record-fill` records a fill that has already happened.

Example:

```bash
scorpion-record-fill <event-id> \
  --db scorpion.db \
  --quantity-delta 1 \
  --fill-price 1.05 \
  --source EXTERNAL_CONFIRMATION \
  --external-ref <broker-execution-id> \
  --recorded-by <operator>
```

For a trim/close, `--quantity-delta` is negative. `--final` may only represent a close that consumes the entire currently held quantity.

The command's output explicitly states that Scorpion did not submit an order.

## Operator surface

`scorpion-ops` now includes:

- fill-ledger verification;
- fill count;
- execution-truth availability;
- reconstruction anomalies;
- exact reconstructed quantities, average prices, statuses, and generations.

That gives the operator one place to see whether source interpretation, durable evidence, and actual executed state still agree.
