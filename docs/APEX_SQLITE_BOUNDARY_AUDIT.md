# Program A — MFF SQLite connection boundary audit

## Exact scope and result

Inspected the PR5 reconciliation worktree at parent
`33093ce37c3f15934c222b2e06887dcb7990f774`. The narrow runtime follow-up is local commit
`d2e1b011a7e163ffe479495ebddc582903477b1f`, tree
`32fa9816fe42a61918b67c379f92a3ebb137614d`. The published equivalent of the parent is `69a6f7e831b0a31fd346504a1078225f400e15d7`;
the published SQLite follow-up is `ea2327cc923985d74795380506ed42e74c5e3486`,
with the exact tree above. Final source PR5 is `c75afb7b08730e7cc7cd6fba67760d550645e67d`.

The actual Pipeline startup, order migration, failure/quarantine, integrity initialization,
integrity verification, and checkpoint-writing paths now explicitly restore per-connection
`synchronous=FULL`, `foreign_keys=ON`, and `busy_timeout=5000` before mutation. A deliberately
weakened connection factory exposed 12 failing entry points before the fix; the two existing
positive controls (Store and durable ingress) already passed. All 14 cases pass after the fix.

This is a bounded runtime guarantee. It is not a claim that every research, administrative,
archive, or backup connection in the repository uses the same policy.

## What was measured

Python 3.13.15, SQLite 3.53.1. This SQLite build reports `DEFAULT_SYNCHRONOUS=2` and
`DEFAULT_WAL_SYNCHRONOUS=2`. A fresh connection measured FULL (2), FK disabled (0), and
busy timeout 5000 ms. Therefore the absent explicit FULL settings were a portability and
connection-factory invariant gap, not evidence that this machine had been running those
writers with synchronous OFF.

The adversarial probe sets each newly opened connection to synchronous OFF, FK OFF, and
busy timeout 1 ms. The permanent regression matrix observes the actual connection settings
before DDL, write statements, and transaction admission. It covers startup functions whose
names suggest reads but which also create or migrate tables. It does not simulate a power cut.

## Runtime connection coverage

| Entry point | Role / actual mutation | After fix | Evidence |
| --- | --- | --- | --- |
| `Store.connect` | Raw evidence, runtime halt, transactional normalized state/effects, heartbeats | Explicit FULL / FK ON / 5000 | Existing sentinel test plus degraded-default positive control |
| `durable_ingress.append_raw_with_receipt` | Atomic raw evidence + pending state + receipt order | Explicit FULL / FK ON / 5000, unchanged | Degraded-default positive control |
| `processing_order.register_raw_receipt` | Durable receipt sequence | Shared explicit runtime policy | New degraded-default case |
| `processing_order.load_signals_in_processing_order` | Creates/backfills process and receipt ordering during startup/replay | Shared explicit runtime policy | New degraded-default case |
| `processing_order.load_pending_raw_in_receipt_order` | Creates/backfills ordering before pending replay | Shared explicit runtime policy | New degraded-default case |
| `processing_order.inspect_processing_order` | Creates/backfills ordering during inspection/recovery | Shared explicit runtime policy | New degraded-default case |
| `failure_quarantine.record_processing_failure` | Failure count, failure event, pending/quarantined state | Shared explicit runtime policy | New degraded-default case |
| `failure_quarantine.requeue_quarantined` | Operator requeue of earlier failed revision | Shared explicit runtime policy | New degraded-default case |
| `failure_quarantine.load_quarantined` / `quarantined_count` | Create failure schema, then read | Shared explicit runtime policy | Two new degraded-default cases |
| `IntegrityLedger._connect` | Integrity schema initialization on runtime restore; standalone append API | Shared explicit runtime policy | New constructor case; synthetic standalone append probe |
| `verify_database_evidence` | Integrity schema initialization/migration before runtime verification | Shared explicit runtime policy | New degraded-default case |
| `create_state_checkpoint` | Checkpoint insertion uses existing `Store.connect`; order preparation now configured | Explicit policy on every writing connection in covered operation | New composite degraded-default case |
| `Pipeline` restart | Combined order, integrity, quarantine, and halt behavior | Every observed mutating connection restored | New composite degraded-default case |
| `execution_journal._connect` | Actual fill journal and reconstruction helper | Explicit FULL / FK ON / 5000 from preceding reconciliation | Existing 5-connection degraded-default execution-journal regression |

Read-only connections in `execution_state.load_blocked_event_ids` and the checkpoint
lookup/integrity-hash readers remain read-only SQL paths with default connection flags. FULL
is not a write-durability requirement for those reads. Their presence must not be interpreted
as a failed checkpoint-writer assertion or represented as configured writer coverage.

The shared policy is a small extraction from existing `Store.connect`; it refuses setup after
a transaction has started. It does not instantiate Store, recreate its schema, change WAL
mode, or import a research subsystem. Durable ingress retains its existing isolated explicit
settings. No transaction boundaries were changed.

## Explicit residuals outside the narrowed runtime fix

