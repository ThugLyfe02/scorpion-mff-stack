"""The pre-trade gate: every check runs, and silence is a refusal."""

import pytest

from scorpion.guard import (
    DEFAULT_CHECKS,
    PASS,
    REFUSE,
    UNEVALUABLE,
    GuardError,
    audit_record,
    check_burst,
    check_cooldown,
    check_drift,
    check_tail_size,
    check_vol_estimate,
    evaluate,
    render,
)

CLEAN = {
    "drifted": False,
    "drift_distance": 0.01,
    "vol_age_seconds": 60.0,
    "vol_memory_seconds": 300.0,
    "tail_probability": 0.001,
    "tail_tolerance": 0.01,
    "branching_ratio": 0.2,
    "branching_limit": 0.8,
    "blocked_rate": 0.05,
    "since_halt_seconds": 7200.0,
    "cooldown_seconds": 900.0,
}


def test_a_clean_context_is_allowed():
    decision = evaluate(CLEAN)
    assert decision.allowed
    assert len(decision.verdicts) == len(DEFAULT_CHECKS)
    assert all(v.outcome == PASS for v in decision.verdicts)


def test_a_missing_drift_measurement_refuses():
    context = dict(CLEAN)
    context.pop("drifted")
    decision = evaluate(context)
    assert not decision.allowed
    assert any(v.outcome == UNEVALUABLE for v in decision.blocking)


def test_drift_blocks_even_when_everything_else_is_clean():
    decision = evaluate({**CLEAN, "drifted": True})
    assert not decision.allowed
    assert "drift" in [v.check for v in decision.blocking]


def test_a_stale_volatility_estimate_blocks():
    verdict = check_vol_estimate({**CLEAN, "vol_age_seconds": 900.0})
    assert verdict.outcome == REFUSE
    assert "memory length" in verdict.detail


def test_a_clip_outside_the_tail_tolerance_blocks():
    verdict = check_tail_size({**CLEAN, "tail_probability": 0.05})
    assert verdict.blocks


def test_high_clustering_blocks_and_mentions_the_cap():
    verdict = check_burst({**CLEAN, "branching_ratio": 0.95})
    assert verdict.blocks
    assert "cap met" in verdict.detail


def test_a_halt_inside_its_cooldown_blocks():
    verdict = check_cooldown({**CLEAN, "since_halt_seconds": 10.0})
    assert verdict.blocks


def test_every_check_is_reported_even_after_a_refusal():
    decision = evaluate({**CLEAN, "drifted": True, "branching_ratio": 0.99})
    assert len(decision.verdicts) == len(DEFAULT_CHECKS)
    assert len(decision.blocking) >= 2


def test_a_raising_check_becomes_unevaluable_rather_than_a_pass():
    decision = evaluate({}, checks=(check_vol_estimate,))
    assert not decision.allowed
    assert decision.verdicts[0].outcome == UNEVALUABLE


def test_an_empty_gate_is_refused():
    with pytest.raises(GuardError):
        evaluate(CLEAN, checks=())


def test_a_pass_is_not_a_default_of_last_resort():
    assert check_drift({}).outcome == UNEVALUABLE


def test_the_audit_record_names_the_contract_and_the_blocking_checks():
    decision = evaluate({**CLEAN, "drifted": True})
    record = audit_record(decision, contract_key="QQQ|CALL|732|2026-08-13", size_usd=1500.0,
                          as_of="2026-08-13T10:00:00Z")
    assert record["refused"] is True
    assert record["contract_key"].startswith("QQQ")
    assert "drift" in record["blocking"]
    with pytest.raises(GuardError):
        audit_record(decision, contract_key="", size_usd=1.0, as_of="x")


def test_render_marks_blocking_checks():
    text = render(evaluate({**CLEAN, "drifted": True}))
    assert text.startswith("refused")
    assert "[x] drift" in text


def test_verdicts_serialise_without_none_arithmetic():
    decision = evaluate(CLEAN)
    payload = decision.to_dict()
    assert payload["allowed"] is True
    assert all(isinstance(v["blocks"], bool) for v in payload["verdicts"])
