import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.counterfactual import CounterfactualDelta
from scorpion.domain import EventKind
from scorpion.eligibility import StrategyBucket, StrategyEligibilityPolicy
from scorpion.execution_forensics import CompletedTrade
from scorpion.governance import (
    PromotionEvidence,
    PromotionStatus,
    evaluate_promotion,
)
from scorpion.grammar_suite import evaluate_grammar, option_entry_grammar
from scorpion.integrity import IntegrityLedger
from scorpion.online_drift import PageHinkley
from scorpion.parser import ParseDecision, parse_message, parse_message_with_evidence
from scorpion.pipeline import Pipeline
from scorpion.policy_bundle import RuntimePolicyBundle
from scorpion.profiling import profile_messages
from scorpion.provenance import build_research_manifest, stable_hash, verify_manifest
from scorpion.quote_tape import HistoricalQuote, HistoricalQuoteTape
from scorpion.recovery import create_verified_backup
from scorpion.resilience import OperationalMode
from scorpion.schema_contract import inspect_schema
from scorpion.selective import SelectiveObservation, SelectivePolicy, choose_selective_policy
from scorpion.sizing_lab import SizingConstraints
from scorpion.slo import BurnSeverity, ModeHysteresis, SLOBudget, TimedOutcome, evaluate_burn_rate
from scorpion.storage_health import checkpoint_wal, inspect_storage
from scorpion.store import Store
from scorpion.tournament import (
    CandidateScore,
    TournamentCandidate,
    TournamentCriteria,
    run_parser_tournament,
)
from scorpion.walk_forward import WalkForwardPolicy, WalkForwardReport, evaluate_walk_forward


def _trade(index: int, value: str) -> CompletedTrade:
    opened = datetime(2026, 1, 1, 14, 0, tzinfo=UTC) + timedelta(days=index)
    premium = Decimal("1000")
    returned = Decimal(value)
    return CompletedTrade(
        entry_event_id=f"trade-{index}",
        contract_key=f"AAPL|CALL|200|2026-12-{(index % 20) + 1:02d}",
        channel_id="968352649437126676",
        author_id="author",
        bucket=StrategyBucket.CORE_SINGLE_NAME,
        opened_ts_utc=opened,
        closed_ts_utc=opened + timedelta(minutes=10),
        initial_quantity=5,
        add_count=0,
        trim_count=0,
        gross_premium_in=premium,
        gross_proceeds=premium * (Decimal("1") + returned),
        pnl=premium * returned,
        return_fraction=returned,
        holding_seconds=600.0,
        depth_evidence_complete=True,
    )


def test_policy_fingerprint_is_deterministic_and_semantically_sensitive():
    left = RuntimePolicyBundle()
    right = RuntimePolicyBundle()
    changed = RuntimePolicyBundle(
        eligibility=StrategyEligibilityPolicy(block_lotto_language=False)
    )
    assert left.fingerprint == right.fingerprint
    assert left.fingerprint != changed.fingerprint
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})


def test_research_manifest_binds_code_policy_dataset_and_parameters():
    created = datetime(2026, 9, 8, 22, 0, tzinfo=UTC)
    manifest = build_research_manifest(
        code_revision="abc123",
        package_version="0.6.0",
        parser_version="v3",
        policies={"runtime": RuntimePolicyBundle()},
        datasets={"history": "deadbeef", "quotes": "cafebabe"},
        parameters={"latency": timedelta(milliseconds=250), "channels": ("a", "b")},
        created_ts_utc=created,
    )
    identical = build_research_manifest(
        code_revision="abc123",
        package_version="0.6.0",
        parser_version="v3",
        policies={"runtime": RuntimePolicyBundle()},
        datasets={"quotes": "cafebabe", "history": "deadbeef"},
        parameters={"channels": ("a", "b"), "latency": timedelta(milliseconds=250)},
        created_ts_utc=created,
    )
    assert manifest.manifest_hash == identical.manifest_hash
    assert verify_manifest(manifest, manifest.manifest_hash)
    changed = replace(manifest, code_revision="def456")
    assert changed.manifest_hash != manifest.manifest_hash


