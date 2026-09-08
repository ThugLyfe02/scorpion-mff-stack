import asyncio

from scorpion.pipeline import Pipeline
from scorpion.store import Store


def test_pipeline_recovers_and_deduplicates(tmp_path, raw_factory):
    store = Store(tmp_path / "s.db")
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    raw = raw_factory("QQQ 719C TODAY @ 1.01")

    event, first_effects = asyncio.run(pipeline.handle(raw))
    assert len(first_effects) == 1

    # New process from same durable store reconstructs state.
    recovered = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    assert event.contract_key in recovered.state.positions

    _, duplicate_effects = asyncio.run(recovered.handle(raw))
    assert duplicate_effects == ()
