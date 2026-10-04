import asyncio

from scorpion.eligibility import (
    EligibilityDisposition,
    StrategyBucket,
    classify_eligibility,
)
from scorpion.integrity import IntegrityLedger
from scorpion.parser import parse_message
from scorpion.pipeline import Pipeline
from scorpion.store import Store


def test_core_single_name_is_review_eligible(raw_factory):
    event = parse_message(raw_factory("TSLA 345C TODAY @ 1.00"))
    decision = classify_eligibility(event)
    assert decision.bucket is StrategyBucket.CORE_SINGLE_NAME
    assert decision.disposition is EligibilityDisposition.ALLOW_REVIEW


def test_etf_symbol_is_research_only_even_outside_etf_channel(raw_factory):
    event = parse_message(
        raw_factory(
            "QQQ 719C TODAY @ 1.01",
            channel_id="1448448931116748993",
        )
    )
    decision = classify_eligibility(event)
    assert decision.bucket is StrategyBucket.ETF
    assert decision.disposition is EligibilityDisposition.RESEARCH_ONLY
    assert decision.reason == "etf_underlying_research_only"


def test_etf_channel_is_research_only(raw_factory):
    event = parse_message(
        raw_factory(
            "TSLA 345C TODAY @ 1.00",
            channel_id="1231301953972207667",
        )
    )
    decision = classify_eligibility(event)
    assert decision.bucket is StrategyBucket.ETF
    assert decision.reason == "etf_channel_research_only"


def test_lotto_language_is_research_only(raw_factory):
    event = parse_message(raw_factory("TSLA 345C TODAY @ 1.00 ER LOTTO"))
    decision = classify_eligibility(event)
    assert decision.bucket is StrategyBucket.LOTTO
    assert decision.disposition is EligibilityDisposition.RESEARCH_ONLY


def test_pipeline_persists_strategy_block_without_review_urgency(tmp_path, raw_factory):
    store = Store(tmp_path / "strategy.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    event, effects = asyncio.run(
        pipeline.handle(
            raw_factory(
                "QQQ 719C TODAY @ 1.01",
                channel_id="1448448931116748993",
            )
        )
    )
    assert effects
    with store.connect() as db:
        effect = db.execute(
            "SELECT status FROM proposed_effects WHERE source_event_id=?",
            (event.event_id,),
        ).fetchone()
        packet = db.execute(
            "SELECT disposition FROM operator_decision_packets WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
    assert effect["status"] == "BLOCKED_STRATEGY"
    assert packet["disposition"] == "BLOCKED_STRATEGY"
    assert store.health_snapshot()["pending_review_effects"] == 0
    assert IntegrityLedger(store.path).verify_database().ok is True