def test_quote_tape_caps_fill_at_known_displayed_depth():
    ts = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    contract = "AAPL|CALL|200|2026-09-08"
    tape = HistoricalQuoteTape(
        (
            HistoricalQuote(
                contract,
                ts,
                Decimal("0.98"),
                Decimal("1.00"),
                bid_size=1,
                ask_size=2,
                source="fixture",
            ),
        )
    )
    buy = tape.bounded_buy_fill(contract, ts, quantity=5, limit_price=Decimal("1.00"))
    sell = tape.bounded_sell_fill(contract, ts, quantity=5)
    assert buy is not None and buy.depth_known and buy.filled_quantity == 2
    assert buy.complete is False
    assert sell is not None and sell.depth_known and sell.filled_quantity == 1
    assert sell.complete is False


def test_unknown_depth_is_explicit_not_fabricated_as_known():
    ts = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    contract = "AAPL|CALL|200|2026-09-08"
    tape = HistoricalQuoteTape(
        (HistoricalQuote(contract, ts, Decimal("0.98"), Decimal("1.00")),)
    )
    fill = tape.bounded_buy_fill(contract, ts, quantity=4, limit_price=Decimal("1.00"))
    assert fill is not None
    assert fill.filled_quantity == 4
    assert fill.depth_known is False


def test_purged_walk_forward_accepts_stable_oos_edge():
    trades = tuple(_trade(index, "0.08") for index in range(60))
    report = evaluate_walk_forward(
        trades,
        policy=WalkForwardPolicy(
            min_train_samples=20,
            test_size=10,
            min_folds=3,
            embargo=timedelta(0),
        ),
        sizing_constraints=SizingConstraints(
            min_samples=20,
            bootstrap_resamples=100,
            trials=100,
        ),
    )
    assert report.passed is True
    assert len(report.folds) >= 3
    assert report.pooled_oos_mean > 0


def test_purged_walk_forward_rejects_regime_collapse():
    trades = tuple(
        _trade(index, "0.10" if index < 30 else "-0.20") for index in range(60)
    )
    report = evaluate_walk_forward(
        trades,
        policy=WalkForwardPolicy(
            min_train_samples=20,
            test_size=10,
            min_folds=3,
            embargo=timedelta(0),
        ),
        sizing_constraints=SizingConstraints(
            min_samples=20,
            bootstrap_resamples=100,
            trials=100,
        ),
    )
    assert report.passed is False
    assert report.failures


def test_selective_policy_requires_empirical_risk_support():
    strong = tuple(SelectiveObservation(0.99, True, True) for _ in range(100))
    weak = tuple(
        SelectiveObservation(0.99, index % 5 != 0, True) for index in range(100)
    )
    enabled = choose_selective_policy(
        strong,
        max_error_upper_95=0.05,
        min_coverage=0.5,
        min_samples=50,
    )
    blocked = choose_selective_policy(
        weak,
        max_error_upper_95=0.05,
        min_coverage=0.5,
        min_samples=50,
    )
    assert enabled.enabled is True
    assert blocked.enabled is False


def test_page_hinkley_detects_sustained_upward_degradation():
    detector = PageHinkley(delta=0.01, threshold=3.0, min_instances=20)
    for _ in range(40):
        assert detector.update(0.0).drifted is False
    assert any(detector.update(1.0).drifted for _ in range(20))


def test_multiwindow_slo_and_hysteresis_prevent_mode_flapping():
    now = datetime(2026, 9, 8, 22, 0, tzinfo=UTC)
    outcomes = tuple(
        TimedOutcome(now - timedelta(seconds=index * 20), bad=index < 20)
        for index in range(120)
    )
    report = evaluate_burn_rate(
        outcomes,
        now=now,
        budget=SLOBudget(min_short_samples=10, min_long_samples=50),
    )
    assert report.severity is BurnSeverity.HALTED

    hysteresis = ModeHysteresis(recovery_confirmations=3)
    assert hysteresis.update(OperationalMode.HALTED) is OperationalMode.HALTED
    assert hysteresis.update(OperationalMode.NORMAL) is OperationalMode.HALTED
    assert hysteresis.update(OperationalMode.NORMAL) is OperationalMode.HALTED
    assert hysteresis.update(OperationalMode.NORMAL) is OperationalMode.NORMAL


