from dataclasses import replace
from datetime import timedelta

from scorpion.domain import BookState, EventKind
from scorpion.parser import parse_message
from scorpion.sequence_guard import SequenceSeverity, assess_sequence
from scorpion.source_intelligence import SourceBehaviorProfile, compare_source_behavior


def test_orphan_followup_is_critical(raw_factory):
    event = replace(parse_message(raw_factory("closed runners +19%")), kind=EventKind.EXIT)
    assessment = assess_sequence(event, BookState())
    assert assessment.critical is True
    assert any(
        finding.code == "followup_without_contract"
        and finding.severity is SequenceSeverity.CRITICAL
        for finding in assessment.findings
    )


def test_source_timestamp_regression_requires_review(raw_factory):
    older = parse_message(raw_factory("QQQ 719C TODAY @ 1.01"))
    newer = replace(older, event_id="newer", source_ts_utc=older.source_ts_utc + timedelta(seconds=5))
    assessment = assess_sequence(older, BookState(), recent_events=(newer,))
    assert assessment.requires_review is True
    assert any(finding.code == "source_timestamp_regression" for finding in assessment.findings)


def test_source_behavior_shift_detects_edit_and_ambiguity_spike():
    baseline = SourceBehaviorProfile(
        "author",
        "channel",
        100,
        2,
        60,
        5,
        35,
        20,
        0.02,
        0.60,
        0.05,
    )
    recent = SourceBehaviorProfile(
        "author",
        "channel",
        40,
        12,
        12,
        16,
        12,
        18,
        0.30,
        0.30,
        0.40,
    )
    shift = compare_source_behavior(recent, baseline)
    assert shift.suspicious is True
    assert shift.score >= 1.0
    assert any(reason.startswith("edit_rate_shift") for reason in shift.reasons)
    assert any(reason.startswith("ambiguity_rate_shift") for reason in shift.reasons)
