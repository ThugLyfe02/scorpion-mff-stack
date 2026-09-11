# Scorpion v0.24 — operator observability, host readiness, and measured hot-path budgets

v0.24 turns production readiness from an informal collection of health checks into one measurable,
short-lived control-plane judgment. It does not change locked MFF strategy semantics or grant
unattended brokerage authority.

## 1. One operator truth surface

`operator_observability.py` produces a read-only, hashed snapshot spanning the seams that previously
required several commands and mental joins:

- runtime halt state and reason;
- schema compatibility;
- durable raw backlog and oldest receipt age;
- review and delivery backlog;
- expired delivery leases;
- ingress queue utilization and normalized-consumer liveness;
- historical stage latency;
- active release identity by component;
- safety mode and exact release bound to that safety state;
- emergency NO_TRADE sentinel presence;
- current rollout / rollback state;
- active shadow identity;
- bottleneck findings, blockers, warnings and deterministic next actions.

An operator therefore does not have to infer whether a fresh heartbeat belongs to a dead consumer,
whether NORMAL safety belongs to the wrong active model, or whether several individually healthy
subsystems describe different production generations.

`scorpion-prod-status --db <db>` emits the snapshot as structured JSON and returns non-zero when a
blocking seam exists.

## 2. Hot-path benchmark

`hot_path_benchmark.py` benchmarks three different latency domains instead of publishing one blended
number:

1. pure parse / association / reducer compute;
2. FULL-sync durable raw receipt;
3. full durable receipt -> normalized transition.

It records p50/p95/p99/max latency, SQLite precommit p95, WAL/FULL-sync mode, Python/SQLite/platform
identity, final deterministic state fingerprint, and a hash over the benchmark evidence.

The benchmark also proves that every synthetic raw event already owns a durable receipt sequence
after the first persistence boundary. This protects the performance optimization from silently
weakening deterministic recovery.

`scorpion-hot-benchmark --workspace <dir> --samples <n>` runs only against an isolated store.

## 3. Remove one serialized write boundary

The production gateway now uses `append_raw_with_receipt()`.

Previously the live path was:

```text
raw event + PENDING write
  -> COMMIT
receipt sequence write
  -> COMMIT
normalized transition
  -> COMMIT
```

v0.24 binds raw evidence, PENDING processing state, and receipt ordering in one `BEGIN IMMEDIATE`
FULL-sync transaction:

```text
raw event + PENDING + receipt sequence
  -> COMMIT
normalized transition
  -> COMMIT
```

The normalized reducer remains single-writer and deterministic. This removes an avoidable SQLite
lock acquisition/fsync boundary rather than obtaining speed by weakening durability.

The gateway still performs the durability work off the asyncio event loop and remains capture-only
after normalized processing halts.

## 4. Short-lived production-readiness certificate

`production_readiness.py` composes:

- schema contract;
- operator observability;
- live bottleneck/conflict audit;
- isolated host hot-path benchmark;
- atomic receipt-order proof;
- exact requested component;
- exact active release and safety consistency;
- isolated partial-outage chaos drills.

The result is a hashed, expiring `ProductionReadinessCertificate`.

A passing result is deliberately named `READY_FOR_OPERATOR_DECISION`: it says the host/control plane
is currently healthy enough for a human-controlled production decision. It is not production
activation authority and does not submit an order, change a release, clear NO_TRADE, or choose risk.

The certificate binds the exact active release, operator snapshot, bottleneck report, benchmark and
chaos run. A later control-plane generation therefore produces different readiness evidence rather
than silently inheriting an old answer.

`scorpion-prod-ready --db <db> --workspace <dir> --component <name> --operator <id>` emits the
certificate and returns non-zero on any failed readiness check.

## 5. Why this improves performance rather than just observability

The same measurements used for readiness localize where latency is being spent.

- core p95/p99 regression points to parser/association/reducer compute;
- durable-receipt regression points to filesystem/fsync/SQLite lock pressure;
- DB-precommit regression points to normalized transaction amplification;
- full-path regression with stable subcomponents points to scheduling/thread handoff overhead;
- queue/backlog growth despite healthy microbenchmarks points to burst capacity or external stalls.

This prevents optimization work from chasing whichever subsystem is easiest to rewrite. The
bottleneck can be named and measured first, then changed surgically.

## 6. Operational invariants

v0.24 preserves the asymmetric control model:

- measurement and observability are read-only against production state;
- benchmarks and chaos drills use isolated stores;
- runtime may automatically capture, halt, quarantine and trip NO_TRADE;
- production activation remains explicitly human controlled;
- rollback application and resume remain explicitly operator controlled;
- no component chooses personalized live position size;
- no component submits unattended securities/options orders.

The competitive advantage is measurable adaptation: Scorpion can tell not only whether a model has
edge, but whether the current host, evidence graph, runtime, storage path and safety generation are
healthy enough to preserve that edge at production speed.