def test_pipeline_binds_policy_to_packet_integrity_and_verified_backup(tmp_path, raw_factory):
    store = Store(tmp_path / "source.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    event, _ = asyncio.run(pipeline.handle(raw_factory("AAPL 200C TODAY @ 1.00")))

    with sqlite3.connect(store.path) as db:
        row = db.execute(
            "SELECT payload_json FROM operator_decision_packets WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
    assert row is not None
    packet_payload = json.loads(row[0])
    assert packet_payload["policy_fingerprint"] == pipeline.runtime_policy.fingerprint
    assert IntegrityLedger(store.path).verify_database().ok is True

    schema = inspect_schema(store.path)
    assert schema.compatible is True
    backup = create_verified_backup(store.path, tmp_path / "backup.db")
    assert backup.verified is True
    assert backup.source.state_fingerprint == backup.backup.state_fingerprint
    assert backup.source.integrity_head == backup.backup.integrity_head


def test_storage_inspection_and_explicit_checkpoint_are_operator_controlled(tmp_path):
    store = Store(tmp_path / "storage.db")
    snapshot = inspect_storage(store.path)
    assert snapshot.journal_mode.lower() == "wal"
    checkpoint = checkpoint_wal(store.path, "PASSIVE")
    assert checkpoint.mode == "PASSIVE"
    assert checkpoint.busy >= 0


def test_grammar_conformance_and_offline_profile_share_runtime_semantics(raw_factory):
    base = raw_factory("AAPL 200C TODAY @ 1.00", message_id="grammar")
    cases = option_entry_grammar(
        ticker="AAPL",
        strike=Decimal("200"),
        side="CALL",
        expiry_text="TODAY",
        price=Decimal("1.00"),
        expected_contract_key="AAPL|CALL|200|2026-09-08",
    )
    report = evaluate_grammar(base, cases)
    assert report.failures == ()

    profile = profile_messages(
        (base,),
        allowed_author_ids=frozenset({"author"}),
    )
    assert profile.count == 1
    assert len(profile.final_state_fingerprint) == 64


def test_champion_challenger_governance_never_self_promotes():
    candidate = CandidateScore(
        name="candidate",
        labeled_samples=200,
        accuracy=0.99,
        actionable_precision=1.0,
        ambiguity_rate=0.01,
        parser_p95_us=500.0,
        total_p95_us=1500.0,
        surface_kind_changes=0,
        surface_contract_changes=0,
        grammar_action_leaks=0,
        contract_label_errors=0,
        review_effects=2,
        action_escalations_vs_baseline=0,
        downstream_delta=CounterfactualDelta(False, 0, 0, 0),
        fingerprint="a" * 64,
        qualified=True,
        failures=(),
    )
    selective = SelectivePolicy(True, 0.99, 100, 0.5, 0.03, "supported")
    walk = WalkForwardReport((), 100, 0.05, 1.0, 0.02, True, ())
    ready = evaluate_promotion(
        PromotionEvidence(
            candidate,
            selective,
            walk,
            "manifest-hash",
            online_drifted=False,
            dataset_complete=True,
            quote_coverage_ok=True,
            depth_coverage_ok=True,
        )
    )
    blocked = evaluate_promotion(
        PromotionEvidence(
            candidate,
            selective,
            walk,
            "manifest-hash",
            online_drifted=True,
            dataset_complete=True,
            quote_coverage_ok=True,
            depth_coverage_ok=True,
        )
    )
    assert ready.status is PromotionStatus.READY_FOR_OPERATOR_REVIEW
    assert "operator review" in ready.reason
    assert blocked.status is PromotionStatus.BLOCKED
    assert "online_drift_active" in blocked.failures


def test_tournament_rejects_action_escalating_challenger(raw_factory):
    entry = raw_factory("AAPL 200C TODAY @ 1.00", message_id="t1")
    ambiguous = raw_factory("19%", message_id="t2", minute=1)

    def aggressive(raw, allowed):
        parsed = parse_message_with_evidence(raw, allowed)
        if raw.content.strip() == "19%":
            return ParseDecision(replace(parsed.event, kind=EventKind.EXIT), parsed.evidence)
        return parsed

    expected = {
        entry.revision_id: EventKind.ENTRY,
        ambiguous.revision_id: parse_message(ambiguous).kind,
    }
    report = run_parser_tournament(
        (entry, ambiguous),
        (
            TournamentCandidate("baseline", parse_message_with_evidence),
            TournamentCandidate("aggressive", aggressive),
        ),
        expected_kinds=expected,
        allowed_author_ids=frozenset({"author"}),
        criteria=TournamentCriteria(
            min_labeled_samples=2,
            min_accuracy=0.90,
            min_actionable_precision=0.90,
            max_parser_p95_us=1_000_000.0,
            max_total_p95_us=1_000_000.0,
        ),
    )
    challenger = next(score for score in report.scores if score.name == "aggressive")
    assert challenger.qualified is False
    assert challenger.action_escalations_vs_baseline >= 1
