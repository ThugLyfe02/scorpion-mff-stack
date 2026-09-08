from datetime import UTC, datetime, timedelta

from scorpion.resilience import OperationalMode, assess_resilience


def _health(**overrides):
    payload = {
        "halt": {"halted": False, "reason": ""},
        "pending_raw_revisions": 0,
        "pending_review_effects": 0,
        "decision_health": {
            "count": 50,
            "ambiguity_rate": 0.0,
            "low_confidence_rate": 0.0,
            "unresolved_association_rate": 0.0,
            "parser_p95_us": 100.0,
            "pipeline_p95_us": 500.0,
        },
        "heartbeats": {},
    }
    payload.update(overrides)
    return payload


def test_resilience_normal_when_quality_and_backlog_are_clean():
    assessment = assess_resilience(_health())
    assert assessment.mode is OperationalMode.NORMAL
    assert assessment.signals == ()


def test_resilience_degrades_on_semantic_quality_spike():
    health = _health()
    health["decision_health"]["ambiguity_rate"] = 0.35
    assessment = assess_resilience(health)
    assert assessment.mode is OperationalMode.DEGRADED
    assert any(signal.code == "ambiguity_spike" for signal in assessment.signals)


def test_resilience_halts_on_stale_pipeline_heartbeat():
    now = datetime.now(UTC)
    health = _health(
        heartbeats={
            "pipeline": {"last_seen_ts_utc": (now - timedelta(seconds=20)).isoformat()}
        }
    )
    assessment = assess_resilience(health, now=now)
    assert assessment.mode is OperationalMode.HALTED
    assert any("heartbeat_stale_critical" in signal.code for signal in assessment.signals)
