"""Synthetic evidence and adversarial tests for Stage 3; no market claims."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scorpion.causal_features import load_paper_intent_features
from scorpion.confidence_sequence import anytime_bounded_mean_confidence_sequence
from scorpion.cost_adjusted_policy_value import session_mean_diagnostics
from scorpion.exec_cli import main
from scorpion.paper_qualification import (
    PaperQualificationError,
    _root,
    _study,
    assess_paper_study,
    common_roster_digest,
    execution_binding_digest,
    finalize_paper_study,
    reserve_paper_study,
)
from scorpion.paper_session_statistics import (
    PaperCohortError,
    PaperCohortPolicy,
    _bounds,
    _Day,
    _evaluate_days,
    _RouteScore,
    evaluate_paper_session_cohort,
    load_verified_paper_cohort,
)
from scorpion.research_hypothesis_memory import (
    prospective_registry_status,
    read_finalized_paper_dossier,
    verify_hypothesis_memory,
)
from scorpion.session_calendar import session_hours
from scorpion.session_service import Journal
from scorpion.walk_forward import FrozenValidationWindow, verify_frozen_validation_windows

DAYS = tuple(date.fromisoformat(v) for v in (
    "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27",
))
REGISTERED = datetime(2026, 11, 20, 20, tzinfo=UTC)
AS_OF = datetime(2026, 11, 28, 1, tzinfo=UTC)
RULES = "a" * 64
COLLECTOR = b"scorpion.paper-outcome-export.v1:collector\x00"
REVIEWER = b"scorpion.paper-outcome-export.v1:reviewer\x00"
COST_COLLECTOR = b"scorpion.paper-cost-export.v1:collector\x00"
COST_REVIEWER = b"scorpion.paper-cost-export.v1:reviewer\x00"


def put(path: Path, payload: object) -> str:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stamp(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)


def raw_policy(trust_hash: str, cost_hash: str) -> dict:
    return {
        "schema": "scorpion.paper-session-cohort.v1",
        "analysis_version": "paper-session-statistics-v1",
        "authority": "RESEARCH_ONLY", "family_id": "fixture-family",
        "registered_ts_utc": REGISTERED.isoformat(),
        "start_date": DAYS[0].isoformat(), "end_date": DAYS[-1].isoformat(),
        "sessions": [v.isoformat() for v in DAYS], "trust_sha256": trust_hash,
        "cost_policy_sha256": cost_hash, "family_alpha": 0.05, "clip_bps": 100,
        "minimum_sessions": 3, "minimum_routes": 2, "minimum_complete_fraction": 0.8,
        "minimum_effective_sessions": 2, "maximum_session_share": 0.6,
        "maximum_ticker_share": 0.8, "minimum_primary_score_bps": 0,
        "minimum_chase_score_bps": 0, "minimum_observed_chases": 1,
        "maximum_tail_loss_bps": 50, "minimum_group_sessions": 2,
        "density_ratio_bound": 2, "block_days": 2, "bootstrap_trials": 1000,
        "random_seed": 42,
    }


def policy() -> PaperCohortPolicy:
    return PaperCohortPolicy(
        "fixture-family", REGISTERED, DAYS[0], DAYS[-1], DAYS, "a" * 64, "b" * 64,
        0.05, 100, 3, 2, 0.8, 2, 0.6, 0.8, 0, 0, 1, 50, 2, 2, 2, 1000, 42,
    )


def score(key: str, primary: float | None, chase: float | None,
          ticker: str = "AAA", group: str = "TIGHT") -> _RouteScore:
    return _RouteScore(
        key, ticker, (("spread", group), ("quote_age", "FRESH"),
                      ("decision_latency", "FAST"), ("session", "OPEN")),
        primary, chase, chase is not None and chase != 0,
    )


def test_bounded_sequence_unknown_support_and_alpha() -> None:
    report = anytime_bounded_mean_confidence_sequence(
        [(20, 20), (-100, 100)], lower=-100, upper=100, alpha=0.025,
    )
    assert report.samples == 2 and report.fully_observed == 1
    assert report.interval_mean_lower == -40 and report.interval_mean_upper == 60
    assert report.lower_bound <= -40 and report.upper_bound >= 60
    assert report.radius == pytest.approx(200 * math.sqrt(math.log(2 * 2 * 3 / 0.025) / 4))
    empty = anytime_bounded_mean_confidence_sequence([], lower=-100, upper=100)
    assert empty.interval_mean_lower is None and empty.lower_bound == -100
    assert empty.upper_bound == 100
    with pytest.raises(ValueError):
        anytime_bounded_mean_confidence_sequence([(0, float("nan"))], lower=-100, upper=100)
    with pytest.raises(ValueError):
        anytime_bounded_mean_confidence_sequence([(0, 101)], lower=-100, upper=100)


def test_session_bootstrap_is_deterministic_and_not_gap_imputed() -> None:
    data = [10, -5, 20, -10, 15, 5]
    first = session_mean_diagnostics(data, block_days=2, trials=1000, random_seed=9)
    assert first == session_mean_diagnostics(data, block_days=2, trials=1000, random_seed=9)
    assert first.bootstrap_lower is not None and first.bootstrap_upper is not None
    assert first.leave_one_out_min == pytest.approx((sum(data) - max(data)) / 5)
    assert first.lower_tail_mean == -10
    gap = session_mean_diagnostics(data, block_days=2, contiguous=False)
    assert gap.bootstrap_lower is None and gap.lag_one_correlation is None
    constant = session_mean_diagnostics([5] * 20, block_days=2)
    assert constant.bootstrap_lower is None and constant.lag_one_correlation is None


def test_missing_censored_and_explicit_empty_are_not_the_same() -> None:
    days = (
        _Day(DAYS[0], "COVERED_COMPLETE", 1, (score("a", 20, 0),), "a", ()),
        _Day(DAYS[1], "COVERED_CENSORED", 2,
             (score("b", 40, 10), score("c", None, None)), "b", ()),
        _Day(DAYS[2], "COVERED_EMPTY", 0, (), "c", ()),
        _Day(DAYS[3], "MISSING_SESSION", None, (), None, ("missing",)),
    )
    assert _bounds(days[1], "primary", 100) == (-30, 70)
    assert _bounds(days[2], "primary", 100) == (0, 0)
    assert _bounds(days[3], "primary", 100) == (-100, 100)
    result = _evaluate_days(days, policy(), "a" * 64, AS_OF)
    assert result.primary.identification_interval_bps == (-27.5, 47.5)
    assert result.primary.raw_route_mean_bps == 30
    assert result.primary.complete_session_diagnostics.mean == 10
    assert result.primary.complete_session_diagnostics.bootstrap_lower is None
    assert result.primary.anytime_conditional_mean.alpha == 0.025
    assert result.chase.anytime_conditional_mean.alpha == 0.025
    assert result.known_expected_routes == 3 and result.observed_primary_routes == 2
    assert result.missing_or_invalid_sessions == 1
    assert result.observed_route_coverage == pytest.approx(2 / 3)
    assert "insufficient_verified_session_coverage" in result.primary.blockers
    assert not result.authorizes_training and not result.authorizes_promotion
    assert not result.certifies_live_fills


def test_raw_extreme_is_retained_and_blocks_support() -> None:
    days = tuple(_Day(day, "COVERED_COMPLETE", 1, (
        score(str(i), -250 if i == 0 else 90, 10, "AAA" if i == 0 else "BBB"),
    ), str(i), ()) for i, day in enumerate(DAYS))
    result = _evaluate_days(days, policy(), "a" * 64, AS_OF)
    assert result.primary.clipped_routes == 1
    assert result.primary.raw_worst_route_bps == -250
    assert result.primary.raw_lower_tail_mean_bps == -250
    assert "unclipped_tail_exceeds_preregistered_support" in result.primary.blockers
    assert "raw_route_tail_loss_exceeds_limit" in result.primary.blockers


def test_concentration_and_regime_mixture_not_hidden() -> None:
    days = tuple(_Day(day, "COVERED_COMPLETE", 2, (
        score(f"{i}a", 20, 5, "AAA", "TIGHT"), score(f"{i}b", -10, 1, "BBB", "WIDE"),
    ), str(i), ()) for i, day in enumerate(DAYS))
    result = _evaluate_days(days, policy(), "a" * 64, AS_OF)
    assert result.kish_effective_session_count == 4
    assert result.maximum_observed_session_share == 0.25
    assert result.maximum_observed_ticker_share == 0.5
    spread = next(axis for axis in result.primary_regimes if axis.axis == "spread")
    assert spread.worst_case_observed_mixture_bps == pytest.approx(-2.5)
    assert dict(spread.worst_case_weights) == pytest.approx({"TIGHT": 0.25, "WIDE": 0.75})
    assert all(g.observed_sessions == 4 for g in spread.groups)
    assert all(g.pooled_session_mean_bps is not None for g in spread.groups)
    assert result.regime_robustness == "ADVERSE_DESCRIPTIVE_STRESS"
    concentrated = replace(policy(), maximum_ticker_share=0.4)
    assert "ticker_concentration" in _evaluate_days(
        days, concentrated, "a" * 64, AS_OF,
    ).primary.blockers


@dataclass
class SignedBundle:
    folder: Path
    row: dict
    outcome: dict
    costs: dict


@dataclass
class SignedCohort:
    root: Path
    trust: Path
    cost_policy: Path
    cohort: Path
    inputs: Path
    pin: str
    trust_hash: str
    cost_hash: str
    collector: Ed25519PrivateKey
    reviewer: Ed25519PrivateKey
    bundles: list[SignedBundle]
    rules: str = RULES
    account: str = "fixture-account"

    def sign(self, bundle: SignedBundle) -> None:
        raw = json.dumps(bundle.outcome, sort_keys=True).encode()
        (bundle.folder / "outcome.json").write_bytes(raw)
        (bundle.folder / "collector.sig").write_text(self.collector.sign(COLLECTOR + raw).hex())
        (bundle.folder / "reviewer.sig").write_text(self.reviewer.sign(REVIEWER + raw).hex())
        bundle.costs["outcome_export_sha256"] = hashlib.sha256(raw).hexdigest()
        raw = json.dumps(bundle.costs, sort_keys=True).encode()
        (bundle.folder / "costs.json").write_bytes(raw)
        (bundle.folder / "cost-collector.sig").write_text(
            self.collector.sign(COST_COLLECTOR + raw).hex(),
        )
        (bundle.folder / "cost-reviewer.sig").write_text(
            self.reviewer.sign(COST_REVIEWER + raw).hex(),
        )

    def index(self, bundles: list[SignedBundle] | None = None) -> None:
        rows = self.bundles if bundles is None else bundles
        put(self.inputs, {
            "schema": "scorpion.paper-session-inputs.v1", "sessions": [b.row for b in rows],
        })

    def report(self, as_of: datetime = AS_OF):
        return evaluate_paper_session_cohort(
            cohort_path=self.cohort, expected_cohort_sha256=self.pin, inputs_path=self.inputs,
            trust_path=self.trust, cost_policy_path=self.cost_policy, as_of=as_of,
        )


def build_bundle(
    ctx: SignedCohort, day: date, *, mode: str, disconnects: int = 0,
    include_markers: bool = True, armed: bool = True, ingress_ready: bool = True,
    safety_changed: bool = False,
) -> SignedBundle:
    root = ctx.root / day.isoformat()
    root.mkdir()
    journal_path = root / "journal.jsonl"
    journal = Journal(journal_path)
    hours = session_hours(day)
    assert hours is not None
    open_at, close_at = (v.astimezone(UTC) for v in hours)
    source = stamp(day, 15)
    first = source + timedelta(seconds=1)
    second = source + timedelta(seconds=2)
    horizon = first + timedelta(seconds=90)
    end = close_at + timedelta(minutes=5)
    through = end + timedelta(seconds=1)
    collected = through + timedelta(seconds=1)
    reviewed = collected + timedelta(seconds=1)
    ticker = "AAA" if day == DAYS[0] else "BBB"
    call = f"call-{day.isoformat()}"
    common = {"session_date": day.isoformat(), "mode": "PAPER", "rules_sha256": ctx.rules}
    if include_markers:
        journal.submit({"kind": "session_start", **common,
                        "ts_utc": (open_at - timedelta(seconds=1)).isoformat(),
                        "signal_ingress_enabled": True, "latch_armed": armed,
                        "safety_integrity_valid": True, "safety_event_count": 2})
    if mode != "empty":
        journal.submit({
            "kind": "decision", "action": "schedule_entry", "call_id": call,
            "ticker": ticker, "rules_sha256": ctx.rules, "source_ts_utc": source.isoformat(),
            "received_ts_utc": source.isoformat(), "ts_utc": source.isoformat(),
            "revision_id": hashlib.sha256(call.encode()).hexdigest(),
            "content_sha256": "c" * 64,
        })
        for chase, moment in [(False, first)] + ([(True, second)] if mode == "chase" else []):
            journal.submit({
                "kind": "intent", "call_id": call, "ticker": ticker, "rules_sha256": ctx.rules,
                "mode": "PAPER", "status": "PAPER_INTENT_ONLY", "chase": chase,
                "conditional_on": "initial_unfilled" if chase else None,
                "limit": "10.10" if chase else "10.00", "quantity": 9 if chase else 10,
                "reference_price": "10.00", "ts_utc": moment.isoformat(),
                "quote_evidence": {
                    "bid": "9.99", "ask": "10.10", "observed_ts_utc": moment.isoformat(),
                    "ts_event_ns": int(moment.timestamp()) * 1_000_000_000,
                    "recv_mono_ns": 100_000_000, "decision_mono_ns": 110_000_000,
                    "source_age_ns": 10_000_000,
                },
            })
    if include_markers:
        journal.submit({
            "kind": "session_end", **common, "ts_utc": end.isoformat(),
            "feed_disconnects": disconnects, "feed_messages": 100,
            "ingress_overflow": False, "pending_ingress": 0, "latch_armed": armed,
            "safety_integrity_valid": True, "safety_event_count": 4 if safety_changed else 2,
            "feed_first_usable_recv_wall_ns": int(open_at.timestamp()) * 1_000_000_000,
            "ingress_scope_ready_ts_utc": open_at.isoformat() if ingress_ready else None,
            "ingress_first_disconnect_ts_utc": None, "ingress_callback_failures": 0,
        })
    journal.close()
    features = load_paper_intent_features(journal_path)
    orders, fees, marks = [], [], []
    for row in features.rows:
        chase = row.role == "CHASE"
        state = "CANCELLED" if mode == "chase" and not chase else "FILLED"
        if mode == "working":
            state = "WORKING"
        oid = f"order-{day}-{row.role}"
        fill_rows = ([{"fill_id": f"fill-{day}-{row.role}",
                       "quantity": row.features["quantity"], "price": "10.05" if chase else "9.98",
                       "filled_ts_utc": second.isoformat()}] if state == "FILLED" else [])
        orders.append({
            "feature_sha256": row.feature_sha256, "venue_order_id": oid,
            "call_id": call, "ticker": ticker, "role": row.role, "intent_seq": row.intent_seq,
            "requested_quantity": row.features["quantity"], "limit": row.features["limit"],
            "state": state, "asset_class": "EQUITY", "side": "BUY", "currency": "USD",
            "quantity_unit": "SHARES", "submitted_ts_utc": row.features["intent_ts_utc"],
            "terminal_ts_utc": None if state == "WORKING" else second.isoformat(),
            "fills": fill_rows,
        })
        fees.append({"feature_sha256": row.feature_sha256, "venue_order_id": oid,
                     "fees_usd": "0.1" if state == "CANCELLED" else "0.30",
                     "rebates_usd": "0" if state == "CANCELLED" else "0.05"})
        if not chase:
            marks.append({
                "call_id": call, "initial_feature_sha256": row.feature_sha256,
                "ticker": ticker, "quote_id": f"quote-{day}", "target_ts_utc": horizon.isoformat(),
                "event_ts_utc": (horizon - timedelta(seconds=1)).isoformat(),
                "received_ts_utc": (horizon - timedelta(seconds=1)).isoformat(),
                "bid": "10.49", "ask": "10.51", "route_closed": True,
            })
    venue_hash = put(root / "venue.json", {
        "synthetic": "venue", "date": day.isoformat(), "account": ctx.account,
    })
    market_hash = put(root / "market.json", {"synthetic": "market", "date": day.isoformat()})
    outcome = {
        "schema": "scorpion.paper-outcome-export.v1", "authority": "PAPER_ONLY",
        "source_id": "fixture-venue", "account_ref": ctx.account,
        "trust_sha256": ctx.trust_hash, "journal_sha256": features.journal_sha256,
        "source_artifact_sha256": venue_hash, "complete_order_export": True,
        "observed_through_ts_utc": through.isoformat(),
        "collected_ts_utc": collected.isoformat(), "reviewed_ts_utc": reviewed.isoformat(),
        "orders": orders,
    }
    costs = {
        "schema": "scorpion.paper-cost-export.v1", "authority": "PAPER_ONLY",
        "trust_sha256": ctx.trust_hash, "cost_policy_sha256": ctx.cost_hash,
        "journal_sha256": features.journal_sha256, "outcome_export_sha256": "",
        "market_source_id": "fixture-market", "market_artifact_sha256": market_hash,
        "complete_fee_export": True, "observed_through_ts_utc": through.isoformat(),
        "collected_ts_utc": collected.isoformat(), "reviewed_ts_utc": reviewed.isoformat(),
        "fees": fees, "marks": marks,
        "session_coverage": {
            "session_date": day.isoformat(), "covered_from_ts_utc": open_at.isoformat(),
            "covered_through_ts_utc": close_at.isoformat(),
            "expected_initial_routes": int(mode != "empty"), "journal_complete": True,
            "ingress_complete": True, "no_feed_gaps": True,
        },
    }
    names = {"journal": "journal.jsonl", "outcome_export": "outcome.json",
             "source_artifact": "venue.json", "collector_signature": "collector.sig",
             "reviewer_signature": "reviewer.sig", "cost_export": "costs.json",
             "market_artifact": "market.json", "cost_collector_signature": "cost-collector.sig",
             "cost_reviewer_signature": "cost-reviewer.sig"}
    row = {"session_date": day.isoformat(), **{key: str(root / name) for key, name in names.items()}}
    bundle = SignedBundle(root, row, outcome, costs)
    ctx.sign(bundle)
    return bundle


def make_cohort(
    tmp_path: Path, *, rules: str = RULES, account: str = "fixture-account",
) -> SignedCohort:
    tmp_path.mkdir(parents=True, exist_ok=True)
    collector = Ed25519PrivateKey.generate()
    reviewer = Ed25519PrivateKey.generate()
    trust = tmp_path / "trust.json"
    trust_hash = put(trust, {
        "schema": "scorpion.paper-outcome-trust.v1", "authority": "PAPER_ONLY",
        "source_id": "fixture-venue", "account_ref": account, "rules_sha256": rules,
        "registered_ts_utc": REGISTERED.isoformat(), "horizon_seconds": 60,
        "collector_id": "collector", "reviewer_id": "reviewer",
        "collector_public_key": collector.public_key().public_bytes_raw().hex(),
        "reviewer_public_key": reviewer.public_key().public_bytes_raw().hex(),
    })
    cost_policy = tmp_path / "cost-policy.json"
    cost_hash = put(cost_policy, {
        "schema": "scorpion.paper-cost-policy.v1", "authority": "RESEARCH_ONLY",
        "trust_sha256": trust_hash, "registered_ts_utc": REGISTERED.isoformat(),
        "horizon_seconds": 90, "max_mark_age_ms": 2000, "market_source_id": "fixture-market",
        "benchmark": "INITIAL_DECISION_MID", "fee_scope": "ALL_ORDER_CHARGES_AND_REBATES_USD",
    })
    cohort = tmp_path / "cohort.json"
    pin = put(cohort, raw_policy(trust_hash, cost_hash))
    ctx = SignedCohort(tmp_path, trust, cost_policy, cohort, tmp_path / "inputs.json",
                       pin, trust_hash, cost_hash, collector, reviewer, [], rules, account)
    ctx.bundles.extend([
        build_bundle(ctx, DAYS[0], mode="full"), build_bundle(ctx, DAYS[1], mode="chase"),
        build_bundle(ctx, DAYS[2], mode="empty"),
    ])
    ctx.index()
    return ctx


@pytest.fixture
def signed_cohort(tmp_path: Path) -> SignedCohort:
    return make_cohort(tmp_path)


def test_signed_cohort_keeps_calendar_and_missing_day(signed_cohort: SignedCohort) -> None:
    result = signed_cohort.report()
    assert result.planned_sessions == result.elapsed_sessions == 4
    assert result.complete_sessions == 3
    assert result.known_expected_routes == 2
    assert result.observed_primary_routes == 2 and result.observed_terminal_chase_routes == 1
    assert [v.status for v in result.sessions] == [
        "COVERED_COMPLETE", "COVERED_COMPLETE", "COVERED_EMPTY", "MISSING_SESSION",
    ]
    assert result.sessions[2].primary_bounds_bps == (0, 0)
    assert result.sessions[3].primary_bounds_bps == (-100, 100)
    assert result.sessions[1].chase_raw_mean_bps is not None
    assert result.sessions[1].chase_raw_mean_bps > 0
    assert result.primary.anytime_conditional_mean.samples == 4
    assert result.primary.anytime_conditional_mean.fully_observed == 3
    signed_cohort.index(list(reversed(signed_cohort.bundles)))
    assert result == signed_cohort.report()


def test_valid_early_close_empty_is_not_missing(signed_cohort: SignedCohort) -> None:
    early = build_bundle(signed_cohort, DAYS[3], mode="empty")
    signed_cohort.bundles.append(early)
    signed_cohort.index()
    result = signed_cohort.report()
    assert result.sessions[-1].status == "COVERED_EMPTY"
    assert result.sessions[-1].primary_bounds_bps == (0, 0)


def test_transport_gap_or_absent_lifecycle_invalidates_coverage(signed_cohort: SignedCohort) -> None:
    gap = build_bundle(signed_cohort, DAYS[3], mode="empty", disconnects=1)
    signed_cohort.bundles.append(gap)
    signed_cohort.index()
    result = signed_cohort.report()
    assert result.sessions[-1].status == "COVERAGE_UNVERIFIED"
    assert "journal_session_transport_incomplete" in result.sessions[-1].reasons
    b = signed_cohort.bundles[0]
    del b.costs["session_coverage"]
    signed_cohort.sign(b)
    assert signed_cohort.report().sessions[0].expected_routes is None


def test_signed_but_wrong_route_count_is_unknown(signed_cohort: SignedCohort) -> None:
    b = signed_cohort.bundles[0]
    b.costs["session_coverage"]["expected_initial_routes"] = 0
    signed_cohort.sign(b)
    result = signed_cohort.report()
    assert result.sessions[0].status == "COVERAGE_UNVERIFIED"
    assert "coverage_route_count_mismatch" in result.sessions[0].reasons


def test_tampered_signature_is_invalid_not_zero(signed_cohort: SignedCohort) -> None:
    b = signed_cohort.bundles[0]
    (b.folder / "reviewer.sig").write_text("0" * 128)
    result = signed_cohort.report()
    assert result.sessions[0].status == "INVALID_SESSION"
    assert result.sessions[0].primary_bounds_bps == (-100, 100)


def test_cross_session_order_and_fill_reuse_refused(signed_cohort: SignedCohort) -> None:
    a, b = signed_cohort.bundles[:2]
    old = b.outcome["orders"][0]["venue_order_id"]
    b.outcome["orders"][0]["venue_order_id"] = a.outcome["orders"][0]["venue_order_id"]
    b.costs["fees"][0]["venue_order_id"] = a.outcome["orders"][0]["venue_order_id"]
    signed_cohort.sign(b)
    with pytest.raises(PaperCohortError, match="cross_session_venue_order"):
        signed_cohort.report()
    b.outcome["orders"][0]["venue_order_id"] = old
    b.costs["fees"][0]["venue_order_id"] = old
    b.outcome["orders"][1]["fills"][0]["fill_id"] = a.outcome["orders"][0]["fills"][0]["fill_id"]
    signed_cohort.sign(b)
    with pytest.raises(PaperCohortError, match="cross_session_venue_fill"):
        signed_cohort.report()


def test_roster_pin_future_data_and_registration(signed_cohort: SignedCohort) -> None:
    with pytest.raises(PaperCohortError, match="outside_elapsed"):
        signed_cohort.report(stamp(DAYS[0], 22))
    signed_cohort.index(signed_cohort.bundles[:1])
    prefix = signed_cohort.report(stamp(DAYS[0], 22))
    assert prefix.elapsed_sessions == 1 and prefix.future_sessions == 3
    original = json.loads(signed_cohort.cohort.read_text())
    altered = dict(original, sessions=original["sessions"][:-1])
    put(signed_cohort.cohort, altered)
    with pytest.raises(PaperCohortError, match="pin"):
        signed_cohort.report()
    signed_cohort.pin = put(signed_cohort.cohort, altered)
    with pytest.raises(PaperCohortError, match="calendar_membership"):
        signed_cohort.report()
    signed_cohort.pin = put(signed_cohort.cohort, dict(
        original, registered_ts_utc=stamp(DAYS[0], 15).isoformat(),
    ))
    with pytest.raises(PaperCohortError, match="not_prospective"):
        signed_cohort.report()


def test_cli_nonzero_without_evidence_support(signed_cohort: SignedCohort, capsys) -> None:
    rc = main([
        "paper-session-statistics", "--cohort", str(signed_cohort.cohort),
        "--cohort-sha256", signed_cohort.pin, "--inputs", str(signed_cohort.inputs),
        "--trust", str(signed_cohort.trust), "--cost-policy", str(signed_cohort.cost_policy),
        "--as-of", AS_OF.isoformat(),
    ])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["authority"] == "RESEARCH_ONLY"
    assert report["primary"]["status"] == "NOT_QUALIFIED"


@pytest.mark.parametrize(("armed", "ready", "reason"), [
    (False, True, "journal_session_latch_not_armed"),
    (True, False, "journal_session_readiness_incomplete"),
])
def test_empty_session_requires_operational_readiness(
    signed_cohort: SignedCohort, armed: bool, ready: bool, reason: str,
) -> None:
    bundle = build_bundle(signed_cohort, DAYS[3], mode="empty", armed=armed,
                          ingress_ready=ready)
    signed_cohort.bundles.append(bundle)
    signed_cohort.index()
    result = signed_cohort.report()
    assert result.sessions[-1].expected_routes is None
    assert result.sessions[-1].primary_bounds_bps == (-100, 100)
    assert reason in result.sessions[-1].reasons


def test_chase_concentration_not_diluted_by_structural_zero_routes() -> None:
    days = tuple(_Day(day, "COVERED_COMPLETE", 2, (
        score(f"{i}a", 20, 40 if i == 0 else 0, "AAA"),
        score(f"{i}b", 10, 0, "BBB"),
    ), str(i), ()) for i, day in enumerate(DAYS))
    result = _evaluate_days(days, policy(), "a" * 64, AS_OF)
    assert result.primary_concentration.effective_session_count == 4
    assert result.chase_intervention_concentration.effective_session_count == 1
    assert result.chase_intervention_concentration.maximum_session_share == 1
    assert "chase_intervention_insufficient_kish_session_diversity" in result.chase.blockers
    assert "chase_intervention_session_concentration" in result.chase.blockers
    assert "chase_intervention_ticker_concentration" in result.chase.blockers


def test_preregistered_resolution_and_internal_denominator_guard() -> None:
    days = (_Day(DAYS[0], "COVERED_COMPLETE", 1, (score("a", 20, 0),), "a", ()),)
    result = _evaluate_days(days, policy(), "a" * 64, AS_OF)
    assert result.primary.planned_horizon_radius_bps > 0
    assert result.primary.best_case_planned_lower_bound_bps <= 0
    assert "preregistered_horizon_cannot_resolve_threshold_even_at_upper_support" in (
        result.primary.blockers
    )
    with pytest.raises(PaperCohortError, match="denominator"):
        _Day(DAYS[0], "COVERED_COMPLETE", 2, (score("a", 20, 0),), "a", ())
    with pytest.raises(PaperCohortError, match="unknown_session"):
        _Day(DAYS[0], "MISSING_SESSION", None, (score("a", 20, 0),), None, ())


@pytest.mark.parametrize("missing", [0, 1, 2, 4])
def test_interval_contains_every_bounded_missing_completion(missing: int) -> None:
    import itertools

    rows = (score("observed", 150, 0),) + tuple(
        score(f"missing-{i}", None, None) for i in range(missing)
    )
    day = _Day(
        DAYS[0], "COVERED_CENSORED" if missing else "COVERED_COMPLETE",
        len(rows), rows, "a", (),
    )
    lo, hi = _bounds(day, "primary", 100)
    for completion in itertools.product((-100, 0, 100), repeat=missing):
        latent = (100 + sum(completion)) / len(rows)
        assert lo <= latent <= hi


def test_mid_session_latch_transition_cannot_be_hidden_by_rearming(
    signed_cohort: SignedCohort,
) -> None:
    bundle = build_bundle(signed_cohort, DAYS[3], mode="empty", safety_changed=True)
    signed_cohort.bundles.append(bundle)
    signed_cohort.index()
    result = signed_cohort.report()
    assert result.sessions[-1].status == "COVERAGE_UNVERIFIED"
    assert "journal_session_safety_history_changed_or_unverified" in result.sessions[-1].reasons


# Stage 4 fixtures are entirely synthetic, including artifacts, keys and approvals.
RESERVE_CLOCK = REGISTERED + timedelta(hours=1)
REVIEW_CLOCK = AS_OF + timedelta(minutes=1)
GENESIS = "0" * 64
RESERVE_DOMAIN = b"scorpion.prospective-study-approval.v1:reserve\x00"
REVIEW_DOMAIN = b"scorpion.prospective-study-approval.v1:review\x00"


@dataclass
class StudyFixture:
    folder: Path
    arms: list[SignedCohort]
    root: Path
    root_pin: str
    study: Path
    study_pin: str
    inputs: Path
    registry: Path
    registrar: Ed25519PrivateKey
    reviewer: Ed25519PrivateKey
    chain: str = GENESIS

    def kwargs(self) -> dict:
        return dict(
            registry_path=self.registry, root_path=self.root, root_sha256=self.root_pin,
            study_path=self.study, study_sha256=self.study_pin, inputs_path=self.inputs,
            expected_chain_hash=self.chain,
        )

    def approval(self, *, review: bool = False, dossier=None, disposition="INCONCLUSIVE",
                 signer=None) -> tuple[Path, Path]:
        now = REVIEW_CLOCK if review else RESERVE_CLOCK
        payload = {
            "schema": "scorpion.prospective-study-approval.v1", "authority": "RESEARCH_ONLY",
            "action": "REVIEW" if review else "RESERVE",
            "root_sha256": self.root_pin, "study_sha256": self.study_pin,
            "ledger_chain_hash": self.chain, "signer_id": "study-reviewer" if review else "registrar",
            "issued_ts_utc": now.isoformat(), "expires_ts_utc": (now + timedelta(days=1)).isoformat(),
        }
        if review:
            payload.update(dossier_sha256=dossier.dossier_sha256, disposition=disposition)
        path, signature = self.folder / "approval.json", self.folder / "approval.sig"
        put(path, payload)
        key = signer or (self.reviewer if review else self.registrar)
        signature.write_text(key.sign(
            (REVIEW_DOMAIN if review else RESERVE_DOMAIN) + path.read_bytes(),
        ).hex())
        return path, signature

    def reserve(self):
        approval, signature = self.approval()
        result = reserve_paper_study(
            **self.kwargs(), approval_path=approval, signature_path=signature, now=RESERVE_CLOCK,
        )
        self.chain = result.ledger_chain_hash
        return result

    def assess(self):
        return assess_paper_study(**self.kwargs(), as_of=AS_OF)

    def finalize(self, dossier, disposition="INCONCLUSIVE"):
        approval, signature = self.approval(review=True, dossier=dossier, disposition=disposition)
        result = finalize_paper_study(
            **self.kwargs(), as_of=AS_OF, approval_path=approval,
            signature_path=signature, now=REVIEW_CLOCK,
        )
        self.chain = result.ledger_chain_hash
        return result

    def status(self):
        return prospective_registry_status(
            self.registry, root_sha256=self.root_pin, expected_chain_hash=self.chain,
        )


def study_fixture(tmp_path: Path, *, paired: bool = False) -> StudyFixture:
    arms = [make_cohort(tmp_path / "incumbent")]
    if paired:
        arms.append(make_cohort(tmp_path / "challenger", rules="b" * 64, account="fixture-other"))
    registrar, reviewer = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    root = tmp_path / "root.json"
    root_pin = put(root, {
        "schema": "scorpion.prospective-study-root.v1", "authority": "RESEARCH_ONLY",
        "registry_id": "fixture-registry", "registered_ts_utc": REGISTERED.isoformat(),
        "registrar_id": "registrar", "reviewer_id": "study-reviewer",
        "registrar_public_key": registrar.public_key().public_bytes_raw().hex(),
        "reviewer_public_key": reviewer.public_key().public_bytes_raw().hex(),
    })
    plan_arms, input_arms = [], []
    frozen = REGISTERED - timedelta(days=1)
    for idx, arm in enumerate(arms):
        name = ("INCUMBENT" if idx == 0 else "CHALLENGER") if paired else "COHORT"
        artifact = arm.root / "artifact.bin"
        artifact.write_bytes(f"synthetic frozen strategy {name}".encode())
        artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
        manifest = arm.root / "training.json"
        revision = str(idx + 1) * 40
        manifest_hash = put(manifest, {
            "schema": "scorpion.frozen-training-lineage.v2", "authority": "RESEARCH_ONLY",
            "artifact_sha256": artifact_hash, "code_revision": revision, "rules_sha256": arm.rules,
            "trained_through_ts_utc": (frozen - timedelta(days=2)).isoformat(),
            "labels_available_through_ts_utc": (frozen - timedelta(days=1)).isoformat(),
            "frozen_ts_utc": frozen.isoformat(), "training_data_sha256": ["f" * 64],
            "artifact_kind": "DETERMINISTIC_RULES", "walk_forward_folds": [],
        })
        plan_arms.append({
            "arm": name, "cohort_sha256": arm.pin, "artifact_sha256": artifact_hash,
            "code_revision": revision, "training_manifest_sha256": manifest_hash,
            "frozen_ts_utc": frozen.isoformat(),
        })
        input_arms.append({
            "arm": name, "cohort": str(arm.cohort), "trust": str(arm.trust),
            "cost_policy": str(arm.cost_policy), "artifact": str(artifact),
            "training_manifest": str(manifest), "sessions": str(arm.inputs),
        })
    study, inputs = tmp_path / "study.json", tmp_path / "study-inputs.json"
    study_pin = put(study, {
        "schema": "scorpion.prospective-study.v1", "authority": "RESEARCH_ONLY",
        "family_id": "fixture-family", "kind": "PAIRED_ENTRY_TRIAL" if paired else "COHORT",
        "registered_ts_utc": REGISTERED.isoformat(), "alpha_units": 50_000_000,
        "embargo_seconds": 86400, "minimum_improvement_bps": 0,
        "maximum_challenger_cost_bps": 100, "soak_sessions": 2,
        "adverse_session_null_rate": 0.1, "adverse_session_alternative_rate": 0.8,
        "arms": plan_arms,
    })
    put(inputs, {"schema": "scorpion.prospective-study-inputs.v1", "arms": input_arms})
    ctx = StudyFixture(tmp_path, arms, root, root_pin, study, study_pin, inputs,
                       tmp_path / "hypothesis.db", registrar, reviewer)
    if paired:
        root_obj = _root(root, root_pin)
        study_obj = _study(study, study_pin, inputs, root_obj)
        for arm, spec in zip(arms, study_obj.arms, strict=True):
            evidence = load_verified_paper_cohort(
                cohort_path=arm.cohort, expected_cohort_sha256=arm.pin, inputs_path=arm.inputs,
                trust_path=arm.trust, cost_policy_path=arm.cost_policy, as_of=AS_OF,
            )
            by_day = dict(evidence.costs)
            for bundle in arm.bundles:
                day = date.fromisoformat(bundle.row["session_date"])
                cost = by_day[day]
                bundle.costs["trial_binding"] = {
                    "study_sha256": study_pin, "arm": spec.arm,
                    "artifact_sha256": spec.artifact_hash, "code_revision": spec.code_revision,
                    "common_roster_sha256": common_roster_digest(cost.routes),
                    "execution_manifest_sha256": execution_binding_digest(study_obj, spec, day, cost),
                }
                arm.sign(bundle)
    return ctx


def test_reserved_cohort_finalization_preserves_negative_result(tmp_path: Path) -> None:
    ctx = study_fixture(tmp_path)
    with pytest.raises(ValueError, match="registry unavailable"):
        ctx.assess()
    reservation = ctx.reserve()
    assert reservation.sessions == DAYS
    assert ctx.status().remaining_alpha_units == 0
    assert ctx.status().reserved_sessions == 4
    assert verify_hypothesis_memory(ctx.registry).valid
    result = ctx.assess()
    assert result.status == "NOT_QUALIFIED" and result == ctx.assess()
    assert not result.authorizes_training and not result.authorizes_promotion
    assert not result.authorizes_live_orders and not result.certifies_round_trip_profitability
    with pytest.raises(PaperQualificationError, match="disposition"):
        ctx.finalize(result, "PAPER_RESEARCH_REVIEWED")
    final = ctx.finalize(result, "INCONCLUSIVE")
    assert final.disposition == "INCONCLUSIVE"
    assert ctx.status().remaining_alpha_units == 0
    assert ctx.status().finalized_studies == 1
    stored = read_finalized_paper_dossier(
        ctx.registry, study_sha256=ctx.study_pin, root_sha256=ctx.root_pin,
        expected_chain_hash=ctx.chain,
    )
    assert stored["dossier"]["dossier_sha256"] == result.dossier_sha256
    assert stored["disposition"] == "INCONCLUSIVE"
    assert verify_hypothesis_memory(ctx.registry).valid
    with pytest.raises(ValueError, match="already finalized"):
        ctx.assess()


def test_approval_signature_independence_and_actual_reservation_clock(tmp_path: Path) -> None:
    ctx = study_fixture(tmp_path)
    approval, signature = ctx.approval(signer=ctx.reviewer)
    with pytest.raises(PaperQualificationError, match="signature"):
        reserve_paper_study(**ctx.kwargs(), approval_path=approval,
                            signature_path=signature, now=RESERVE_CLOCK)
    approval, signature = ctx.approval()
    with pytest.raises(PaperQualificationError, match="not_prospective"):
        reserve_paper_study(**ctx.kwargs(), approval_path=approval,
                            signature_path=signature, now=stamp(DAYS[0], 15))
    assert not ctx.registry.exists()
    ctx.reserve()
    old = ctx.chain
    with pytest.raises(ValueError, match="already reserved"):
        ctx.reserve()
    ctx.chain = GENESIS
    with pytest.raises(ValueError, match="checkpoint mismatch"):
        ctx.assess()
    ctx.chain = old
    assert ctx.status().reservations == 1


def test_new_study_pin_cannot_reuse_reserved_calendar_or_budget(tmp_path: Path) -> None:
    ctx = study_fixture(tmp_path)
    ctx.reserve()
    plan = json.loads(ctx.study.read_text())
    plan["minimum_improvement_bps"] = 1
    ctx.study_pin = put(ctx.study, plan)
    with pytest.raises(ValueError, match="roster overlaps"):
        ctx.reserve()
    arm = ctx.arms[0]
    policy_data = json.loads(arm.cohort.read_text())
    policy_data["start_date"], policy_data["end_date"] = "2026-11-30", "2026-12-03"
    policy_data["sessions"] = ["2026-11-30", "2026-12-01", "2026-12-02", "2026-12-03"]
    plan["arms"][0]["cohort_sha256"] = put(arm.cohort, policy_data)
    ctx.study_pin = put(ctx.study, plan)
    with pytest.raises(ValueError, match="alpha budget"):
        ctx.reserve()
    assert ctx.status().reservations == 1


def test_signed_review_cannot_finalize_changed_evidence(tmp_path: Path) -> None:
    ctx = study_fixture(tmp_path)
    ctx.reserve()
    result = ctx.assess()
    approval, signature = ctx.approval(review=True, dossier=result)
    arm = ctx.arms[0]
    arm.index(arm.bundles[1:])
    with pytest.raises(PaperQualificationError, match="dossier_mismatch"):
        finalize_paper_study(
            **ctx.kwargs(), as_of=AS_OF, approval_path=approval,
            signature_path=signature, now=REVIEW_CLOCK,
        )
    assert ctx.status().finalized_studies == 0


def test_ledger_tamper_refuses_read_even_when_current_pin_supplied(tmp_path: Path) -> None:
    ctx = study_fixture(tmp_path)
    ctx.reserve()
    with sqlite3.connect(ctx.registry) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            db.execute("UPDATE prospective_study_reservations SET alpha_units=1")
        db.execute("DROP TRIGGER prospective_study_reservations_no_update")
        db.execute("UPDATE prospective_study_reservations SET alpha_units=1")
    assert not verify_hypothesis_memory(ctx.registry).valid
    with pytest.raises(ValueError, match="integrity"):
        ctx.assess()


def test_paired_trial_matches_same_opportunities_and_keeps_missing_day(tmp_path: Path) -> None:
    ctx = study_fixture(tmp_path, paired=True)
    ctx.reserve()
    result = ctx.assess()
    pair = result.paired_entry
    assert pair is not None
    assert pair.observed_pairs == 2 and pair.complete_sessions == 3
    assert pair.sessions[-1].status == "UNKNOWN"
    assert pair.endpoint.anytime_conditional_mean.alpha == pytest.approx(0.04)
    assert pair.degradation.alpha == pytest.approx(0.01)
    assert pair.degradation.unknown_sessions == 1
    assert pair.endpoint.raw_route_mean_bps == 0
    assert "paired_session_coverage_incomplete" in result.blockers
    assert result.status == "NOT_QUALIFIED"
    assert result == ctx.assess()


def test_paired_market_horizon_mismatch_is_censored_not_matched(tmp_path: Path) -> None:
    ctx = study_fixture(tmp_path, paired=True)
    ctx.reserve()
    arm = ctx.arms[1]
    bundle = arm.bundles[0]
    bundle.costs["marks"][0]["bid"] = "10.39"
    bundle.costs["marks"][0]["ask"] = "10.41"
    arm.sign(bundle)
    pair = ctx.assess().paired_entry
    assert pair is not None
    assert pair.sessions[0].status == "COVERED_CENSORED"
    assert pair.sessions[0].matched_observed == 0
    assert pair.sessions[0].raw_improvement_mean_bps is None


def test_signed_trial_binding_must_match_frozen_artifact(tmp_path: Path) -> None:
    ctx = study_fixture(tmp_path, paired=True)
    ctx.reserve()
    arm = ctx.arms[1]
    bundle = arm.bundles[0]
    bundle.costs["trial_binding"]["artifact_sha256"] = "0" * 64
    arm.sign(bundle)
    with pytest.raises(PaperQualificationError, match="binding_mismatch"):
        ctx.assess()


def test_training_embargo_rejects_future_label_availability(tmp_path: Path) -> None:
    ctx = study_fixture(tmp_path)
    plan = json.loads(ctx.study.read_text())
    inputs = json.loads(ctx.inputs.read_text())
    manifest_path = Path(inputs["arms"][0]["training_manifest"])
    manifest = json.loads(manifest_path.read_text())
    manifest["labels_available_through_ts_utc"] = stamp(DAYS[0], 15).isoformat()
    plan["arms"][0]["training_manifest_sha256"] = put(manifest_path, manifest)
    ctx.study_pin = put(ctx.study, plan)
    with pytest.raises(PaperQualificationError, match="embargo"):
        ctx.reserve()


def test_frozen_walk_forward_rejects_future_labels_and_reused_holdout() -> None:
    t = REGISTERED - timedelta(days=20)
    embargo = timedelta(days=1)
    folds = tuple(FrozenValidationWindow(
        t + timedelta(days=3 * i), t + timedelta(days=3 * i),
        t + timedelta(days=3 * i + 1), t + timedelta(days=3 * i + 2),
        t + timedelta(days=3 * i + 2),
        ("a" * 64,) if i == 0 else ("a" * 64, str(i) * 64),
        str(i + 1) * 64,
    ) for i in range(3))
    data = frozenset({"a" * 64, "1" * 64, "2" * 64, "3" * 64})
    assert verify_frozen_validation_windows(
        folds, embargo=embargo, frozen=REGISTERED, declared_data=data,
    ).valid
    future = replace(folds[0], training_data_hashes=("a" * 64, "3" * 64))
    result = verify_frozen_validation_windows(
        (future, *folds[1:]), embargo=embargo, frozen=REGISTERED, declared_data=data,
    )
    assert "fold_future_validation_data_reuse:0" in result.failures
    late = replace(folds[0], training_labels_available_through=folds[0].validation_start)
    assert not verify_frozen_validation_windows(
        (late, *folds[1:]), embargo=embargo, frozen=REGISTERED, declared_data=data,
    ).valid
    duplicate = replace(folds[1], validation_data_hash=folds[0].validation_data_hash)
    assert not verify_frozen_validation_windows(
        (folds[0], duplicate, folds[2]), embargo=embargo, frozen=REGISTERED, declared_data=data,
    ).valid


def test_trained_model_requires_declared_purged_folds(tmp_path: Path) -> None:
    ctx = study_fixture(tmp_path)
    plan = json.loads(ctx.study.read_text())
    inputs = json.loads(ctx.inputs.read_text())
    manifest_path = Path(inputs["arms"][0]["training_manifest"])
    manifest = json.loads(manifest_path.read_text())
    manifest["artifact_kind"] = "TRAINED_MODEL"
    plan["arms"][0]["training_manifest_sha256"] = put(manifest_path, manifest)
    ctx.study_pin = put(ctx.study, plan)
    with pytest.raises(PaperQualificationError, match="walk_forward_lineage"):
        ctx.reserve()
