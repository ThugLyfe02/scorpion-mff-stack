# Causal quote admission — paper-only release prerequisite

The existing equity forward-paper path computed freshness solely from local receive age.
A delayed exchange event could therefore become a fresh executable quote on receipt.

This slice checks exchange age at ingress, stores that age once, and ages the quote with
the monotonic clock thereafter. Future, delayed, duplicate and reordered events cannot
refresh the price. An unusable newer quote withdraws the previous price; disconnect
invalidates all cached prices. The event watermark survives reconnect within the process.
Identical exchange timestamps are conservatively discarded; provider sequence semantics
must be validated before loosening this rule.

The accepted budget is 3 seconds by default (the existing stale policy); this is not a
measured optimal threshold. It is not a claim about OPRA or consolidated coverage.
Actual subscription entitlements, snapshot semantics, feed gaps and timestamp provenance
must be exercised with a licensed provider before a forward session is certified.

No strategy rule, broker authorization, or production account changes. This fixes market
data admission; it does not make intent-only session output into verified fills.

## Follow-up: ambiguous feed identity

Exact retransmissions do not renew prices or size. Conflicting payloads at the same exchange
timestamp withdraw the cached quote until a strictly newer event arrives; a timestamp alone
cannot establish ordering between distinct updates. The subscription universe bounds admission;
unsolicited symbols cannot refresh feed liveness. A failed dynamic subscription can be retried.
Naive timestamps and monotonic regressions are not accepted as healthy quote evidence.
