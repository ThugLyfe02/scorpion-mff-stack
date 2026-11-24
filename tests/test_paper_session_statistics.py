"""Synthetic evidence and adversarial tests for Stage 3; no market claims."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scorpion.causal_features import load_paper_intent_features
from scorpion.confidence_sequence import anytime_bounded_mean_confidence_sequence
from scorpion.cost_adjusted_policy_value import session_mean_diagnostics
from scorpion.exec_cli import main
from scorpion.paper_session_statistics import (
    PaperCohortError,
    PaperCohortPolicy,
    _bounds,
    _Day,
    _evaluate_days,
    _RouteScore,
    evaluate_paper_session_cohort,
)
from scorpion.session_calendar import session_hours
from scorpion.session_service import Journal

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
        put(self.inputs, {"schema": "scorpion.paper-session-inputs.v1",
                          "sessions": [b.row for b in (self.bundles if bundles is None else bundles)]})

    def report(self, as_of: datetime = AS_OF):
        return evaluate_paper_session_cohort(
            cohort_path=self.cohort, expected_cohort_sha256=self.pin, inputs_path=self.inputs,
            trust_path=self.trust, cost_policy_path=self.cost_policy, as_of=as_of,
        )


def build_bundle(
    ctx: SignedCohort, day: date, *, mode: str, disconnects: int = 0,
    include_markers: bool = True,
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
    common = {"session_date": day.isoformat(), "mode": "PAPER", "rules_sha256": RULES}
    if include_markers:
        journal.submit({"kind": "session_start", **common,
                        "ts_utc": (open_at - timedelta(seconds=1)).isoformat(),
                        "signal_ingress_enabled": True})
    if mode != "empty":
        journal.submit({
            "kind": "decision", "action": "schedule_entry", "call_id": call,
            "ticker": ticker, "rules_sha256": RULES, "source_ts_utc": source.isoformat(),
            "received_ts_utc": source.isoformat(), "ts_utc": source.isoformat(),
            "revision_id": hashlib.sha256(call.encode()).hexdigest(),
            "content_sha256": "c" * 64,
        })
        for chase, moment in [(False, first)] + ([(True, second)] if mode == "chase" else []):
            journal.submit({
                "kind": "intent", "call_id": call, "ticker": ticker, "rules_sha256": RULES,
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
            "ingress_overflow": False, "pending_ingress": 0,
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
    venue_hash = put(root / "venue.json", {"synthetic": "venue", "date": day.isoformat()})
    market_hash = put(root / "market.json", {"synthetic": "market", "date": day.isoformat()})
    outcome = {
        "schema": "scorpion.paper-outcome-export.v1", "authority": "PAPER_ONLY",
        "source_id": "fixture-venue", "account_ref": "fixture-account",
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


@pytest.fixture
def signed_cohort(tmp_path: Path) -> SignedCohort:
    collector = Ed25519PrivateKey.generate()
    reviewer = Ed25519PrivateKey.generate()
    trust = tmp_path / "trust.json"
    trust_hash = put(trust, {
        "schema": "scorpion.paper-outcome-trust.v1", "authority": "PAPER_ONLY",
        "source_id": "fixture-venue", "account_ref": "fixture-account", "rules_sha256": RULES,
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
                       pin, trust_hash, cost_hash, collector, reviewer, [])
    ctx.bundles.extend([
        build_bundle(ctx, DAYS[0], mode="full"), build_bundle(ctx, DAYS[1], mode="chase"),
        build_bundle(ctx, DAYS[2], mode="empty"),
    ])
    ctx.index()
    return ctx


def test_signed_cohort_keeps_calendar_and_missing_day(signed_cohort: SignedCohort) -> None:
    result = signed_cohort.report()
    assert result.planned_sessions == result.elapsed_sessions == 4
    assert result.complete_sessions == 3
    assert result.known_expected_routes == 2
    assert result.observed_primary_routes == 2 and result.executed_chase_routes == 1
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
