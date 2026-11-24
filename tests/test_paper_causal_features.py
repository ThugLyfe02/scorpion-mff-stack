"""Forward evidence stays causal, immutable and explicitly unlabeled."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scorpion.causal_features import load_paper_intent_features
from scorpion.counterfactual import compare_paper_intent_alternatives, compare_paper_no_chase
from scorpion.execution_attribution import PaperExecutionCostReport, build_paper_execution_cost_report
from scorpion.execution_meta_labels import (
    PaperOutcome,
    PaperOutcomeAdmission,
    PaperOutcomeError,
    PaperOutcomeReport,
    admit_paper_outcome_labels,
)
from scorpion.session_service import Journal, JournalError

T = "2026-11-24T15:00:00+00:00"
T1 = "2026-11-24T15:00:01+00:00"
T2 = "2026-11-24T15:00:02+00:00"
RULES = "a" * 64
EVENT_NS = int(datetime.fromisoformat(T1).timestamp()) * 1_000_000_000


def decision() -> dict[str, object]:
    return {
        "kind": "decision", "action": "schedule_entry", "call_id": "fixture",
        "ticker": "XYZ", "rules_sha256": RULES, "source_ts_utc": T,
        "received_ts_utc": T, "ts_utc": T, "revision_id": "b" * 64,
        "content_sha256": "c" * 64,
    }


def intent(chase: bool = False) -> dict[str, object]:
    return {
        "kind": "intent", "call_id": "fixture", "ticker": "XYZ", "rules_sha256": RULES,
        "mode": "PAPER", "status": "PAPER_INTENT_ONLY", "chase": chase,
        "conditional_on": "initial_unfilled" if chase else None,
        "limit": "10.10" if chase else "10.00", "quantity": 9 if chase else 10,
        "reference_price": "10.00", "ts_utc": T2 if chase else T1,
        "quote_evidence": {
            "bid": "9.99", "ask": "10.10", "observed_ts_utc": T1,
            "ts_event_ns": EVENT_NS, "recv_mono_ns": 100_000_000,
            "decision_mono_ns": 1_100_000_000 if chase else 110_000_000,
            "source_age_ns": 10_000_000,
        },
    }


def seal(path: Path, *records: dict[str, object]) -> Path:
    journal = Journal(path)
    for row in records:
        journal.submit(row)
    journal.close()
    return path


def test_feature_rows_bind_verified_prior_decision_and_never_have_labels(tmp_path: Path) -> None:
    path = seal(tmp_path / "session.jsonl", decision(), intent(), intent(True))
    report = load_paper_intent_features(path)
    assert len(report.rows) == 2
    assert report.rows[0].decision_seq == 1
    assert report.rows[0].intent_seq == 2
    assert report.rows[0].features["quote_age_ns"] == 20_000_000
    assert report.rows[0].label_status == "UNLABELED"
    assert report.labeled_training_rows == 0
    assert report == load_paper_intent_features(path)


def test_counterfactual_is_requested_price_delta_not_profit(tmp_path: Path) -> None:
    path = seal(tmp_path / "session.jsonl", decision(), intent(), intent(True))
    report = compare_paper_intent_alternatives(path)
    assert len(report.pairs) == 1
    assert report.pairs[0].requested_limit_change_bps == Decimal("100")
    assert report.pairs[0].requested_quantity_change == -1
    assert report.pairs[0].modeled_pnl_delta is None


def test_missing_prior_decision_and_future_quote_refused(tmp_path: Path) -> None:
    row = intent()
    quote = dict(row["quote_evidence"])  # type: ignore[arg-type]
    quote["observed_ts_utc"] = "2026-11-25T15:00:00+00:00"
    row["quote_evidence"] = quote
    path = seal(tmp_path / "session.jsonl", intent(), decision(), row, intent(True))
    report = load_paper_intent_features(path)
    assert not report.rows
    assert dict(report.refusals) == {
        "missing_prior_decision": 1, "invalid_intent_evidence": 1,
        "missing_qualified_initial": 1,
    }


def test_tamper_never_enters_research_dataset(tmp_path: Path) -> None:
    path = seal(tmp_path / "session.jsonl", decision(), intent())
    path.write_bytes(path.read_bytes().replace(b"XYZ", b"ABC"))
    with pytest.raises(JournalError, match="receipt_mismatch"):
        load_paper_intent_features(path)


# Synthetic signatures and venue exports are test fixtures, not real external evidence.
COLLECTOR = b"scorpion.paper-outcome-export.v1:collector\x00"
REVIEWER = b"scorpion.paper-outcome-export.v1:reviewer\x00"
THROUGH = "2026-11-24T15:02:00+00:00"
COLLECTED = "2026-11-24T15:02:01+00:00"
REVIEWED = "2026-11-24T15:02:02+00:00"


@dataclass
class OutcomeFixture:
    journal: Path
    trust: Path
    export: Path
    artifact: Path
    trust_hash: str
    collector: Ed25519PrivateKey
    reviewer: Ed25519PrivateKey
    payload: dict

    def admit(self, *, as_of: str = REVIEWED) -> PaperOutcomeReport:
        raw = json.dumps(self.payload, sort_keys=True).encode()
        self.export.write_bytes(raw)
        return admit_paper_outcome_labels(
            self.journal, trust_path=self.trust, expected_trust_sha256=self.trust_hash,
            export_path=self.export, source_artifact_path=self.artifact,
            collector_signature=self.collector.sign(COLLECTOR + raw).hex(),
            reviewer_signature=self.reviewer.sign(REVIEWER + raw).hex(),
            as_of=datetime.fromisoformat(as_of),
        )


def fill(fid: str = "paper-fill-1", quantity: int = 10, price: str = "9.98") -> dict:
    return {"fill_id": fid, "quantity": quantity, "price": price, "filled_ts_utc": T2}


def order_for(row, *, state: str = "FILLED", fills: list[dict] | None = None) -> dict:
    return {
        "feature_sha256": row.feature_sha256, "venue_order_id": "fixture-" + row.role,
        "call_id": row.call_id, "ticker": row.ticker, "role": row.role,
        "intent_seq": row.intent_seq, "requested_quantity": row.features["quantity"],
        "limit": row.features["limit"], "state": state,
        "asset_class": "EQUITY", "side": "BUY", "currency": "USD", "quantity_unit": "SHARES",
        "submitted_ts_utc": row.features["intent_ts_utc"],
        "terminal_ts_utc": T2 if state not in ("WORKING", "PARTIAL", "UNKNOWN") else None,
        "fills": fills if fills is not None else [fill()],
    }


@pytest.fixture
def outcome_fixture(tmp_path: Path) -> OutcomeFixture:
    journal = seal(tmp_path / "journal.jsonl", decision(), intent(), intent(True))
    collector = Ed25519PrivateKey.generate()
    reviewer = Ed25519PrivateKey.generate()
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({
        "schema": "scorpion.paper-outcome-trust.v1", "authority": "PAPER_ONLY",
        "source_id": "synthetic-fixture", "account_ref": "paper-fixture",
        "rules_sha256": RULES, "registered_ts_utc": T, "horizon_seconds": 60,
        "collector_id": "fixture-collector", "reviewer_id": "fixture-reviewer",
        "collector_public_key": collector.public_key().public_bytes_raw().hex(),
        "reviewer_public_key": reviewer.public_key().public_bytes_raw().hex(),
    }))
    trust_hash = hashlib.sha256(trust.read_bytes()).hexdigest()
    artifact = tmp_path / "original-export.json"
    artifact.write_text('{"synthetic_fixture":true}')
    report = load_paper_intent_features(journal)
    payload = {
        "schema": "scorpion.paper-outcome-export.v1", "authority": "PAPER_ONLY",
        "source_id": "synthetic-fixture", "account_ref": "paper-fixture",
        "trust_sha256": trust_hash, "journal_sha256": report.journal_sha256,
        "source_artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "complete_order_export": True, "observed_through_ts_utc": THROUGH,
        "collected_ts_utc": COLLECTED, "reviewed_ts_utc": REVIEWED,
        "orders": [order_for(report.rows[0])],
    }
    return OutcomeFixture(
        journal, trust, tmp_path / "normalized.json", artifact, trust_hash,
        collector, reviewer, payload,
    )


def test_attested_fill_remains_paper_research_and_missing_is_not_zero(outcome_fixture) -> None:
    report = outcome_fixture.admit()
    assert report.labels[0].outcome is PaperOutcome.FULL_FILL
    assert report.labels[0].observed_fill_target == 10
    assert report.labels[0].average_fill_price == Decimal("9.98")
    assert report.labels[1].outcome is PaperOutcome.UNOBSERVED
    assert report.labels[1].filled_quantity is None
    assert report.labels[1].observed_fill_target is None
    assert report.observed_fill_targets == 1
    assert report.authority == "RESEARCH_ONLY"
    assert not report.authorizes_training and not report.authorizes_promotion
    assert not report.certifies_live_fills
    assert report == outcome_fixture.admit()


@pytest.mark.parametrize(
    ("state", "fills", "outcome", "target"),
    [
        ("CANCELLED", [], PaperOutcome.CANCELLED_NO_FILL, 0),
        ("EXPIRED", [], PaperOutcome.EXPIRED_NO_FILL, 0),
        ("REJECTED", [], PaperOutcome.REJECTED, None),
        ("CANCELLED", [fill(quantity=4)], PaperOutcome.PARTIAL_FILL, 4),
        ("WORKING", [], PaperOutcome.CENSORED, None),
        ("PARTIAL", [fill(quantity=4)], PaperOutcome.CENSORED, None),
        ("FILLED", [fill(quantity=4)], PaperOutcome.QUARANTINED, None),
        ("FILLED", [fill(quantity=11)], PaperOutcome.QUARANTINED, None),
        ("FILLED", [fill(price="10.01")], PaperOutcome.QUARANTINED, None),
    ],
)
def test_outcome_taxonomy(outcome_fixture, state, fills, outcome, target) -> None:
    row = load_paper_intent_features(outcome_fixture.journal).rows[0]
    outcome_fixture.payload["orders"] = [order_for(row, state=state, fills=fills)]
    label = outcome_fixture.admit().labels[0]
    assert label.outcome is outcome
    assert label.observed_fill_target == target


def test_horizon_is_not_implicitly_extended(outcome_fixture) -> None:
    order = outcome_fixture.payload["orders"][0]
    order["terminal_ts_utc"] = "2026-11-24T15:01:02+00:00"
    assert outcome_fixture.admit().labels[0].outcome is PaperOutcome.CENSORED
    order["terminal_ts_utc"] = T2
    outcome_fixture.payload["observed_through_ts_utc"] = T2
    assert outcome_fixture.admit().labels[0].outcome is PaperOutcome.CENSORED


def test_label_not_available_before_review(outcome_fixture) -> None:
    with pytest.raises(PaperOutcomeError, match="availability"):
        outcome_fixture.admit(as_of=COLLECTED)


def test_wrong_signature_and_tampered_export_refused(outcome_fixture) -> None:
    fixture = outcome_fixture
    raw = json.dumps(fixture.payload, sort_keys=True).encode()
    fixture.export.write_bytes(raw + b" ")
    with pytest.raises(PaperOutcomeError, match="signature"):
        admit_paper_outcome_labels(
            fixture.journal, trust_path=fixture.trust,
            expected_trust_sha256=fixture.trust_hash, export_path=fixture.export,
            source_artifact_path=fixture.artifact,
            collector_signature=fixture.collector.sign(COLLECTOR + raw).hex(),
            reviewer_signature=fixture.reviewer.sign(REVIEWER + raw).hex(),
            as_of=datetime.fromisoformat(REVIEWED),
        )


def test_trust_pin_and_distinct_keys(outcome_fixture) -> None:
    trust = json.loads(outcome_fixture.trust.read_text())
    trust["reviewer_public_key"] = trust["collector_public_key"]
    outcome_fixture.trust.write_text(json.dumps(trust))
    with pytest.raises(PaperOutcomeError, match="trust_pin"):
        outcome_fixture.admit()
    outcome_fixture.trust_hash = hashlib.sha256(outcome_fixture.trust.read_bytes()).hexdigest()
    with pytest.raises(PaperOutcomeError, match="independent_signers"):
        outcome_fixture.admit()


@pytest.mark.parametrize("field,value", [
    ("authority", "LIVE"),
    ("account_ref", "different-account"),
    ("journal_sha256", "0" * 64),
    ("source_artifact_sha256", "0" * 64),
    ("complete_order_export", False),
])
def test_scope_cannot_be_asserted_by_signature_alone(outcome_fixture, field, value) -> None:
    outcome_fixture.payload[field] = value
    with pytest.raises(PaperOutcomeError, match="scope"):
        outcome_fixture.admit()


def test_source_artifact_cannot_be_changed_after_attestation(outcome_fixture) -> None:
    outcome_fixture.artifact.write_text('{"altered":true}')
    with pytest.raises(PaperOutcomeError, match="scope"):
        outcome_fixture.admit()


def test_duplicate_venue_order_and_fill_ids_are_rejected(outcome_fixture) -> None:
    report = load_paper_intent_features(outcome_fixture.journal)
    first = outcome_fixture.payload["orders"][0]
    second = order_for(report.rows[1], fills=[fill(quantity=9)])
    second["venue_order_id"] = first["venue_order_id"]
    outcome_fixture.payload["orders"].append(second)
    with pytest.raises(PaperOutcomeError, match="duplicate_venue_order"):
        outcome_fixture.admit()
    second["venue_order_id"] = "other"
    with pytest.raises(PaperOutcomeError, match="duplicate_venue_fill"):
        outcome_fixture.admit()


def test_chase_needs_terminal_zero_fill_initial(outcome_fixture) -> None:
    report = load_paper_intent_features(outcome_fixture.journal)
    second = order_for(report.rows[1], fills=[fill("chase-fill", 9, "10.05")])
    outcome_fixture.payload["orders"].append(second)
    conflict = outcome_fixture.admit()
    assert conflict.labels[0].outcome is PaperOutcome.QUARANTINED
    assert conflict.labels[1].outcome is PaperOutcome.QUARANTINED
    assert conflict.observed_fill_targets == 0
    outcome_fixture.payload["orders"][0] = order_for(
        report.rows[0], state="CANCELLED", fills=[],
    )
    verified = outcome_fixture.admit()
    assert verified.labels[0].outcome is PaperOutcome.CANCELLED_NO_FILL
    assert verified.labels[1].outcome is PaperOutcome.FULL_FILL
    assert verified.observed_fill_targets == 2


def test_unmatched_order_is_counted_not_joined(outcome_fixture) -> None:
    outcome_fixture.payload["orders"][0]["feature_sha256"] = "d" * 64
    report = outcome_fixture.admit()
    assert report.extra_feature_ids == ("d" * 64,)
    assert report.observed_fill_targets == 0
    assert all(label.outcome is PaperOutcome.UNOBSERVED for label in report.labels)


@pytest.mark.parametrize("field,value", [
    ("asset_class", "OPTION"), ("side", "SELL"),
    ("currency", "EUR"), ("quantity_unit", "CONTRACTS"),
])
def test_equity_label_refuses_other_asset_side_currency_or_units(
    outcome_fixture, field, value,
) -> None:
    outcome_fixture.payload["orders"][0][field] = value
    with pytest.raises(PaperOutcomeError, match="asset_scope"):
        outcome_fixture.admit()


def test_invalid_order_identity_is_quarantined(outcome_fixture) -> None:
    outcome_fixture.payload["orders"][0]["ticker"] = "DIFFERENT"
    report = outcome_fixture.admit()
    assert report.labels[0].outcome is PaperOutcome.QUARANTINED
    assert report.observed_fill_targets == 0


# Stage 2 uses the same signed fixture and a separately attested market/fee supplement.
COST_COLLECTOR = b"scorpion.paper-cost-export.v1:collector\x00"
COST_REVIEWER = b"scorpion.paper-cost-export.v1:reviewer\x00"
MARK_TIME = "2026-11-24T15:01:31+00:00"
MARK_EVENT = "2026-11-24T15:01:30+00:00"


@dataclass
class CostFixture:
    outcome: OutcomeFixture
    policy: Path
    policy_hash: str
    market: Path
    export: Path
    payload: dict

    def synchronize(self) -> PaperOutcomeAdmission:
        raw = json.dumps(self.outcome.payload, sort_keys=True).encode()
        self.outcome.export.write_bytes(raw)
        self.payload["outcome_export_sha256"] = hashlib.sha256(raw).hexdigest()
        return PaperOutcomeAdmission(
            self.outcome.journal, self.outcome.trust, self.outcome.trust_hash,
            self.outcome.export, self.outcome.artifact,
            self.outcome.collector.sign(COLLECTOR + raw).hex(),
            self.outcome.reviewer.sign(REVIEWER + raw).hex(),
            datetime.fromisoformat(REVIEWED),
        )

    def report(self) -> PaperExecutionCostReport:
        admission = self.synchronize()
        raw = json.dumps(self.payload, sort_keys=True).encode()
        self.export.write_bytes(raw)
        return build_paper_execution_cost_report(
            admission, cost_policy_path=self.policy, expected_policy_sha256=self.policy_hash,
            cost_export_path=self.export, market_artifact_path=self.market,
            collector_signature=self.outcome.collector.sign(COST_COLLECTOR + raw).hex(),
            reviewer_signature=self.outcome.reviewer.sign(COST_REVIEWER + raw).hex(),
        )


def fee_for(order: dict, fees: str = "0.30", rebates: str = "0.05") -> dict:
    return {
        "feature_sha256": order["feature_sha256"],
        "venue_order_id": order["venue_order_id"], "fees_usd": fees, "rebates_usd": rebates,
    }


@pytest.fixture
def cost_fixture(tmp_path: Path, outcome_fixture: OutcomeFixture) -> CostFixture:
    policy = tmp_path / "cost-policy.json"
    policy.write_text(json.dumps({
        "schema": "scorpion.paper-cost-policy.v1", "authority": "RESEARCH_ONLY",
        "trust_sha256": outcome_fixture.trust_hash, "registered_ts_utc": T,
        "horizon_seconds": 90, "max_mark_age_ms": 2000,
        "market_source_id": "synthetic-market-fixture",
        "benchmark": "INITIAL_DECISION_MID", "fee_scope": "ALL_ORDER_CHARGES_AND_REBATES_USD",
    }))
    policy_hash = hashlib.sha256(policy.read_bytes()).hexdigest()
    market = tmp_path / "market-artifact.json"
    market.write_text('{"synthetic_market_fixture":true}')
    features = load_paper_intent_features(outcome_fixture.journal)
    first = features.rows[0]
    payload = {
        "schema": "scorpion.paper-cost-export.v1", "authority": "PAPER_ONLY",
        "trust_sha256": outcome_fixture.trust_hash, "cost_policy_sha256": policy_hash,
        "journal_sha256": features.journal_sha256, "outcome_export_sha256": "",
        "market_source_id": "synthetic-market-fixture",
        "market_artifact_sha256": hashlib.sha256(market.read_bytes()).hexdigest(),
        "complete_fee_export": True, "observed_through_ts_utc": THROUGH,
        "collected_ts_utc": COLLECTED, "reviewed_ts_utc": REVIEWED,
        "fees": [fee_for(outcome_fixture.payload["orders"][0])],
        "marks": [{
            "call_id": first.call_id, "initial_feature_sha256": first.feature_sha256,
            "ticker": first.ticker, "quote_id": "fixture-market-quote",
            "target_ts_utc": MARK_TIME, "event_ts_utc": MARK_EVENT,
            "received_ts_utc": MARK_EVENT, "bid": "10.49", "ask": "10.51",
            "route_closed": True,
        }],
    }
    return CostFixture(
        outcome_fixture, policy, policy_hash, market, tmp_path / "cost-export.json", payload,
    )


def test_full_fill_price_improvement_remains_signed_cost(cost_fixture: CostFixture) -> None:
    report = cost_fixture.report()
    row = report.routes[0]
    assert row.complete and report.complete_routes == 1 and not report.blockers
    assert row.filled_notional_usd == Decimal("99.80")
    assert row.decision_midpoint == Decimal("10.045")
    assert row.within_leg_slippage_usd == Decimal("-0.650")
    assert row.net_fees_usd == Decimal("0.25")
    assert row.opportunity_cost_usd == 0
    assert row.net_shortfall_usd == Decimal("-0.40")
    assert not report.authorizes_training and not report.authorizes_promotion
    assert not report.certifies_live_fills
    assert report == cost_fixture.report()
    with localcontext() as ctx:
        ctx.prec = 12
        assert report == cost_fixture.report()
    comparison = compare_paper_no_chase(report)
    assert comparison.comparable_routes == 0
    assert comparison.comparable_delta_usd is None


def test_partial_fill_keeps_original_parent_target(cost_fixture: CostFixture) -> None:
    first = load_paper_intent_features(cost_fixture.outcome.journal).rows[0]
    order = order_for(first, state="CANCELLED", fills=[fill(quantity=4)])
    cost_fixture.outcome.payload["orders"] = [order]
    row = cost_fixture.report().routes[0]
    assert row.target_quantity == 10 and row.filled_quantity == 4
    assert row.within_leg_slippage_usd == Decimal("-0.26")
    assert row.opportunity_cost_usd == Decimal("2.73")
    assert row.net_shortfall_usd == Decimal("2.72")


def setup_chase(cost_fixture: CostFixture, *, filled: bool = True) -> None:
    rows = load_paper_intent_features(cost_fixture.outcome.journal).rows
    first = order_for(rows[0], state="CANCELLED", fills=[])
    chase = order_for(rows[1], state="FILLED" if filled else "CANCELLED",
                      fills=[fill("chase-fixture", 9, "10.05")] if filled else [])
    cost_fixture.outcome.payload["orders"] = [first, chase]
    cost_fixture.payload["fees"] = [fee_for(first, "0.10", "0"), fee_for(chase)]


def test_observed_chase_compares_fixed_target_not_hypothetical_fill(
    cost_fixture: CostFixture,
) -> None:
    setup_chase(cost_fixture)
    report = cost_fixture.report()
    row = report.routes[0]
    assert row.complete and row.target_quantity == 10 and row.filled_quantity == 9
    assert row.filled_notional_usd == Decimal("90.45")
    assert row.net_shortfall_usd == Decimal("0.85")
    comparison = compare_paper_no_chase(report)
    assert comparison.comparable_routes == 1
    pair = comparison.comparisons[0]
    assert pair.no_chase_reference_shortfall_usd == Decimal("4.65")
    assert pair.observed_minus_no_chase_usd == Decimal("-3.80")
    assert pair.modeled_pnl_delta is None
    assert not comparison.authorizes_promotion


def test_zero_fill_chase_quantifies_fees_not_fictitious_returns(cost_fixture: CostFixture) -> None:
    setup_chase(cost_fixture, filled=False)
    report = cost_fixture.report()
    row = report.routes[0]
    assert row.filled_quantity == 0 and row.chase_order_observed
    assert row.net_shortfall_usd == Decimal("4.90")
    assert compare_paper_no_chase(report).comparable_delta_usd == Decimal("0.25")


def test_missing_chase_after_zero_fill_is_unknown(cost_fixture: CostFixture) -> None:
    first = load_paper_intent_features(cost_fixture.outcome.journal).rows[0]
    cost_fixture.outcome.payload["orders"] = [order_for(first, state="CANCELLED", fills=[])]
    report = cost_fixture.report()
    assert report.routes[0].reason == "CHASE_ROUTE_UNOBSERVED"
    assert report.complete_route_net_shortfall_usd is None
    assert compare_paper_no_chase(report).comparable_delta_usd is None


def test_missing_fee_preserves_gross_but_withholds_net(cost_fixture: CostFixture) -> None:
    cost_fixture.payload["fees"] = []
    report = cost_fixture.report()
    assert report.orders[0].gross_slippage_usd == Decimal("-0.65")
    assert report.orders[0].net_filled_cost_usd is None
    assert report.routes[0].net_shortfall_usd is None


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("event_ts_utc", "2026-11-24T15:01:28+00:00", "NONCAUSAL_OR_STALE_HORIZON_MARK"),
    ("received_ts_utc", "2026-11-24T15:01:32+00:00", "NONCAUSAL_OR_STALE_HORIZON_MARK"),
    ("bid", "11", "NONCAUSAL_OR_STALE_HORIZON_MARK"),
    ("target_ts_utc", THROUGH, "ROUTE_CLOSURE_OR_MARK_UNAVAILABLE"),
    ("route_closed", False, "ROUTE_CLOSURE_OR_MARK_UNAVAILABLE"),
    ("ticker", "OTHER", "ROUTE_CLOSURE_OR_MARK_UNAVAILABLE"),
])
def test_mark_must_be_causal_fresh_and_bound(cost_fixture, field, value, reason) -> None:
    cost_fixture.payload["marks"][0][field] = value
    report = cost_fixture.report()
    assert report.routes[0].reason == reason
    assert report.complete_routes == 0


def test_cost_pin_and_independent_market_artifact(cost_fixture: CostFixture) -> None:
    cost_fixture.policy.write_bytes(cost_fixture.policy.read_bytes() + b" ")
    with pytest.raises(PaperOutcomeError, match="cost_policy_pin"):
        cost_fixture.report()
    cost_fixture.policy.write_bytes(cost_fixture.policy.read_bytes()[:-1])
    cost_fixture.market.write_text('{"changed":true}')
    with pytest.raises(PaperOutcomeError, match="cost_export_scope"):
        cost_fixture.report()


def test_duplicate_fee_and_mark_identity_refuse(cost_fixture: CostFixture) -> None:
    cost_fixture.payload["fees"].append(cost_fixture.payload["fees"][0])
    with pytest.raises(PaperOutcomeError, match="duplicate_cost_fee"):
        cost_fixture.report()
    cost_fixture.payload["fees"].pop()
    cost_fixture.payload["marks"].append(cost_fixture.payload["marks"][0])
    with pytest.raises(PaperOutcomeError, match="duplicate_cost_route"):
        cost_fixture.report()


def test_cost_supplement_signature_domain_cannot_be_substituted(cost_fixture: CostFixture) -> None:
    admission = cost_fixture.synchronize()
    raw = json.dumps(cost_fixture.payload, sort_keys=True).encode()
    cost_fixture.export.write_bytes(raw)
    with pytest.raises(PaperOutcomeError, match="signature"):
        build_paper_execution_cost_report(
            admission, cost_policy_path=cost_fixture.policy,
            expected_policy_sha256=cost_fixture.policy_hash, cost_export_path=cost_fixture.export,
            market_artifact_path=cost_fixture.market,
            collector_signature=cost_fixture.outcome.collector.sign(COLLECTOR + raw).hex(),
            reviewer_signature=cost_fixture.outcome.reviewer.sign(REVIEWER + raw).hex(),
        )


def test_extra_fee_blocks_aggregate_not_just_row(cost_fixture: CostFixture) -> None:
    cost_fixture.payload["fees"].append({
        "feature_sha256": "f" * 64, "venue_order_id": "unknown",
        "fees_usd": "0", "rebates_usd": "0",
    })
    report = cost_fixture.report()
    assert "unexpected_or_mismatched_fee" in report.blockers
    assert report.complete_route_net_shortfall_usd is None


def test_stage1_exact_fill_notional_and_label_identity(cost_fixture: CostFixture) -> None:
    first = load_paper_intent_features(cost_fixture.outcome.journal).rows[0]
    order = order_for(first, state="CANCELLED",
                      fills=[fill("one", 1, "9.99"), fill("two", 2, "9.98")])
    cost_fixture.outcome.payload["orders"] = [order]
    report = cost_fixture.outcome.admit()
    with localcontext() as ctx:
        ctx.prec = 50
        assert cost_fixture.outcome.admit() == report
    assert report.labels[0].filled_notional_usd == Decimal("29.95")