| Surface | Current behavior | Significance / limits |
| --- | --- | --- |
| `DeliveryLedger._connect` | Explicit busy 5000; FULL comes from current build; FK OFF; no declared FK in its schema | Durable prepared/leased/acknowledged records deserve explicit policy before wiring this standalone API into a deployment. There is no direct use by `run_discord` or `Pipeline`. No live brokerage dispatch was added. |
| `ReleaseRegistry._connect` | Explicit busy 5000; FULL comes from current build; no declared FK | Administrative release ledger remains a separate residual, not covered by runtime-writer attestation. |
| `NoTradeSafetyLatch` | Raw connections use current FULL/5000 defaults; no declared FK | The independent fsync'd emergency sentinel still protects risk-decreasing trips if SQLite fails. This audit does not replace its outage tests with an assumption about SQLite. |
| `ProductionGateRegistry` | Raw connections use FULL/5000 defaults; declared dossier-to-approval FK is disabled on current default connections | The API checks dossier existence itself, so this audit does not demonstrate an authorization bypass. Database-level FK enforcement is nevertheless absent here. |
| `DeploymentStateMachine._connect` | Explicit FK ON and busy 5000; FULL comes from current build | Declared rollout FKs are active; portable explicit synchronous guarantee remains a separate administrative-control hardening item. |
| `schema_migrations` and older `_schema_migrations_v27_core` writer | Explicit FK ON and busy 5000; FULL comes from current build | Operator migration entry points, not Pipeline startup's order/integrity migration helpers. |
| `decision_store.resolve_decision_packet` | Raw connection uses current FULL/5000 defaults; no declared FK | Operator review resolution is separate from normalized packet insertion, which already uses the Store transaction. No critical Pipeline call to this standalone resolver was found. |
| `recovery.create_verified_backup` | Raw source and backup destination connections use current build defaults | Logical replay/integrity verification does not prove physical backup durability under a weakened destination connection. This audit leaves that independent backup-publication risk explicit. |
| `history_archive.HistoryArchive.connect` | Explicit WAL, synchronous NORMAL, busy 10000 | Deliberate archive-specific policy. Do not apply the runtime helper here silently or claim archive capture has the runtime FULL guarantee. |
| `storage_health.inspect_storage` and `hot_path_benchmark._sqlite_modes` | Inspect a newly opened connection | Their connection-scoped synchronous/busy readings do not prove the settings of a different writer connection. The mutation-witness tests provide that evidence. |

Journal mode is deliberately not forced uniformly: an initialized runtime Store uses WAL,
while an independently created standalone integrity database in the probe uses DELETE. FULL
is explicitly restored in either case. FK ON has material enforcement value where schemas
declare FKs (raw state/order/failure records); it does not invent constraints for schemas that
declare none.

The audit searched all SQLite opens in `src/scorpion`, then classified the critical call paths.
It did not refactor the many research ledgers or claim they were all audited against a common
durability contract. Constructors and readers which mutate schema are not treated as pure
reads merely because of their names.

## Validation and change accounting

- Full suite: 480 tests passed at the local follow-up tree.
- `ruff check src tests`: passed (the actual CI lint scope).
- `mypy src/scorpion`: passed, 199 source files.
- `git diff --check`: passed.
- Permanent test file: `tests/test_runtime_sqlite_connections.py` (14 cases).
- Red fixture run: 12 failures and 2 passes before implementation; green: all 14.
- Source delta: +32 / -7 across four existing source files.
- Test delta: +109 in one new regression test file.
- Dependency delta: zero. Configuration delta: zero. New TCB source files: zero.
- Two new import edges point to existing Store policy from order and quarantine modules.
- Strategy, parser, eligibility, quantity, and authorization semantics are unchanged.
- Isolated before/after latency measurement belongs to the parent baseline benchmark, pending
  at this audit's completion. No latency improvement is claimed.

An exploratory `ruff check .` also traversed locked historical backtest and unrelated research
artifacts and reported 417 existing lint findings. Those files were not changed; the CI-scoped
check above passed. It would be incorrect to describe the entire historical repository as
globally lint-clean.

## Reproducible artifacts

The original paths below identify measured artifacts; their complete bytes are retained as deterministic gzip under `docs/apex/program-a/sqlite/` using the same basename plus `.gz`:

- `apex-evidence/sqlite_boundary_probe.py`: standalone temporary-database probe.
- `apex-evidence/sqlite_boundary_probe.json`: measured settings before this fix.
- `apex-evidence/sqlite_boundary_probe_after.json`: measured settings after this fix.
- `apex-evidence/sqlite_runtime_red.log`: permanent regression matrix before implementation.
- `apex-evidence/mff-pr5-sqlite-full-pytest.log`: complete-suite successful output.
- `apex-evidence/mff-pr5-sqlite-collection.log`: independent collected test count.

The standalone probe records final settings for all connections, including read-only ones;
the permanent tests are the narrower proof that each actual mutation in the listed runtime
entry points happens after restoration. Do not collapse these different evidence scopes into
one repository-wide green badge.

The later baseline capture in `APEX_BASELINE_ATTESTATION.md` completes the before/after benchmark. Final PR5 CI passes 482 tests after the separate benchmark-workload correction; the 480-test count above is the earlier exact SQLite follow-up tree, not the final head.
